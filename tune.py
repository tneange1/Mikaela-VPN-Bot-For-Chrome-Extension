"""Ускорение скачивания на сервере: BBR вместо cubic. Меняет настройки ядра сервера (sysctl), поэтому только по кнопке."""
from __future__ import annotations

CONF = '/etc/sysctl.d/99-mikaela-bbr.conf'


async def _sysctl(r, key: str) -> str:
    return (await r.run(f'sysctl -n {key}')).out.strip()


async def enable_bbr(r, say) -> str:
    cur, qd = await _sysctl(r, 'net.ipv4.tcp_congestion_control'), await _sysctl(r, 'net.core.default_qdisc')
    await say(f'Сейчас: congestion={cur or "?"}, qdisc={qd or "?"}')
    if cur == 'bbr':
        return 'BBR уже включён, менять нечего'
    avail = (await r.run('cat /proc/sys/net/ipv4/tcp_available_congestion_control')).out.split()
    if 'bbr' not in avail:
        await r.run('modprobe tcp_bbr')
        avail = (await r.run('cat /proc/sys/net/ipv4/tcp_available_congestion_control')).out.split()
    if 'bbr' not in avail:
        raise RuntimeError('В ядре сервера нет BBR (модуль tcp_bbr не загрузился). Ничего не менял.')
    await r.put(CONF, 'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n', mode='644')
    await r.run('mkdir -p /etc/modules-load.d && echo tcp_bbr > /etc/modules-load.d/mikaela-bbr.conf')
    res = await r.run(f'sysctl -p {CONF}')
    new = await _sysctl(r, 'net.ipv4.tcp_congestion_control')
    if new != 'bbr':
        await r.run(f"rm -f {CONF}")
        raise RuntimeError(f'Не удалось включить BBR ({res.err or res.out}). Вернул как было.')
    await say(f'Теперь: congestion={new}, qdisc={await _sysctl(r, "net.core.default_qdisc")}')
    return (f'BBR включён (было {cur}). Новые соединения пойдут уже с ним, текущие переподключатся сами. '
            f'Откатить: удалить {CONF} и перезагрузить сервер.')
