from __future__ import annotations
import json
from pathlib import Path


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.d = json.loads(path.read_text()) if path.exists() else {}

    def get(self, k: str, default=None):
        return self.d.get(k, default)

    def set(self, k: str, v) -> None:
        self.d[k] = v
        self.path.write_text(json.dumps(self.d, ensure_ascii=False))
