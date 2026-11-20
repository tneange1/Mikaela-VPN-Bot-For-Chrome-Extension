from __future__ import annotations
import asyncio
import json
import time

import httpx

DIR = '/opt/mikaela-proxy'
NAME = 'mikaela-proxy'


def xray_config(cfg, domain: str, port: int) -> dict:
    return {
        'log': {'loglevel': 'none'},
        'inbounds': [{
            'tag': 'chrome-https', 'listen': '0.0.0.0', 'port': port, 'protocol': 'http',
            'settings': {'accounts': [{'user': cfg.proxy_user, 'pass': cfg.proxy_pass}], 'allowTransparent': False},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
            'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {
                'alpn': ['http/1.1'],
                'certificates': [{'certificateFile': f'/etc/letsencrypt/live/{domain}/fullchain.pem',
                                  'keyFile': f'/etc/letsencrypt/live/{domain}/privkey.pem'}]}},
        }],
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}, {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [
            {'ip': ['geoip:private'], 'outboundTag': 'block'},
            {'protocol': ['bittorrent'], 'outboundTag': 'block'},
            {'port': '25,465,587', 'outboundTag': 'block'},
        ]},
    }


async def used_ports(r) -> set[int]:
    out = (await r.run('ss -H -ltun')).out
    ports = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and ':' in f[4] and f[4].rsplit(':', 1)[1].isdigit():
            ports.add(int(f[4].rsplit(':', 1)[1]))
    return ports


async def current_port(r):
    """Порт уже работающего нашего прокси (чтобы повторный /add его не менял)."""
    up = (await r.run("docker inspect -f '{{.State.Running}}' " + NAME)).out.strip() == 'true'
    cfgtxt = await r.run(f"cat {DIR}/config.json")
    if up and cfgtxt.ok:
        try:
            return int(json.loads(cfgtxt.out)['inbounds'][0]['port'])
        except Exception:
            return None
    return None


async def pick_port(r, cfg) -> int:
    used = await used_ports(r)
    for p in range(cfg.port_lo, cfg.port_hi + 1):
        if p not in used:
            return p
    raise RuntimeError(f'Нет свободных портов в диапазоне {cfg.port_lo}-{cfg.port_hi}')


async def deploy(r, cfg, domain: str, say) -> int:
    port = await current_port(r) or await pick_port(r, cfg)
    await r.put(f'{DIR}/config.json', json.dumps(xray_config(cfg, domain, port), indent=2))
    pull = await r.run(f'docker pull {cfg.proxy_image}', timeout=300)
    if not pull.ok:
        raise RuntimeError('docker pull не удался: ' + pull.err[-300:])
    await r.run(f'docker rm -f {NAME}')
    run = await r.run(
        f"docker run -d --name {NAME} --restart always --network host --user 0:0 "
        f"-v {cfg.certbot_base}/certs:/etc/letsencrypt:ro -v {DIR}/config.json:/etc/xray/config.json:ro "
        f"{cfg.proxy_image} run -c /etc/xray/config.json", timeout=120)
    if not run.ok:
        raise RuntimeError('docker run не удался: ' + run.err[-300:])
    for _ in range(10):
        await asyncio.sleep(1)
        if f':{port}' in (await r.run(f"ss -H -ltn 'sport = :{port}'")).out:
            break
    else:
        logs = (await r.run(f'docker logs --tail 15 {NAME} 2>&1')).out
        raise RuntimeError(f'Прокси не начал слушать порт {port}:\n{logs}')
    if (await r.run('ufw status')).out.startswith('Status: active'):
        await r.run(f'ufw allow {port}/tcp')
        await say(f'🔓 ufw: открыт {port}/tcp')
    return port


async def restart(r) -> None:
    await r.run(f'docker restart {NAME}', timeout=60)


async def remove(r) -> None:
    await r.run(f'docker rm -f {NAME}')
    await r.run(f'rm -rf {DIR}')


async def external_check(cfg, domain: str, port: int, tries: int = 3):
    """Проверка «снаружи», с машины бота: валидный сертификат, логин, выход в интернет."""
    err = ''
    for i in range(tries):
        try:
            t = time.monotonic()
            async with httpx.AsyncClient(proxy=f'https://{cfg.proxy_user}:{cfg.proxy_pass}@{domain}:{port}',
                                         timeout=20) as c:
                resp = await c.get('https://api.ipify.org')
            if resp.status_code == 200:
                return True, f'{resp.text.strip()} ({int((time.monotonic() - t) * 1000)} мс)'
            err = f'HTTP {resp.status_code}'
        except Exception as e:  # noqa: BLE001
            err = f'{type(e).__name__}: {e}'
        await asyncio.sleep(3)
    return False, err
