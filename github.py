from __future__ import annotations
import base64
import json

import httpx


def _dump(data: dict) -> str:
    rows = ',\n'.join('    ' + json.dumps(s, ensure_ascii=False) for s in data['servers'])
    return f'{{\n  "version": {data.get("version", 1)},\n  "servers": [\n{rows}\n  ]\n}}\n'


async def update_servers(cfg, add: dict | None = None, remove_host: str | None = None) -> str:
    """Добавляет/заменяет/удаляет запись в chrome/servers.json репозитория."""
    url = f'https://api.github.com/repos/{cfg.gh_repo}/contents/{cfg.gh_path}'
    h = {'Authorization': f'Bearer {cfg.gh_token}', 'Accept': 'application/vnd.github+json'}
    async with httpx.AsyncClient(headers=h, timeout=30) as c:
        r = await c.get(url, params={'ref': cfg.gh_branch})
        r.raise_for_status()
        j = r.json()
        data = json.loads(base64.b64decode(j['content']))
        hosts = {x for x in (remove_host, add and add['host']) if x}
        servers = [s for s in data.get('servers', [])
                   if 'REPLACE' not in json.dumps(s) and s.get('host') not in hosts and not (add and s.get('id') == add['id'])]
        if add:
            servers.append(add)
        data['servers'] = servers
        body = {'message': f'chrome: servers.json — {"добавлен " + add["host"] if add else "удалён " + str(remove_host)}',
                'content': base64.b64encode(_dump(data).encode()).decode(), 'sha': j['sha'], 'branch': cfg.gh_branch}
        p = await c.put(url, json=body)
        p.raise_for_status()
        return p.json()['commit']['html_url']
