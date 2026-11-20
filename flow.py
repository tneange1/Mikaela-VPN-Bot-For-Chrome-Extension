from __future__ import annotations
import asyncio
import re
import ssl
import time

import asyncssh
import certs
import github
import keys
import ports
import proxy
from registry import Registry
from remote import Remote

DOMAIN_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$')
_locks: dict[str, asyncio.Lock] = {}


class FlowError(Exception):
    pass


def busy() -> bool:
    return any(l.locked() for l in _locks.values())


def lock(domain: str) -> asyncio.Lock:
    return _locks.setdefault(domain, asyncio.Lock())


async def tls_days_left(domain: str, port: int, timeout: int = 10) -> int:
    """Проверка с машины бота: сертификат валиден для домена, сколько дней осталось."""
    rd, wr = await asyncio.wait_for(asyncio.open_connection(
        domain, port, ssl=ssl.create_default_context(), server_hostname=domain), timeout)
    try:
        cert = wr.get_extra_info('ssl_object').getpeercert()
    finally:
        wr.close()
    return int((ssl.cert_time_to_seconds(cert['notAfter']) - time.time()) // 86400)


def entry(cfg, domain: str, rec: dict) -> dict:
    label = domain.split('.')[0]
    return {'id': label if label != 'www' else domain, 'name': rec['name'], 'code': rec['code'], 'scheme': 'https',
            'host': domain, 'port': rec['port'], 'username': cfg.proxy_user, 'password': cfg.proxy_pass}


async def publish(cfg, reg: Registry, say, domain: str | None = None) -> None:
    """Записать сервер (или все) из реестра бота в servers.json на GitHub."""
    items = [domain] if domain else list(reg.d)
    if not items or not all(reg.get(d) for d in items):
        raise FlowError('Нет серверов для записи')
    await say('📤 GitHub: ' + await github.update_servers(cfg, add_many=[entry(cfg, d, reg.get(d)) for d in items]))


def _ssh(cfg, domain, rec):
    return dict(host=rec['ssh_host'], port=rec['ssh_port'], user=rec['ssh_user'], key=rec.get('ssh_key') or cfg.ssh_key,
                known_hosts=cfg.known_hosts, passphrase=cfg.ssh_pass)


async def provision(cfg, reg: Registry, domain: str, opts: dict, say, confirm=None) -> None:
    old = reg.get(domain) or {}
    rec = {
        'ssh_host': opts.get('ssh_host') or old.get('ssh_host') or domain,
        'ssh_port': int(opts.get('ssh_port') or old.get('ssh_port') or 22),
        'ssh_user': opts.get('user') or old.get('ssh_user') or cfg.ssh_user,
    }
    label = domain.split('.')[0]
    rec['code'] = (opts.get('code') or old.get('code') or re.sub('[^A-Za-z]', '', label)[:2] or 'XX').upper()
    rec['name'] = opts.get('name') or old.get('name') or rec['code']
    pw = opts.get('password')
    if pw:
        rec['ssh_key'] = None
    elif opts.get('key_path'):
        rec['ssh_key'] = opts['key_path']
    else:
        rec['ssh_key'] = old.get('ssh_key')
    try:
        conn = await Remote.open(trust_new=True, password=pw, **_ssh(cfg, domain, rec))
    except (asyncssh.Error, OSError, PermissionError) as e:
        raise FlowError(f'SSH не подключился: {e}\nПроверьте IP/порт и пароль или ключ. Для входа по паролю на сервере '
                        f'должно быть разрешено PermitRootLogin yes.')
    async with conn as r:
        if r.fingerprint:
            await say(f'🔑 Новый сервер, запомнил отпечаток ключа {r.fingerprint}')
        await say('✅ SSH подключён')
        if pw:
            await r.install_pubkey(keys.pub_of(cfg.ssh_key))
            await say('🔑 Установил на сервер ключ бота. Пароль нигде не сохранён, дальше вход только по ключу')
        if not (await r.run('command -v docker')).ok:
            raise FlowError('На сервере не установлен docker')
        await say('🔐 ' + await certs.ensure(r, cfg, domain, say, confirm=confirm))
        port = await proxy.deploy(r, cfg, domain, say)
        await say(f'✅ Прокси запущен на порту {port}')
    ok, info = await proxy.external_check(cfg, domain, port)
    if not ok:
        raise FlowError(f'Проверка снаружи не прошла: {info}\nЧаще всего порт {port}/tcp закрыт в файрволе хостера '
                        f'или DNS домена указывает не на этот сервер. В GitHub ничего не записано.')
    await say(f'✅ Проверка снаружи: выход через {info}')
    rec['port'] = port
    reg.set(domain, rec)
    try:
        await say('📤 GitHub: ' + await github.update_servers(cfg, add=entry(cfg, domain, rec)))
    except Exception as e:  # noqa: BLE001
        await say(f'⚠️ Сервер работает, но записать в GitHub не вышло: {e}\n'
                  f'Исправьте токен и нажмите «📤 Записать в GitHub» в карточке сервера: заново добавлять не нужно.')


async def renew(cfg, reg: Registry, domain: str, say, force: bool = False, confirm=None) -> None:
    rec = reg.get(domain)
    if not rec:
        raise FlowError(f'{domain} не найден (/list)')
    async with await Remote.open(**_ssh(cfg, domain, rec)) as r:
        await say('🔐 ' + await certs.ensure(r, cfg, domain, say, force=force, confirm=confirm))
        await proxy.restart(r)
    await say(f'✅ {domain}: прокси перезапущен с новым сертификатом')


async def restore(cfg, reg: Registry, domain: str) -> list[str]:
    rec = reg.get(domain)
    if not rec:
        raise FlowError(f'{domain} не найден (/list)')
    async with await Remote.open(**_ssh(cfg, domain, rec)) as r:
        return await ports.restore_from_state(r) or ['ничего возвращать не нужно']


async def remove(cfg, reg: Registry, domain: str, say) -> None:
    rec = reg.get(domain)
    if not rec:
        raise FlowError(f'{domain} не найден (/list)')
    async with await Remote.open(**_ssh(cfg, domain, rec)) as r:
        await proxy.remove(r)
    await say('🗑 Прокси удалён с сервера (сертификат оставлен)')
    reg.delete(domain)
    try:
        await say('📤 GitHub: ' + await github.update_servers(cfg, remove_host=domain))
    except Exception as e:  # noqa: BLE001
        await say(f'⚠️ Из GitHub не удалилось: {e}')


async def recover_all(cfg, reg: Registry) -> list[str]:
    """После падения/перезапуска бота: вернуть то, что осталось остановленным на :80."""
    out = []
    for d in list(reg.d):
        try:
            res = await restore(cfg, reg, d)
            if res and res != ['ничего возвращать не нужно']:
                out.append(f'{d}: {"; ".join(res)}')
        except Exception:  # noqa: BLE001
            pass
    return out
