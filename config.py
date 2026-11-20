from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _req(name: str) -> str:
    v = os.environ.get(name, '').strip()
    if not v:
        raise SystemExit(f'Не задана переменная {name} (см. .env.example)')
    return v


@dataclass(frozen=True)
class Cfg:
    tg_token: str
    admins: frozenset
    ssh_key: str
    ssh_pass: str | None
    ssh_user: str
    gh_token: str
    gh_repo: str
    gh_path: str
    gh_branch: str
    proxy_user: str
    proxy_pass: str
    email: str
    certbot_base: str
    proxy_image: str
    port_lo: int
    port_hi: int
    http_port: int
    renew_days: int
    data_dir: Path

    @property
    def known_hosts(self) -> Path:
        return self.data_dir / 'known_hosts'

    @property
    def registry_file(self) -> Path:
        return self.data_dir / 'registry.json'


def load(env_file: str = '.env') -> Cfg:
    _load_env(Path(env_file))
    g = os.environ.get
    lo, hi = (g('PORT_RANGE', '9443-9499').split('-') + ['9499'])[:2]
    data = Path(g('DATA_DIR', 'data'))
    data.mkdir(parents=True, exist_ok=True)
    return Cfg(
        tg_token=_req('TG_TOKEN'),
        admins=frozenset(int(x) for x in _req('ADMIN_IDS').replace(' ', '').split(',') if x),
        ssh_key=g('SSH_KEY') or str(data / 'ssh' / 'id_ed25519'),
        ssh_pass=g('SSH_KEY_PASSPHRASE') or None,
        ssh_user=g('SSH_USER', 'root'),
        gh_token=_req('GITHUB_TOKEN'),
        gh_repo=g('GITHUB_REPO', 'tneange1/Mikaela-VPN-Client'),
        gh_path=g('GITHUB_PATH', 'chrome/servers.json'),
        gh_branch=g('GITHUB_BRANCH', 'main'),
        proxy_user=g('PROXY_USER', 'free'),
        proxy_pass=_req('PROXY_PASS'),
        email=_req('LE_EMAIL'),
        certbot_base=g('CERTBOT_BASE', '/opt/certbot').rstrip('/'),
        proxy_image=g('PROXY_IMAGE', 'ghcr.io/xtls/xray-core:latest'),
        port_lo=int(lo), port_hi=int(hi),
        http_port=int(g('HTTP_PORT', '80')),
        renew_days=int(g('RENEW_DAYS', '30')),
        data_dir=data,
    )
