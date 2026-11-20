"""Проверка обновлений в git. Само применение делает entrypoint.sh: бот выходит с кодом 42."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path

APP_DIR = os.environ.get('APP_DIR', '.')
DATA_DIR = Path(os.environ.get('DATA_DIR', 'data'))
BRANCH = os.environ.get('BRANCH', 'main')
IN_DOCKER = bool(os.environ.get('MIKAELA_DOCKER'))


async def _git(*args: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec('git', *args, cwd=APP_DIR, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 90)
    return p.returncode, out.decode().strip()


async def version() -> str:
    rc, out = await _git('log', '-1', '--format=%h %s')
    return out if rc == 0 else 'неизвестно'


async def check() -> dict:
    rc, out = await _git('fetch', '-q', '--depth', '1', 'origin', BRANCH)
    if rc:
        raise RuntimeError('git fetch: ' + out[-200:])
    local = (await _git('rev-parse', 'HEAD'))[1]
    remote = (await _git('rev-parse', 'FETCH_HEAD'))[1]
    blocked = (DATA_DIR / 'blocked_commit').read_text().strip() if (DATA_DIR / 'blocked_commit').exists() else ''
    subject = (await _git('log', '-1', '--format=%h %s', 'FETCH_HEAD'))[1]
    return {'local': local, 'remote': remote, 'subject': subject,
            'new': remote != local and remote != blocked, 'blocked': remote == blocked and remote != local}


def restart_for_update() -> None:
    os._exit(42)
