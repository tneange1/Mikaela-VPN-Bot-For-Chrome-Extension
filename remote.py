from __future__ import annotations
import asyncssh
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Res:
    rc: int
    out: str
    err: str = ''

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _pat(host: str, port: int) -> str:
    return host if port == 22 else f'[{host}]:{port}'


def _pinned(path: Path, host: str, port: int) -> bool:
    p = _pat(host, port) + ' '
    return path.exists() and any(l.startswith(p) for l in path.read_text().splitlines())


class Remote:
    """SSH-сессия. Ключ хоста запоминается при первом /add и дальше проверяется строго."""

    def __init__(self, conn, fingerprint: str | None = None):
        self.conn = conn
        self.fingerprint = fingerprint

    @classmethod
    async def open(cls, host: str, port: int, user: str, key: str, known_hosts: Path,
                   passphrase: str | None = None, trust_new: bool = False) -> 'Remote':
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        kw = dict(host=host, port=port, username=user, client_keys=[key],
                  passphrase=passphrase, connect_timeout=20)
        if _pinned(known_hosts, host, port):
            return cls(await asyncssh.connect(known_hosts=str(known_hosts), **kw))
        if not trust_new:
            raise PermissionError(f'Хост {host} ещё не добавлен — сначала /add')
        conn = await asyncssh.connect(known_hosts=None, **kw)
        hk = conn.get_server_host_key()
        line = f"{_pat(host, port)} {hk.export_public_key('openssh').decode().strip()}\n"
        with known_hosts.open('a') as f:
            f.write(line)
        return cls(conn, hk.get_fingerprint())

    async def __aenter__(self) -> 'Remote':
        return self

    async def __aexit__(self, *a) -> None:
        self.conn.close()
        await self.conn.wait_closed()

    async def run(self, cmd: str, timeout: int = 120, stdin: str | None = None) -> Res:
        try:
            p = await self.conn.run(cmd, check=False, timeout=timeout, input=stdin)
        except asyncssh.TimeoutError:
            return Res(124, '', 'timeout')
        return Res(p.exit_status if p.exit_status is not None else -1,
                   (p.stdout or '').strip() if isinstance(p.stdout, str) else '',
                   (p.stderr or '').strip() if isinstance(p.stderr, str) else '')

    async def put(self, path: str, text: str, mode: str = '600') -> None:
        d = path.rsplit('/', 1)[0]
        r = await self.run(f"mkdir -p '{d}' && cat > '{path}' && chmod {mode} '{path}'", stdin=text)
        if not r.ok:
            raise RuntimeError(f'Не удалось записать {path}: {r.err}')
