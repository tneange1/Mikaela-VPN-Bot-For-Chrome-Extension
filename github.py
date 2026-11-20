from __future__ import annotations
import base64
import json

import httpx


def _dump(data: dict) -> str:
    rows = ',\n'.join('    ' + json.dumps(s, ensure_ascii=False) for s in data['servers'])
    return f'{{\n  "version": {data.get("version", 1)},\n  "servers": [\n{rows}\n  ]\n}}\n'


HINTS = {401: 'токен GitHub неверный, обрезан или истёк — выпустите новый и обновите GITHUB_TOKEN в .env',
         403: 'у токена нет права Contents: Read and write на этот репозиторий',
         404: 'токен не видит репозиторий или файл chrome/servers.json — проверьте доступ токена к репозиторию'}


async def update_servers(cfg, add: dict | None = None, remove_host: str | None = None,
                         add_many: list[dict] | None = None) -> str:
    """Добавляет/заменяет/удаляет записи в chrome/servers.json репозитория."""
    try:
        return await _update(cfg, add, remove_host, add_many)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        raise RuntimeError(f'GitHub {code}: ' + HINTS.get(code, e.response.text[:200])) from None


async def _update(cfg, add, remove_host, add_many) -> str:
    url = f'https://api.github.com/repos/{cfg.gh_repo}/contents/{cfg.gh_path}'
    h = {'Authorization': f'Bearer {cfg.gh_token}', 'Accept': 'application/vnd.github+json'}
    async with httpx.AsyncClient(headers=h, timeout=30) as c:
        r = await c.get(url, params={'ref': cfg.gh_branch})
        r.raise_for_status()
        j = r.json()
        data = json.loads(base64.b64decode(j['content']))
        adds = ([add] if add else []) + list(add_many or [])
        hosts = {x for x in [remove_host] + [a['host'] for a in adds] if x}
        ids = {a['id'] for a in adds}
        servers = [s for s in data.get('servers', [])
                   if 'REPLACE' not in json.dumps(s) and s.get('host') not in hosts and s.get('id') not in ids]
        servers += adds
        data['servers'] = servers
        what = ('добавлено: ' + ', '.join(a['host'] for a in adds)) if adds else f'удалён {remove_host}'
        body = {'message': f'chrome: servers.json — {what}',
                'content': base64.b64encode(_dump(data).encode()).decode(), 'sha': j['sha'], 'branch': cfg.gh_branch}
        p = await c.put(url, json=body)
        p.raise_for_status()
        return p.json()['commit']['html_url']
