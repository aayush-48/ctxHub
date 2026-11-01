"""Local CLI configuration: server URL, tokens and the default repo."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SERVER = os.environ.get("CTX_SERVER", "http://localhost:8080")


def config_path() -> Path:
    if override := os.environ.get("CTX_CONFIG"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ctx" / "config.json"


@dataclass
class Config:
    server: str = DEFAULT_SERVER
    client_id: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    repo: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        p = config_path()
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)

    def save(self) -> None:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)  # tokens inside
        tmp.replace(p)
