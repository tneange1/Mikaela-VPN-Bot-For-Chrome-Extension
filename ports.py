"""Освобождение порта 80 на время выпуска сертификата и возврат всего как было.

Останавливаем только то, что можно гарантированно вернуть: docker-контейнеры и
systemd-юниты. Если порт держит что-то ещё — ничего не трогаем и сообщаем."""
from __future__ import annotations
import asyncio
import json
import re
from dataclasses import dataclass, asdict

STATE = '/var/lib/mikaela-bot/paused.json'
PROTECTED = ('docker.service', 'containerd.service', 'ssh.service', 'sshd.service')


class PauseError(Exception):
    pass


@dataclass(frozen=True)
class Held:
    kind: str   # docker | unit
    name: str


def parse_ss_pids(text: str) -> list[int]:
    return sorted({int(x) for x in re.findall(r'pid=(\d+)', text)})


def parse_cgroup(text: str):
    for line in text.splitlines():
        path = line.split(':', 2)[-1]
        m = re.search(r'docker-([0-9a-f]{64})\.scope', path) or re.search(r'/docker/([0-9a-f]{64})', path)
        if m:
            return 'docker', m.group(1)
        if '/user.slice/' in path:
            return None, None
        units = re.findall(r'/([^/]+\.service)', path)
        if units:
            return 'unit', units[-1]
    return None, None


def is_protected(unit: str) -> bool:
    return unit in PROTECTED or unit.startswith(('systemd-', 'user@', 'getty', 'session-'))


async def discover(r, port: int) -> list[Held]:
    held: list[Held] = []

    def add(h: Held):
        if h not in held:
            held.append(h)

    if (await r.run('command -v docker')).ok:
        res = await r.run(f"docker ps --filter publish={port} --format '{{{{.Names}}}}'")
        for n in res.out.split():
            add(Held('docker', n))
    res = await r.run(f"ss -H -ltnp 'sport = :{port}'")
    for pid in parse_ss_pids(res.out):
        comm = (await r.run(f'cat /proc/{pid}/comm')).out.strip()
        if comm == 'docker-proxy':
            continue  # его контейнер уже найден через docker ps
        kind, name = parse_cgroup((await r.run(f'cat /proc/{pid}/cgroup')).out)
        if kind == 'docker':
            n = (await r.run("docker inspect -f '{{.Name}}' " + name)).out.strip().lstrip('/')
            if not n:
                raise PauseError(f'Не удалось определить контейнер для pid {pid}')
            add(Held('docker', n))
        elif kind == 'unit' and not is_protected(name):
            add(Held('unit', name))
        else:
            raise PauseError(
                f'Порт {port} слушает «{comm}» (pid {pid}), который нельзя безопасно остановить '
                f'и вернуть ({name or "не docker и не systemd"}). Ничего не тронул.')
    return held


async def _is_up(r, h: Held) -> bool:
    if h.kind == 'docker':
        return (await r.run("docker inspect -f '{{.State.Running}}' " + h.name)).out.strip() == 'true'
    return (await r.run(f'systemctl is-active {h.name}')).out.strip() == 'active'


async def _start_all(r, held: list[Held]) -> list[str]:
    """Запускает в обратном порядке, проверяет. Возвращает список проблем."""
    problems = []
    for h in reversed(held):
        for attempt in range(3):
            cmd = f'docker start {h.name}' if h.kind == 'docker' else f'systemctl start {h.name}'
            await r.run(cmd, timeout=60)
            if await _is_up(r, h):
                break
            await asyncio.sleep(2)
        else:
            problems.append(f'{h.kind} {h.name} не запустился')
    return problems


class Paused:
    """async with Paused(r, 80): ... — порт свободен внутри, всё возвращается на выходе (даже при ошибке)."""

    def __init__(self, r, port: int, confirm=None):
        self.r, self.port, self.confirm = r, port, confirm
        self.held: list[Held] = []
        self.touched: list[Held] = []
        self.problems: list[str] = []

    async def __aenter__(self) -> 'Paused':
        try:
            self.held = await discover(self.r, self.port)
            if self.held and self.confirm and not await self.confirm(self.held):
                self.held = []
                raise PauseError('Отменено: для сертификата нужен свободный порт 80, ничего не останавливал')
            if self.held:
                await self.r.put(STATE, json.dumps({'port': self.port, 'held': [asdict(h) for h in self.held]}))
                for h in self.held:
                    self.touched.append(h)
                    cmd = f'docker stop -t 20 {h.name}' if h.kind == 'docker' else f'systemctl stop {h.name}'
                    res = await self.r.run(cmd, timeout=90)
                    if not res.ok:
                        raise PauseError(f'Не удалось остановить {h.kind} {h.name}: {res.err}')
                for _ in range(15):
                    if not (await self.r.run(f"ss -H -ltn 'sport = :{self.port}'")).out.strip():
                        break
                    await asyncio.sleep(1)
                else:
                    raise PauseError(f'Порт {self.port} остался занят после остановки процессов')
        except BaseException:
            await self._restore()
            raise
        return self

    async def __aexit__(self, *a) -> bool:
        await self._restore()
        return False

    async def _restore(self) -> None:
        if not self.touched:
            return
        self.problems = await _start_all(self.r, self.touched)
        if not self.problems:
            await self.r.run(f"rm -f '{STATE}'")
        self.touched = []


async def restore_from_state(r) -> list[str]:
    """Аварийный возврат (команда /restore), если бот упал посреди выпуска."""
    res = await r.run(f"cat '{STATE}'")
    if not res.ok or not res.out:
        return []
    held = [Held(**h) for h in json.loads(res.out)['held']]
    problems = await _start_all(r, held)
    if not problems:
        await r.run(f"rm -f '{STATE}'")
    return problems or [f'возвращено: {", ".join(h.name for h in held)}']
