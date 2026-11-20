import asyncio, json, os, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ports
from ports import Paused, PauseError, Held, parse_cgroup, parse_ss_pids, STATE
from remote import Res

CID = 'a' * 64


class Fake:
    """Имитация сервера: контейнеры, юниты, слушатели порта 80."""
    def __init__(self, holders, fail_start=()):
        self.h = {x['name']: dict(x, running=True) for x in holders}
        self.files, self.log, self.fail_start = {}, [], set(fail_start)

    def _holders(self):
        return [x for x in self.h.values() if x['running']]

    async def put(self, path, text, mode='600'):
        self.files[path] = text

    async def run(self, cmd, timeout=120, stdin=None):
        self.log.append(cmd)
        hs = self._holders()
        if cmd == 'command -v docker':
            return Res(0, '/usr/bin/docker')
        if cmd.startswith('docker ps --filter publish=80'):
            return Res(0, '\n'.join(x['name'] for x in hs if x['kind'] == 'docker' and x.get('published')))
        if cmd.startswith("ss -H -ltnp"):
            return Res(0, '\n'.join(f'LISTEN 0 511 *:80 *:* users:(("{x["comm"]}",pid={x["pid"]},fd=6))' for x in hs))
        if cmd.startswith("ss -H -ltn "):
            return Res(0, 'LISTEN 0 511 *:80 *:*' if hs else '')
        m = re.match(r'cat /proc/(\d+)/(comm|cgroup)', cmd)
        if m:
            x = next(x for x in self.h.values() if x['pid'] == int(m.group(1)))
            return Res(0, x['comm'] if m.group(2) == 'comm' else x['cgroup'])
        if cmd.startswith("docker inspect -f '{{.Name}}'"):
            return Res(0, '/' + next(n for n, x in self.h.items() if x.get('id') == cmd.split()[-1]))
        m = re.match(r"docker inspect -f '\{\{\.State\.Running\}\}' (\S+)", cmd)
        if m:
            return Res(0, 'true' if self.h[m.group(1)]['running'] else 'false')
        m = re.match(r'(?:docker stop -t 20|systemctl stop) (\S+)', cmd)
        if m:
            self.h[m.group(1)]['running'] = False
            return Res(0, '')
        m = re.match(r'(?:docker start|systemctl start) (\S+)', cmd)
        if m:
            if m.group(1) not in self.fail_start:
                self.h[m.group(1)]['running'] = True
            return Res(0, '')
        m = re.match(r'systemctl is-active (\S+)', cmd)
        if m:
            return Res(0, 'active' if self.h[m.group(1)]['running'] else 'inactive')
        if cmd.startswith('cat ') and STATE in cmd:
            return Res(0, self.files[STATE]) if STATE in self.files else Res(1, '')
        if cmd.startswith('rm -f') and STATE in cmd:
            self.files.pop(STATE, None)
            return Res(0, '')
        raise AssertionError('неожиданная команда: ' + cmd)


NGINX = dict(kind='unit', name='nginx.service', pid=100, comm='nginx', cgroup='0::/system.slice/nginx.service')
APP = dict(kind='docker', name='web', pid=200, comm='caddy', id=CID,
           cgroup=f'0::/system.slice/docker-{CID}.scope', published=True)


def run(c):
    return asyncio.run(c)


def test_parsers():
    assert parse_ss_pids('x users:(("nginx",pid=12,fd=6),("nginx",pid=13,fd=6))') == [12, 13]
    assert parse_cgroup(f'0::/system.slice/docker-{CID}.scope') == ('docker', CID)
    assert parse_cgroup('0::/system.slice/nginx.service') == ('unit', 'nginx.service')
    assert parse_cgroup('0::/user.slice/user-0.slice/user@0.service/app.slice/x.service') == (None, None)
    assert parse_cgroup('0::/init.scope') == (None, None)
    assert ports.is_protected('docker.service') and ports.is_protected('systemd-resolved.service')


def test_pause_and_restore():
    f = Fake([NGINX, APP])

    async def go():
        async with Paused(f, 80) as p:
            assert {h.name for h in p.held} == {'nginx.service', 'web'}
            assert not f._holders(), 'порт должен быть свободен внутри'
            assert STATE in f.files
        return p
    p = run(go())
    assert all(x['running'] for x in f.h.values()), 'всё должно вернуться'
    assert not p.problems and STATE not in f.files


def test_restore_when_work_fails():
    f = Fake([NGINX])

    async def go():
        async with Paused(f, 80):
            raise RuntimeError('certbot упал')
    try:
        run(go()); assert False
    except RuntimeError:
        pass
    assert f.h['nginx.service']['running']


def test_unmanaged_process_aborts_untouched():
    f = Fake([NGINX, dict(kind='proc', name='py', pid=300, comm='python3', cgroup='0::/init.scope')])

    async def go():
        async with Paused(f, 80):
            pass
    try:
        run(go()); assert False
    except PauseError as e:
        assert 'python3' in str(e)
    assert all(x['running'] for x in f.h.values())
    assert not any('stop' in c for c in f.log), 'до проверки ничего не должно останавливаться'


def test_docker_proxy_maps_to_container():
    web = dict(kind='docker', name='web', pid=400, comm='docker-proxy', id=CID,
               cgroup='0::/system.slice/docker.service', published=True)
    f = Fake([web])

    async def go():
        async with Paused(f, 80) as p:
            assert [h.name for h in p.held] == ['web']
    run(go())
    assert f.h['web']['running']
    assert not any('systemctl stop docker' in c for c in f.log)


def test_start_failure_is_reported_and_state_kept():
    f = Fake([NGINX], fail_start={'nginx.service'})
    orig, ports.asyncio.sleep = ports.asyncio.sleep, (lambda s: orig(0))

    async def go():
        async with Paused(f, 80) as p:
            pass
        return p
    try:
        p = run(go())
    finally:
        ports.asyncio.sleep = orig
    assert p.problems and STATE in f.files
    f.fail_start.clear()
    orig2, ports.asyncio.sleep = ports.asyncio.sleep, (lambda s: orig(0))
    try:
        res = run(ports.restore_from_state(f))
    finally:
        ports.asyncio.sleep = orig2
    assert f.h['nginx.service']['running'] and STATE not in f.files and 'nginx' in res[0]


def test_nothing_on_port_80():
    f = Fake([])

    async def go():
        async with Paused(f, 80) as p:
            assert p.held == []
    run(go())
    assert STATE not in f.files


def test_github_dump_and_update():
    import github, httpx, base64
    store = {'content': {'version': 1, 'servers': [{'id': 'REPLACE-1', 'host': 'REPLACE.example.com'},
                                                   {'id': 'nl', 'host': 'nl.x.xyz', 'port': 1}]}, 'put': None}

    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'sha': 's1', 'content': base64.b64encode(json.dumps(store['content']).encode()).decode()})
        store['put'] = json.loads(req.content)
        return httpx.Response(200, json={'commit': {'html_url': 'https://gh/commit/1'}})
    real = httpx.AsyncClient
    github.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    cfg = type('C', (), dict(gh_repo='o/r', gh_path='chrome/servers.json', gh_branch='main', gh_token='t'))()
    try:
        url = run(github.update_servers(cfg, add={'id': 'fi', 'host': 'fi.x.xyz', 'port': 9444}))
    finally:
        github.httpx.AsyncClient = real
    out = json.loads(base64.b64decode(store['put']['content']))
    assert url.startswith('https://gh') and store['put']['sha'] == 's1'
    assert [s['id'] for s in out['servers']] == ['nl', 'fi'], out      # заглушка REPLACE ушла
    assert 'REPLACE' not in base64.b64decode(store['put']['content']).decode()


def test_github_errors_and_add_many():
    import github, httpx, base64
    cfg = type('C', (), dict(gh_repo='o/r', gh_path='p.json', gh_branch='main', gh_token='t', proxy_user='u', proxy_pass='x'))()
    real = httpx.AsyncClient
    github.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})), **kw)
    try:
        run(github.update_servers(cfg, add={'id': 'a', 'host': 'a.x'})); assert False
    except RuntimeError as e:
        assert '401' in str(e) and 'токен' in str(e)
    finally:
        github.httpx.AsyncClient = real
    put = {}
    def handler(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'sha': 's', 'content': base64.b64encode(b'{"version":1,"servers":[{"id":"old","host":"old.x"}]}').decode()})
        put.update(json.loads(req.content)); return httpx.Response(200, json={'commit': {'html_url': 'u'}})
    github.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        run(github.update_servers(cfg, add_many=[{'id': 'a', 'host': 'a.x'}, {'id': 'b', 'host': 'b.x'}]))
    finally:
        github.httpx.AsyncClient = real
    assert [x['id'] for x in json.loads(base64.b64decode(put['content']))['servers']] == ['old', 'a', 'b']
    import flow
    e = flow.entry(cfg, 'nl1.x.xyz', {'name': 'NL', 'code': 'NL', 'port': 9443})
    assert (e['id'], e['host'], e['port'], e['username']) == ('nl1', 'nl1.x.xyz', 9443, 'u')


def test_xray_config_and_certbot_cmd():
    import proxy, certs
    cfg = type('C', (), dict(proxy_user='free', proxy_pass='pw', certbot_base='/opt/certbot', email='a@b.c', http_port=80))()
    c = proxy.xray_config(cfg, 'fi.x.xyz', 9444)
    assert c['inbounds'][0]['port'] == 9444 and c['inbounds'][0]['protocol'] == 'http'
    assert '/live/fi.x.xyz/' in json.dumps(c)
    cmd = certs._certbot_cmd(cfg, 'fi.x.xyz', True, False)
    assert '--network host' in cmd and '-d fi.x.xyz' in cmd and '/opt/certbot/certs:/etc/letsencrypt' in cmd


def test_ssh_tofu_run_put():
    import asyncssh, remote

    async def go():
        tmp = Path(tempfile.mkdtemp())
        hostkey = asyncssh.generate_private_key('ssh-ed25519'); hostkey.write_private_key(tmp / 'h')
        userkey = asyncssh.generate_private_key('ssh-ed25519'); userkey.write_private_key(tmp / 'u')

        class S(asyncssh.SSHServer):
            def begin_auth(self, u): return False

        async def handle(proc):
            try:
                data = await asyncio.wait_for(proc.stdin.read(), 1)
            except asyncio.TimeoutError:
                data = ''
            p = await asyncio.create_subprocess_shell(proc.command, stdin=asyncio.subprocess.PIPE,
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            o, e = await p.communicate(data.encode())
            proc.stdout.write(o.decode()); proc.stderr.write(e.decode()); proc.exit(p.returncode)

        srv = await asyncssh.create_server(S, '127.0.0.1', 0, server_host_keys=[str(tmp / 'h')], process_factory=handle)
        port = srv.sockets[0].getsockname()[1]
        kh = tmp / 'kh'
        try:
            await remote.Remote.open('127.0.0.1', port, 'root', str(tmp / 'u'), kh)  # без trust_new — отказ
            assert False
        except PermissionError:
            pass
        async with await remote.Remote.open('127.0.0.1', port, 'root', str(tmp / 'u'), kh, trust_new=True) as r:
            assert r.fingerprint.startswith('SHA256:')
            assert (await r.run('echo hi')).out == 'hi'
            assert (await r.run('exit 3')).rc == 3
            await r.put(str(tmp / 'd/x.json'), '{"a":1}')
            assert (tmp / 'd/x.json').read_text() == '{"a":1}'
        async with await remote.Remote.open('127.0.0.1', port, 'root', str(tmp / 'u'), kh) as r2:  # теперь строго
            assert r2.fingerprint is None and (await r2.run('echo ok')).out == 'ok'
        srv.close()
    run(go())


def test_password_login_installs_key_and_keys_module():
    import asyncssh, remote, keys
    async def go():
        tmp = Path(tempfile.mkdtemp())
        hk = asyncssh.generate_private_key('ssh-ed25519'); hk.write_private_key(tmp / 'h')
        class S(asyncssh.SSHServer):
            def begin_auth(self, u): return True
            def password_auth_supported(self): return True
            def validate_password(self, u, p): return p == 'secret'
        async def handle(proc):
            env = dict(os.environ, HOME=str(tmp / 'home'))
            (tmp / 'home').mkdir(exist_ok=True)
            p = await asyncio.create_subprocess_shell(proc.command, stdout=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.PIPE, env=env)
            o, e = await p.communicate()
            proc.stdout.write(o.decode()); proc.stderr.write(e.decode()); proc.exit(p.returncode)
        srv = await asyncssh.create_server(S, '127.0.0.1', 0, server_host_keys=[str(tmp / 'h')], process_factory=handle)
        port = srv.sockets[0].getsockname()[1]
        kh = tmp / 'kh'
        try:
            await remote.Remote.open('127.0.0.1', port, 'root', 'x', kh, trust_new=True, password='bad'); assert False
        except asyncssh.PermissionDenied:
            pass
        cfg = type('C', (), dict(ssh_key=str(tmp / 'k' / 'id')))()
        keys.ensure_bot_key(cfg)
        assert oct((tmp / 'k' / 'id').stat().st_mode & 0o777) == '0o600'
        pub = keys.pub_of(cfg.ssh_key)
        async with await remote.Remote.open('127.0.0.1', port, 'root', 'x', kh, trust_new=True, password='secret') as r:
            await r.install_pubkey(pub); await r.install_pubkey(pub)     # дважды — без дублей
        ak = (tmp / 'home/.ssh/authorized_keys').read_text()
        assert ak.count(pub) == 1
        srv.close()
        # импорт ключа
        k = asyncssh.generate_private_key('ssh-ed25519')
        assert keys.import_key(k.export_private_key().decode())
        for bad in (pub, 'мусор'):
            try:
                keys.import_key(bad); assert False
            except ValueError:
                pass
    run(go())


if __name__ == '__main__':
    for n, f in list(globals().items()):
        if n.startswith('test_'):
            f(); print('ok', n)
