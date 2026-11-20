from __future__ import annotations
import os
from pathlib import Path

import asyncssh


def ensure_bot_key(cfg) -> Path:
    """Ключ бота: создаётся при первом запуске."""
    p = Path(cfg.ssh_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        save(p, asyncssh.generate_private_key('ssh-ed25519', comment='mikaela-bot'))
    return p


def save(path: Path, key) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        key.write_private_key(str(path))
    finally:
        os.umask(old)
    path.chmod(0o600)


def pub_of(path) -> str:
    return asyncssh.read_private_key(str(path)).export_public_key('openssh').decode().strip()


def import_key(text: str):
    """Приватный ключ из текста. Ключи с парольной фразой не поддерживаются."""
    text = text.strip()
    if text.startswith('ssh-') or 'PUBLIC KEY' in text[:40]:
        raise ValueError('Это публичный ключ. Он не нужен: бот сам вычисляет его из приватного. Пришлите приватный.')
    try:
        return asyncssh.import_private_key(text + '\n')
    except asyncssh.KeyImportError as e:
        raise ValueError('Не удалось прочитать приватный ключ (ключи с парольной фразой не поддерживаются; '
                         f'снять её: ssh-keygen -p -f ключ). {e}')
