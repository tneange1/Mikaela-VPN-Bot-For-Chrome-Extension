"""Дымовой тест кнопок бота на поддельной сессии Telegram (без сети)."""
import asyncio, os, sys, tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(TG_TOKEN='1:abc', ADMIN_IDS='1', GITHUB_TOKEN='t', PROXY_PASS='p', LE_EMAIL='a@b.c',
                  DATA_DIR=tempfile.mkdtemp())

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import bot as B


class Fake(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self): pass
    def stream_content(self, *a, **k): raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        n = type(method).__name__
        self.sent.append((n, getattr(method, 'text', None), getattr(method, 'reply_markup', None)))
        if n in ('SendMessage', 'EditMessageText'):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=1, type='private'),
                           text=getattr(method, 'text', '')).as_(bot)
        return True


USER, CHAT = User(id=1, is_bot=False, first_name='a'), Chat(id=1, type='private')


def msg(text, i=[0]):
    i[0] += 1
    return Update(update_id=i[0], message=Message(message_id=i[0], date=datetime.now(), chat=CHAT, from_user=USER, text=text))


def cbq(data, i=[1000]):
    i[0] += 1
    m = Message(message_id=i[0], date=datetime.now(), chat=CHAT, text='x', from_user=User(id=99, is_bot=True, first_name='b'))
    return Update(update_id=i[0], callback_query=CallbackQuery(id=str(i[0]), from_user=USER, chat_instance='c', message=m, data=data))


def test_wizard_and_menu():
    fake = Fake()
    bot = Bot('1:abc', session=fake)
    B.bot_ref.append(bot)
    calls = []

    async def fake_provision(cfg, reg, domain, opts, say, confirm=None):
        calls.append((domain, opts))
        await say('ok')
    B.flow.provision = fake_provision

    async def go():
        for u in (msg('/start'), msg(B.BTN_ADD), msg('плохой домен'), msg('fi.test.xyz'), msg('1.2.3.4:2222'),
                  cbq('auth:pw'), msg('s3cret'), msg('Финляндия FI')):
            await B.dp.feed_update(bot, u)
        await B.dp.feed_update(bot, msg(B.BTN_LIST))
        await B.dp.feed_update(bot, msg(B.BTN_KEY))
        await B.dp.feed_update(bot, msg(B.BTN_UPD))
    asyncio.run(go())
    assert calls and calls[0][0] == 'fi.test.xyz', fake.sent
    o = calls[0][1]
    assert (o['ssh_host'], o['ssh_port'], o['password'], o['code'], o['name']) == ('1.2.3.4', '2222', 's3cret', 'FI', 'Финляндия')
    texts = ' | '.join(str(t) for _, t, _ in fake.sent)
    assert 'Не похоже на домен' in texts and 'Как заходить' in texts and 'ssh-ed25519' in texts and 'Версия' in texts


def test_stranger_ignored():
    fake = Fake(); bot = Bot('1:abc', session=fake)
    u = Update(update_id=5000, message=Message(message_id=1, date=datetime.now(), chat=CHAT,
                                               from_user=User(id=777, is_bot=False, first_name='x'), text=B.BTN_LIST))
    asyncio.run(B.dp.feed_update(bot, u))
    assert not fake.sent


if __name__ == '__main__':
    test_wizard_and_menu(); test_stranger_ignored(); print('ok ui')
