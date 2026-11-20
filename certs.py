from __future__ import annotations
import re
from ports import Paused


class CertError(Exception):
    pass


async def days_left(r, cfg, domain: str):
    f = f'{cfg.certbot_base}/certs/live/{domain}/fullchain.pem'
    res = await r.run(f"openssl x509 -enddate -noout -in '{f}'")
    if not res.ok:
        return None
    m = re.search(r'notAfter=(.+)', res.out)
    if not m:
        return None
    d = await r.run(f"echo $(( ($(date -d '{m.group(1)}' +%s) - $(date +%s)) / 86400 ))")
    return int(d.out) if d.ok and d.out.lstrip('-').isdigit() else None


def _certbot_cmd(cfg, domain: str, docker: bool, force: bool) -> str:
    b = cfg.certbot_base
    args = (f"certonly --standalone --preferred-challenges http --http-01-port {cfg.http_port} "
            f"-d {domain} --cert-name {domain} -m {cfg.email} --agree-tos --no-eff-email --non-interactive "
            + ('--force-renewal' if force else '--keep-until-expiring'))
    if docker:
        return (f"docker run --rm --network host -v {b}/certs:/etc/letsencrypt "
                f"-v {b}/var-lib-letsencrypt:/var/lib/letsencrypt -v {b}/logs:/var/log/letsencrypt "
                f"certbot/certbot {args}")
    return f"certbot {args} --config-dir {b}/certs --work-dir {b}/var-lib-letsencrypt --logs-dir {b}/logs"


async def ensure(r, cfg, domain: str, say, force: bool = False) -> str:
    """Выпускает/продлевает сертификат. Порт 80 освобождается и всегда возвращается."""
    left = await days_left(r, cfg, domain)
    if left is not None and left > cfg.renew_days and not force:
        return f'сертификат уже есть, действителен ещё {left} дн.'
    docker = (await r.run('command -v docker')).ok
    if not docker and not (await r.run('command -v certbot')).ok:
        raise CertError('На сервере нет ни docker, ни certbot')
    await r.run(f"mkdir -p {cfg.certbot_base}/certs {cfg.certbot_base}/var-lib-letsencrypt {cfg.certbot_base}/logs")
    paused = Paused(r, cfg.http_port)
    try:
        async with paused:
            if paused.held:
                await say('⏸ Остановил на время выпуска: ' + ', '.join(h.name for h in paused.held))
            res = await r.run(_certbot_cmd(cfg, domain, docker, force), timeout=300)
    finally:
        if paused.held:
            if paused.problems:
                await say('⚠️ НЕ ВЕРНУЛОСЬ: ' + '; '.join(paused.problems) + ' — выполните /restore ' + domain)
            else:
                await say('▶️ Вернул обратно: ' + ', '.join(h.name for h in reversed(paused.held)))
    if not res.ok:
        tail = '\n'.join((res.out + '\n' + res.err).strip().splitlines()[-8:])
        raise CertError('certbot не смог выпустить сертификат (домен должен указывать на этот сервер, порт 80 открыт):\n' + tail)
    left = await days_left(r, cfg, domain)
    return f'сертификат выпущен, действителен {left} дн.'
