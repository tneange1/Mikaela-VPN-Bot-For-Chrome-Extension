from __future__ import annotations
import asyncio
import logging
import shlex

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import config
import flow
from registry import Registry

cfg = config.load()
reg = Registry(cfg.registry_file)
dp = Dispatcher()
dp.message.filter(F.from_user.id.in_(cfg.admins))   # всем остальным бот молчит

HELP = (
    'Mikaela Chrome-прокси бот\n\n'
    '/add домен [ssh=ip[:порт]] [user=root] [code=FI] [name=Финляндия]\n'
    '   — сертификат + прокси + запись в GitHub\n'
    '/renew домен|all [force] — продлить сертификат\n'
    '/restore домен — аварийно вернуть остановленные на :80 процессы\n'
    '/remove домен — убрать прокси и запись из GitHub\n'
    '/list — серверы\n'
    '/status — проверка всех серверов\n\n'
    'Если домен указывает на сервер, ssh= не нужен.'
)


class Progress:
    def __init__(self, msg: Message):
        self.msg, self.lines = msg, []

    async def __call__(self, line: str) -> None:
        self.lines.append(line)
        try:
            await self.msg.edit_text('\n'.join(self.lines)[-3800:], disable_web_page_preview=True)
        except TelegramBadRequest:
            pass


def parse(args: str | None):
    toks = shlex.split(args or '')
    pos = [t for t in toks if '=' not in t]
    kv = dict(t.split('=', 1) for t in toks if '=' in t)
    return pos, kv


async def guarded(m: Message, domain: str, work) -> None:
    prog = Progress(await m.answer(f'⏳ {domain} …'))
    async with flow.lock(domain):
        try:
            await work(prog)
            await prog('🎉 Готово')
        except Exception as e:  # noqa: BLE001
            logging.exception('flow failed')
            await prog(f'❌ {e}')


def need_domain(pos: list[str]):
    d = (pos[0] if pos else '').lower()
    return d if flow.DOMAIN_RE.match(d) else None


@dp.message(Command('start', 'help'))
async def _help(m: Message):
    await m.answer(HELP)


@dp.message(Command('add'))
async def _add(m: Message, command: CommandObject):
    pos, kv = parse(command.args)
    d = need_domain(pos)
    if not d:
        return await m.answer('Формат: /add fi.example.com [ssh=1.2.3.4:22] [code=FI] [name=Финляндия]')
    ssh = kv.get('ssh', '')
    opts = {'ssh_host': ssh.split(':')[0] or None, 'ssh_port': ssh.split(':')[1] if ':' in ssh else None,
            'user': kv.get('user'), 'code': kv.get('code'), 'name': kv.get('name', '').replace('_', ' ') or None}
    await guarded(m, d, lambda p: flow.provision(cfg, reg, d, opts, p))


@dp.message(Command('renew'))
async def _renew(m: Message, command: CommandObject):
    pos, kv = parse(command.args)
    force = 'force' in pos
    pos = [x for x in pos if x != 'force']
    targets = list(reg.d) if pos[:1] == ['all'] else ([need_domain(pos)] if need_domain(pos) else [])
    if not targets:
        return await m.answer('Формат: /renew fi.example.com [force]  или  /renew all')
    for d in targets:
        await guarded(m, d, lambda p, d=d: flow.renew(cfg, reg, d, p, force))


@dp.message(Command('restore'))
async def _restore(m: Message, command: CommandObject):
    d = need_domain(parse(command.args)[0])
    if not d:
        return await m.answer('Формат: /restore fi.example.com')
    prog = Progress(await m.answer('⏳ …'))
    try:
        await prog('▶️ ' + '; '.join(await flow.restore(cfg, reg, d)))
    except Exception as e:  # noqa: BLE001
        await prog(f'❌ {e}')


@dp.message(Command('remove'))
async def _remove(m: Message, command: CommandObject):
    d = need_domain(parse(command.args)[0])
    if not d:
        return await m.answer('Формат: /remove fi.example.com')
    await guarded(m, d, lambda p: flow.remove(cfg, reg, d, p))


@dp.message(Command('list'))
async def _list(m: Message):
    if not reg.d:
        return await m.answer('Серверов пока нет. /add')
    await m.answer('\n'.join(f'{r["code"]} {d}:{r["port"]} (ssh {r["ssh_user"]}@{r["ssh_host"]}:{r["ssh_port"]})'
                             for d, r in reg.d.items()))


@dp.message(Command('status'))
async def _status(m: Message):
    async def one(d, r):
        try:
            return f'✅ {d}:{r["port"]} — сертификат ещё {await flow.tls_days_left(d, r["port"])} дн.'
        except Exception as e:  # noqa: BLE001
            return f'❌ {d}:{r["port"]} — {type(e).__name__}: {e}'
    if not reg.d:
        return await m.answer('Серверов пока нет.')
    await m.answer('\n'.join(await asyncio.gather(*(one(d, r) for d, r in reg.d.items()))))


async def renew_loop(bot: Bot) -> None:
    """Раз в 12 часов: если сертификат кончается — продлить сам и сообщить."""
    await asyncio.sleep(60)
    while True:
        for d, r in list(reg.d.items()):
            try:
                left = await flow.tls_days_left(d, r['port'])
            except Exception as e:  # noqa: BLE001
                for a in cfg.admins:
                    await bot.send_message(a, f'❌ {d}:{r["port"]} недоступен: {type(e).__name__}: {e}')
                continue
            if left > cfg.renew_days:
                continue
            lines: list[str] = []

            async def say(x: str):
                lines.append(x)
            async with flow.lock(d):
                try:
                    await flow.renew(cfg, reg, d, say)
                    out = f'🔁 {d}: сертификат продлён\n' + '\n'.join(lines)
                except Exception as e:  # noqa: BLE001
                    out = f'❌ Автопродление {d} не удалось: {e}\n' + '\n'.join(lines)
            for a in cfg.admins:
                await bot.send_message(a, out)
        await asyncio.sleep(12 * 3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(cfg.tg_token)
    asyncio.create_task(renew_loop(bot))
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
