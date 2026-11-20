from __future__ import annotations
import asyncio
import hashlib
import html
import logging
import os
import re
import secrets
import shutil
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as KB,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import config
import flow
import keys
import updater
from registry import Registry
from settings import Settings

cfg = config.load()
keys.ensure_bot_key(cfg)
reg = Registry(cfg.registry_file)
st = Settings(cfg.data_dir / 'settings.json')
dp = Dispatcher()
dp.message.filter(F.from_user.id.in_(cfg.admins))          # всем остальным бот молчит
dp.callback_query.filter(F.from_user.id.in_(cfg.admins))
bot_ref: list[Bot] = []
PENDING: dict[str, asyncio.Future] = {}

BTN_ADD, BTN_LIST, BTN_STATUS, BTN_KEY, BTN_UPD = '➕ Добавить сервер', '📋 Серверы', '🩺 Статус', '🔑 SSH-ключ', '⚙️ Обновление'
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_LIST)],
                                     [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_KEY)],
                                     [KeyboardButton(text=BTN_UPD)]], resize_keyboard=True, is_persistent=True)
CANCEL = KB(inline_keyboard=[[B(text='❌ Отмена', callback_data='cancel')]])


class W(StatesGroup):
    domain = State()
    host = State()
    auth = State()
    secret = State()
    name = State()
    setkey = State()


# ---------- утилиты ----------
class Progress:
    def __init__(self, msg: Message):
        self.msg, self.lines = msg, []

    async def __call__(self, line: str) -> None:
        self.lines.append(line)
        try:
            await self.msg.edit_text('\n'.join(self.lines)[-3800:], disable_web_page_preview=True)
        except TelegramBadRequest:
            pass


def rid(domain: str) -> str:
    return hashlib.sha1(domain.encode()).hexdigest()[:10]


def by_rid(r: str):
    return next((d for d in reg.d if rid(d) == r), None)


def need_domain(t: str):
    d = (t or '').strip().lower()
    return d if flow.DOMAIN_RE.match(d) else None


async def edit(cb: CallbackQuery, text: str, kb: KB | None = None, **kw) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, **kw)
    except TelegramBadRequest:
        pass


def make_confirm(chat_ids, domain: str, timeout: int):
    """Перед остановкой чего-либо на :80 спрашивает разрешение кнопкой."""
    async def confirm(held) -> bool:
        token = secrets.token_hex(4)
        fut = asyncio.get_running_loop().create_future()
        PENDING[token] = fut
        text = (f'⚠️ {domain}: для выпуска сертификата нужен порт 80. На время (около 30 секунд) остановлю:\n• '
                + '\n• '.join(f'{h.name} ({"контейнер" if h.kind == "docker" else "служба"})' for h in held)
                + '\n\nПосле выпуска всё запущу обратно автоматически.')
        kb = KB(inline_keyboard=[[B(text='⏸ Остановить и продолжить', callback_data=f'pause:{token}:y')],
                                 [B(text='❌ Не надо', callback_data=f'pause:{token}:n')]])
        for c in chat_ids:
            await bot_ref[0].send_message(c, text, reply_markup=kb)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            PENDING.pop(token, None)
    return confirm


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
    """Текст или файл; сообщение с секретом сразу удаляется из чата."""
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


# ---------- главное меню ----------
@dp.message(Command('start', 'menu'))
async def _start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('Выберите действие кнопками внизу.', reply_markup=MENU)


@dp.message(F.text == BTN_ADD)
async def _b_add(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(W.domain)
    await m.answer('Домен сервера, например fi.mikaelavpn.xyz (A-запись должна вести на сервер).', reply_markup=CANCEL)


def servers_kb() -> KB:
    rows = [[B(text=f'{r["code"]} · {d}:{r["port"]}', callback_data=f'srv:{rid(d)}')] for d, r in reg.d.items()]
    rows.append([B(text='📤 Записать все в GitHub', callback_data='puball')] if reg.d else
                [B(text='Серверов нет', callback_data='noop')])
    return KB(inline_keyboard=rows)


@dp.message(F.text == BTN_LIST)
async def _b_list(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('Серверы (нажмите для действий):' if reg.d else 'Серверов пока нет. Нажмите «➕ Добавить сервер».',
                   reply_markup=servers_kb())


@dp.message(F.text == BTN_STATUS)
async def _b_status(m: Message, state: FSMContext):
    await state.clear()
    if not reg.d:
        return await m.answer('Серверов пока нет.')

    async def one(d, r):
        try:
            return f'✅ {d}:{r["port"]} — сертификат ещё {await flow.tls_days_left(d, r["port"])} дн.'
        except Exception as e:  # noqa: BLE001
            return f'❌ {d}:{r["port"]} — {type(e).__name__}: {e}'
    await m.answer('\n'.join(await asyncio.gather(*(one(d, r) for d, r in reg.d.items()))))


def key_kb() -> KB:
    return KB(inline_keyboard=[[B(text='📎 Загрузить свой приватный ключ', callback_data='setkey')]])


@dp.message(F.text == BTN_KEY)
async def _b_key(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('Публичный ключ бота. Он нужен, только если не хотите давать пароль: добавьте его в '
                   '~/.ssh/authorized_keys на сервере.\n\n<code>' + html.escape(keys.pub_of(cfg.ssh_key)) + '</code>',
                   parse_mode='HTML', reply_markup=key_kb())


async def upd_card() -> tuple[str, KB]:
    auto = st.get('auto_update', True)
    text = (f'Версия: {await updater.version()}\n'
            f'Автообновление: {"включено" if auto else "выключено"}'
            + ('' if updater.IN_DOCKER else '\n⚠️ Запущен не в Docker: перезапуск для обновления недоступен.'))
    kb = KB(inline_keyboard=[[B(text='🔍 Проверить обновления', callback_data='upd:check')],
                             [B(text=('🔕 Выключить автообновление' if auto else '🔔 Включить автообновление'),
                                callback_data='upd:auto')]])
    return text, kb


@dp.message(F.text == BTN_UPD)
async def _b_upd(m: Message, state: FSMContext):
    await state.clear()
    t, kb = await upd_card()
    await m.answer(t, reply_markup=kb)


# ---------- кнопки под сообщениями ----------
@dp.callback_query(F.data == 'noop')
async def _noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == 'cancel')
async def _cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await edit(cb, 'Отменено.')


@dp.callback_query(F.data == 'srvs')
async def _srvs(cb: CallbackQuery):
    await cb.answer()
    await edit(cb, 'Серверы (нажмите для действий):' if reg.d else 'Серверов нет.', servers_kb())


@dp.callback_query(F.data.startswith('srv:'))
async def _srv(cb: CallbackQuery):
    await cb.answer()
    d = by_rid(cb.data[4:])
    if not d:
        return await edit(cb, 'Сервер не найден.', servers_kb())
    r = reg.get(d)
    try:
        cert = f'сертификат ещё {await asyncio.wait_for(flow.tls_days_left(d, r["port"]), 8)} дн.'
    except Exception as e:  # noqa: BLE001
        cert = f'недоступен ({type(e).__name__})'
    kb = KB(inline_keyboard=[
        [B(text='🔐 Продлить сертификат', callback_data=f'act:renew:{rid(d)}'),
         B(text='♻️ Перевыпустить', callback_data=f'act:force:{rid(d)}')],
        [B(text='📤 Записать в GitHub', callback_data=f'act:pub:{rid(d)}'),
         B(text='🚀 BBR (скорость)', callback_data=f'act:bbr:{rid(d)}')],
        [B(text='▶️ Вернуть остановленное на :80', callback_data=f'act:restore:{rid(d)}')],
        [B(text='🗑 Удалить сервер', callback_data=f'act:rm:{rid(d)}')],
        [B(text='⬅️ Назад', callback_data='srvs')]])
    await edit(cb, f'{r["code"]} · {r["name"]}\n{d}:{r["port"]} — {cert}\n'
                   f'SSH: {r["ssh_user"]}@{r["ssh_host"]}:{r["ssh_port"]}{" (свой ключ)" if r.get("ssh_key") else ""}', kb)


@dp.callback_query(F.data.startswith('act:'))
async def _act(cb: CallbackQuery):
    _, what, r = cb.data.split(':')
    d = by_rid(r)
    await cb.answer()
    if not d:
        return await edit(cb, 'Сервер не найден.', servers_kb())
    chats = [cb.message.chat.id]
    if what == 'rm':
        kb = KB(inline_keyboard=[[B(text='🗑 Да, удалить', callback_data=f'act:rmyes:{r}')],
                                 [B(text='⬅️ Нет', callback_data=f'srv:{r}')]])
        return await edit(cb, f'Удалить {d}? Контейнер прокси на сервере будет убран, запись пропадёт из GitHub '
                              f'(сертификат останется).', kb)
    if what == 'bbr':
        kb = KB(inline_keyboard=[[B(text='🚀 Включить BBR', callback_data=f'act:bbryes:{r}')],
                                 [B(text='⬅️ Нет', callback_data=f'srv:{r}')]])
        return await edit(cb, f'Включить на {d} алгоритм TCP BBR? Он ускоряет скачивание на длинных каналах. Это '
                              f'настройка ядра всего сервера (sysctl), действует на все его соединения и сохраняется '
                              f'после перезагрузки. Прокси и другие сервисы не перезапускаются.', kb)
    cf = make_confirm(chats, d, 600)
    works = {
        'renew': lambda p: flow.renew(cfg, reg, d, p, False, cf),
        'force': lambda p: flow.renew(cfg, reg, d, p, True, cf),
        'rmyes': lambda p: flow.remove(cfg, reg, d, p),
        'pub': lambda p: flow.publish(cfg, reg, p, d),
        'bbryes': lambda p: flow.speedup(cfg, reg, d, p),
    }
    if what == 'restore':
        prog = Progress(await cb.message.answer('⏳ …'))
        try:
            return await prog('▶️ ' + '; '.join(await flow.restore(cfg, reg, d)))
        except Exception as e:  # noqa: BLE001
            return await prog(f'❌ {e}')
    await guarded(cb.message, d, works[what])


@dp.callback_query(F.data == 'puball')
async def _puball(cb: CallbackQuery):
    await cb.answer()
    prog = Progress(await cb.message.answer('⏳ GitHub …'))
    try:
        await flow.publish(cfg, reg, prog)
        await prog('🎉 Готово')
    except Exception as e:  # noqa: BLE001
        await prog(f'❌ {e}')


@dp.callback_query(F.data.startswith('pause:'))
async def _pause(cb: CallbackQuery):
    _, token, ans = cb.data.split(':')
    fut = PENDING.get(token)
    await cb.answer()
    if fut and not fut.done():
        fut.set_result(ans == 'y')
    await edit(cb, cb.message.text + ('\n\n✅ Разрешено' if ans == 'y' else '\n\n🚫 Отклонено'))


@dp.callback_query(F.data == 'setkey')
async def _setkey_btn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(W.setkey)
    await cb.message.answer('Пришлите приватный SSH-ключ текстом или файлом (сообщение сразу удалю). Он заменит ключ '
                            'бота для серверов без собственного. Публичный присылать не нужно, он вычисляется сам.',
                            reply_markup=CANCEL)


@dp.callback_query(F.data == 'upd:auto')
async def _upd_auto(cb: CallbackQuery):
    await cb.answer()
    st.set('auto_update', not st.get('auto_update', True))
    t, kb = await upd_card()
    await edit(cb, t, kb)


@dp.callback_query(F.data == 'upd:check')
async def _upd_check(cb: CallbackQuery):
    await cb.answer('Проверяю…')
    try:
        c = await updater.check()
    except Exception as e:  # noqa: BLE001
        return await edit(cb, f'❌ Не удалось проверить: {e}', (await upd_card())[1])
    if c['new']:
        kb = KB(inline_keyboard=[[B(text='⬆️ Обновить и перезапустить', callback_data='upd:apply')]])
        return await edit(cb, f'Есть обновление: {c["subject"]}', kb)
    t, kb = await upd_card()
    note = '\n⚠️ Последний коммит не запустился и пропущен.' if c['blocked'] else '\n✅ Это последняя версия.'
    await edit(cb, t + note, kb)


@dp.callback_query(F.data == 'upd:apply')
async def _upd_apply(cb: CallbackQuery):
    await cb.answer()
    if not updater.IN_DOCKER:
        return await edit(cb, 'Запущен не в Docker, перезапуск недоступен.')
    if flow.busy():
        return await edit(cb, 'Сейчас идёт операция с сервером. Подождите её окончания и нажмите ещё раз.',
                          KB(inline_keyboard=[[B(text='⬆️ Обновить', callback_data='upd:apply')]]))
    await edit(cb, '⬆️ Перезапускаюсь для обновления… Через минуту напишу, что получилось.')
    await asyncio.sleep(1)
    updater.restart_for_update()


# ---------- мастер добавления ----------
@dp.message(W.domain)
async def _w_domain(m: Message, state: FSMContext):
    d = need_domain(m.text)
    if not d:
        return await m.answer('Не похоже на домен. Пример: fi.mikaelavpn.xyz', reply_markup=CANCEL)
    await state.update_data(domain=d)
    await state.set_state(W.host)
    await m.answer('IP сервера для SSH (можно с портом: 1.2.3.4:2222).\nЕсли домен уже указывает на этот сервер, '
                   'отправьте «-».', reply_markup=CANCEL)


@dp.message(W.host)
async def _w_host(m: Message, state: FSMContext):
    t = (m.text or '').strip()
    host = port = None
    if t != '-':
        mm = re.fullmatch(r'([A-Za-z0-9.\-]+)(?::(\d{1,5}))?', t)
        if not mm:
            return await m.answer('Нужен IP или хост, можно с портом: 1.2.3.4:2222. Или «-».', reply_markup=CANCEL)
        host, port = mm.group(1), mm.group(2)
    await state.update_data(ssh_host=host, ssh_port=port)
    await state.set_state(W.auth)
    await m.answer('Как заходить на сервер?', reply_markup=KB(inline_keyboard=[
        [B(text='🔑 Пароль root', callback_data='auth:pw')],
        [B(text='📎 Загрузить свой SSH-ключ', callback_data='auth:key')],
        [B(text='🤖 Ключ бота (уже добавлен на сервер)', callback_data='auth:bot')],
        [B(text='❌ Отмена', callback_data='cancel')]]))


NAME_Q = 'Название и код страны для списка в расширении, например: Финляндия FI'
NAME_KB = KB(inline_keyboard=[[B(text='Пропустить (код возьму из домена)', callback_data='skipname')],
                              [B(text='❌ Отмена', callback_data='cancel')]])


@dp.callback_query(W.auth, F.data.startswith('auth:'))
async def _w_auth(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(':')[1]
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(auth=kind)
    if kind == 'bot':
        await state.set_state(W.name)
        return await cb.message.answer('Публичный ключ бота — в «🔑 SSH-ключ». Он должен лежать в authorized_keys на '
                                       'сервере.\n\n' + NAME_Q, reply_markup=NAME_KB)
    await state.set_state(W.secret)
    await cb.message.answer(
        'Пришлите пароль root. Сообщение сразу удалю, пароль нигде не сохраняется: бот один раз войдёт и поставит '
        'на сервер свой ключ.' if kind == 'pw' else
        'Пришлите приватный SSH-ключ текстом или файлом (сообщение сразу удалю). Ключ с парольной фразой не подойдёт.',
        reply_markup=CANCEL)


@dp.message(W.secret)
async def _w_secret(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    txt = await secret_text(m, bot)
    if not txt:
        return await m.answer('Пусто или файл слишком большой. Пришлите ещё раз.', reply_markup=CANCEL)
    if data['auth'] == 'pw':
        await state.update_data(password=txt)
    else:
        try:
            key = keys.import_key(txt)
        except ValueError as e:
            return await m.answer(f'{e}\nПришлите ещё раз.', reply_markup=CANCEL)
        path = cfg.data_dir / 'keys' / data['domain']
        keys.save(path, key)
        await state.update_data(key_path=str(path))
    await state.set_state(W.name)
    await m.answer('Принял, сообщение удалил.\n\n' + NAME_Q, reply_markup=NAME_KB)


async def run_add(m: Message, state: FSMContext, name: str | None, code: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    opts = {'ssh_host': data.get('ssh_host'), 'ssh_port': data.get('ssh_port'), 'password': data.get('password'),
            'key_path': data.get('key_path'), 'code': code, 'name': name}
    d = data['domain']
    cf = make_confirm([m.chat.id], d, 600)
    await guarded(m, d, lambda p: flow.provision(cfg, reg, d, opts, p, cf))


@dp.message(W.name)
async def _w_name(m: Message, state: FSMContext):
    toks = (m.text or '').split()
    code = toks.pop().upper() if toks and re.fullmatch(r'[A-Za-z]{2}', toks[-1]) else None
    await run_add(m, state, ' '.join(toks) or None, code)


@dp.callback_query(W.name, F.data == 'skipname')
async def _w_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await run_add(cb.message, state, None, None)


@dp.message(W.setkey)
async def _w_setkey(m: Message, state: FSMContext, bot: Bot):
    txt = await secret_text(m, bot)
    try:
        key = keys.import_key(txt or '')
    except ValueError as e:
        return await m.answer(f'{e}\nПришлите ещё раз.', reply_markup=CANCEL)
    try:
        shutil.copy(cfg.ssh_key, cfg.ssh_key + '.bak')
    except OSError:
        pass
    keys.save(Path(cfg.ssh_key), key)
    await state.clear()
    await m.answer('Ключ заменён (прежний сохранён как .bak). Публичная часть:\n<code>' +
                   html.escape(keys.pub_of(cfg.ssh_key)) + '</code>\nСерверы со старым ключом бота его больше не '
                   'примут: добавьте им этот.', parse_mode='HTML')


# ---------- фоновые задачи ----------
async def notify(text: str, **kw) -> None:
    for a in cfg.admins:
        try:
            await bot_ref[0].send_message(a, text, **kw)
        except Exception:  # noqa: BLE001
            logging.exception('notify failed')


async def announce() -> None:
    """После старта: сообщить об обновлении/откате и вернуть то, что могло остаться остановленным."""
    notice = cfg.data_dir / 'notice.txt'
    if notice.exists():
        await notify(notice.read_text())
        notice.unlink()
    last, cur = cfg.data_dir / 'last_version', await updater.version()
    if not last.exists() or last.read_text() != cur:
        if last.exists():
            await notify(f'✅ Бот обновлён: {cur}')
        last.write_text(cur)
    left = await flow.recover_all(cfg, reg)
    if left:
        await notify('▶️ После перезапуска вернул остановленное: ' + ' | '.join(left))


async def update_loop() -> None:
    notified = ''
    await asyncio.sleep(120)
    while True:
        try:
            c = await updater.check()
            if c['new'] and updater.IN_DOCKER:
                if st.get('auto_update', True):
                    if not flow.busy():
                        await notify(f'⬆️ Нашёл обновление ({c["subject"]}), перезапускаюсь.')
                        await asyncio.sleep(2)
                        updater.restart_for_update()
                elif c['remote'] != notified:
                    notified = c['remote']
                    await notify(f'Вышло обновление: {c["subject"]}',
                                 reply_markup=KB(inline_keyboard=[[B(text='⬆️ Обновить', callback_data='upd:apply')]]))
        except Exception:  # noqa: BLE001
            logging.exception('update check failed')
        await asyncio.sleep(600)


async def renew_loop() -> None:
    """Раз в 12 часов: продлевает сертификаты, которым осталось меньше RENEW_DAYS."""
    await asyncio.sleep(90)
    while True:
        for d, r in list(reg.d.items()):
            try:
                left = await flow.tls_days_left(d, r['port'])
            except Exception as e:  # noqa: BLE001
                await notify(f'❌ {d}:{r["port"]} недоступен: {type(e).__name__}: {e}')
                continue
            if left > cfg.renew_days:
                continue
            lines: list[str] = []

            async def say(x: str):
                lines.append(x)
            cf = None if cfg.auto_pause else make_confirm(list(cfg.admins), d, 6 * 3600)
            async with flow.lock(d):
                try:
                    await flow.renew(cfg, reg, d, say, False, cf)
                    out = f'🔁 {d}: сертификат продлён\n' + '\n'.join(lines)
                except Exception as e:  # noqa: BLE001
                    out = f'❌ Автопродление {d} не удалось: {e}\n' + '\n'.join(lines)
            await notify(out)
        await asyncio.sleep(12 * 3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    api = os.environ.get('TG_API_BASE')          # для тестов с поддельным Telegram
    bot = Bot(cfg.tg_token, session=AiohttpSession(api=TelegramAPIServer.from_base(api)) if api else None)
    bot_ref.append(bot)
    for coro in (announce(), update_loop(), renew_loop()):
        asyncio.create_task(coro)
    while True:                              # старт может упасть из-за сети сразу после перезапуска контейнера
        try:
            await dp.start_polling(bot)      # возвращается по SIGTERM (docker stop)
            break
        except Exception:  # noqa: BLE001
            logging.exception('не удалось запустить polling, повтор через 10 с')
            await asyncio.sleep(10)
    for _ in range(55):                      # дать закончить начатую операцию с сервером
        if not flow.busy():
            break
        await asyncio.sleep(1)


if __name__ == '__main__':
    asyncio.run(main())
