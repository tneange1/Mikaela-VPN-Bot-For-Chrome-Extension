from __future__ import annotations
import asyncio
import html
import logging
import re
import shlex
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import flow
import keys
from registry import Registry

cfg = config.load()
keys.ensure_bot_key(cfg)
reg = Registry(cfg.registry_file)
dp = Dispatcher()
dp.message.filter(F.from_user.id.in_(cfg.admins))          # всем остальным бот молчит
dp.callback_query.filter(F.from_user.id.in_(cfg.admins))

HELP = (
    'Бот Chrome-прокси Mikaela\n\n'
    '/add — добавить сервер (спрошу домен, IP, пароль или ключ, название)\n'
    '/renew домен|all [force] — продлить сертификат\n'
    '/restore домен — аварийно вернуть остановленное на :80\n'
    '/remove домен — убрать прокси и запись из GitHub\n'
    '/list — серверы, /status — проверка\n'
    '/key — публичный SSH-ключ бота\n'
    '/setkey — загрузить свой приватный SSH-ключ вместо ключа бота\n'
    '/cancel — отменить ввод'
)


class W(StatesGroup):
    domain = State()
    host = State()
    auth = State()
    secret = State()
    name = State()
    setkey = State()


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
    return [t for t in toks if '=' not in t], dict(t.split('=', 1) for t in toks if '=' in t)


def need_domain(pos: list[str]):
    d = (pos[0] if pos else '').strip().lower()
    return d if flow.DOMAIN_RE.match(d) else None


async def guarded(m: Message, domain: str, work) -> None:
    prog = Progress(await m.answer(f'⏳ {domain} …'))
    async with flow.lock(domain):
        try:
            await work(prog)
            await prog('🎉 Готово')
        except Exception as e:  # noqa: BLE001
            logging.exception('flow failed')
            await prog(f'❌ {e}')


async def secret_text(m: Message, bot: Bot) -> str | None:
    """Текст или файл от пользователя; сообщение с секретом сразу удаляется из чата."""
    txt = m.text
    if m.document:
        if (m.document.file_size or 0) > 20000:
            return None
        buf = BytesIO()
        await bot.download(m.document, destination=buf)
        txt = buf.getvalue().decode('utf-8', 'ignore')
    try:
        await m.delete()
    except TelegramBadRequest:
        pass
    return (txt or '').strip() or None


# ---------- обычные команды (регистрируются раньше шагов мастера) ----------
@dp.message(Command('start', 'help'))
async def _help(m: Message):
    await m.answer(HELP)


@dp.message(Command('cancel'))
async def _cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('Отменено.')


@dp.message(Command('key'))
async def _key(m: Message):
    await m.answer('Публичный ключ бота. Нужен, только если не хотите давать пароль: добавьте его в '
                   '~/.ssh/authorized_keys на сервере.\n\n<code>' + html.escape(keys.pub_of(cfg.ssh_key)) + '</code>',
                   parse_mode='HTML')


@dp.message(Command('setkey'))
async def _setkey(m: Message, state: FSMContext):
    await state.set_state(W.setkey)
    await m.answer('Пришлите приватный SSH-ключ текстом или файлом (сообщение сразу удалю). Он заменит ключ бота '
                   'для всех серверов, где не задан свой. Публичный ключ присылать не нужно, он вычисляется сам.\n'
                   'Отмена: /cancel')


@dp.message(Command('add'))
async def _add(m: Message, command: CommandObject, state: FSMContext):
    pos, kv = parse(command.args)
    d = need_domain(pos)
    if not command.args:
        await state.set_state(W.domain)
        return await m.answer('Домен сервера, например fi.mikaelavpn.xyz (A-запись должна вести на сервер).\nОтмена: /cancel')
    if not d:
        return await m.answer('Формат: /add fi.example.com [ssh=1.2.3.4:22] [code=FI] [name=Финляндия] — или просто /add')
    ssh = kv.get('ssh', '')
    opts = {'ssh_host': ssh.split(':')[0] or None, 'ssh_port': ssh.split(':')[1] if ':' in ssh else None,
            'user': kv.get('user'), 'code': kv.get('code'), 'name': kv.get('name', '').replace('_', ' ') or None}
    await guarded(m, d, lambda p: flow.provision(cfg, reg, d, opts, p))


@dp.message(Command('renew'))
async def _renew(m: Message, command: CommandObject):
    pos, _ = parse(command.args)
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
    await m.answer('\n'.join(f'{r["code"]} {d}:{r["port"]}  (ssh {r["ssh_user"]}@{r["ssh_host"]}:{r["ssh_port"]}'
                             f'{", свой ключ" if r.get("ssh_key") else ""})' for d, r in reg.d.items()))


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


# ---------- мастер /add ----------
AUTH_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='🔑 Пароль root', callback_data='auth:pw')],
    [InlineKeyboardButton(text='📎 Загрузить свой SSH-ключ', callback_data='auth:key')],
    [InlineKeyboardButton(text='🤖 Ключ бота (уже добавлен на сервер)', callback_data='auth:bot')],
])
NAME_Q = ('Название и код страны для списка в расширении, например: Финляндия FI\n'
          '(или «-» — возьму код из домена)')


@dp.message(W.domain)
async def _w_domain(m: Message, state: FSMContext):
    d = need_domain([m.text or ''])
    if not d:
        return await m.answer('Не похоже на домен. Пример: fi.mikaelavpn.xyz')
    await state.update_data(domain=d)
    await state.set_state(W.host)
    await m.answer('IP сервера для SSH (можно с портом: 1.2.3.4:2222).\nЕсли домен уже указывает на этот сервер — отправьте «-».')


@dp.message(W.host)
async def _w_host(m: Message, state: FSMContext):
    t = (m.text or '').strip()
    host, port = None, None
    if t != '-':
        mm = re.fullmatch(r'([A-Za-z0-9.\-]+)(?::(\d{1,5}))?', t)
        if not mm:
            return await m.answer('Нужен IP или хост, можно с портом: 1.2.3.4:2222. Или «-».')
        host, port = mm.group(1), mm.group(2)
    await state.update_data(ssh_host=host, ssh_port=port)
    await state.set_state(W.auth)
    await m.answer('Как заходить на сервер?', reply_markup=AUTH_KB)


@dp.callback_query(W.auth, F.data.startswith('auth:'))
async def _w_auth(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(':')[1]
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(auth=kind)
    if kind == 'bot':
        await state.set_state(W.name)
        return await cb.message.answer('Публичный ключ бота — /key. Он должен быть в authorized_keys на сервере.\n\n' + NAME_Q)
    await state.set_state(W.secret)
    await cb.message.answer(
        'Пришлите пароль root. Сообщение сразу удалю; пароль нигде не сохраняется: бот один раз войдёт и '
        'поставит на сервер свой ключ.' if kind == 'pw' else
        'Пришлите приватный SSH-ключ текстом или файлом (сообщение сразу удалю). Ключ с парольной фразой не подойдёт.')


@dp.message(W.secret)
async def _w_secret(m: Message, state: FSMContext, bot: Bot):
    st = await state.get_data()
    txt = await secret_text(m, bot)
    if not txt:
        return await m.answer('Пусто или файл слишком большой. Пришлите ещё раз (или /cancel).')
    if st['auth'] == 'pw':
        await state.update_data(password=txt)
    else:
        try:
            key = keys.import_key(txt)
        except ValueError as e:
            return await m.answer(f'{e}\nПришлите ещё раз (или /cancel).')
        path = cfg.data_dir / 'keys' / st['domain']
        keys.save(path, key)
        await state.update_data(key_path=str(path))
    await state.set_state(W.name)
    await m.answer('Принял, сообщение удалил.\n\n' + NAME_Q)


@dp.message(W.name)
async def _w_name(m: Message, state: FSMContext):
    st = await state.get_data()
    await state.clear()
    toks = (m.text or '').split()
    code = toks.pop().upper() if toks and re.fullmatch(r'[A-Za-z]{2}', toks[-1]) else None
    name = ' '.join(t for t in toks if t != '-') or None
    opts = {'ssh_host': st.get('ssh_host'), 'ssh_port': st.get('ssh_port'), 'password': st.get('password'),
            'key_path': st.get('key_path'), 'code': code, 'name': name}
    d = st['domain']
    await guarded(m, d, lambda p: flow.provision(cfg, reg, d, opts, p))


@dp.message(W.setkey)
async def _w_setkey(m: Message, state: FSMContext, bot: Bot):
    txt = await secret_text(m, bot)
    try:
        key = keys.import_key(txt or '')
    except ValueError as e:
        return await m.answer(f'{e}\nПришлите ещё раз (или /cancel).')
    old = cfg.ssh_key
    try:
        import shutil
        shutil.copy(old, old + '.bak')
    except OSError:
        pass
    keys.save(__import__('pathlib').Path(old), key)
    await state.clear()
    await m.answer('Ключ заменён (старый сохранён как .bak). Публичная часть:\n<code>' +
                   html.escape(keys.pub_of(old)) + '</code>\nСерверы, где был прежний ключ бота, теперь его не примут — '
                   'добавьте им этот или верните старый.', parse_mode='HTML')


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
