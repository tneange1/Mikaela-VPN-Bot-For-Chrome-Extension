from __future__ import annotations
import json
from pathlib import Path


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.d = json.loads(path.read_text()) if path.exists() else {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=2))

    def get(self, domain: str):
        return self.d.get(domain)

    def set(self, domain: str, rec: dict) -> None:
        self.d[domain] = rec
        self.save()

    def delete(self, domain: str) -> None:
        self.d.pop(domain, None)
        self.save()
