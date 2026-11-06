"""Client-side configuration (server address, token, sync profiles)."""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from .. import APP_NAME


def client_config_dir() -> Path:
    p = os.environ.get("DS_CLIENT_DIR")
    path = Path(p).expanduser() if p else Path(user_config_dir(APP_NAME)) / "client"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_server_url() -> str:
    """DS_SERVER env > value baked in at build time > localhost."""
    if os.environ.get("DS_SERVER"):
        return os.environ["DS_SERVER"]
    try:
        data = json.loads((Path(__file__).resolve().parent.parent / "_build_defaults.json").read_text("utf-8"))
        if data.get("server_url"):
            return data["server_url"]
    except (OSError, ValueError):
        pass
    return "http://localhost:8787"


def default_sync_dir() -> Path:
    return Path.home() / "Domshon"


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


@dataclass
class Profile:
    name: str
    local_dir: str
    remote_root: str = "main"
    remote_path: str = ""
    interval: int = 30
    enabled: bool = True


@dataclass
class ClientConfig:
    server_url: str = field(default_factory=default_server_url)
    token: str = ""
    device: str = field(default_factory=socket.gethostname)
    profiles: list = field(default_factory=list)  # list[Profile]
    prefs: dict = field(default_factory=dict)      # desktop preferences (confirm_delete, notify, ...)

    @staticmethod
    def path() -> Path:
        return client_config_dir() / "config.json"

    @classmethod
    def load(cls) -> "ClientConfig":
        cfg = cls()
        try:
            raw = json.loads(cls.path().read_text("utf-8"))
        except (OSError, ValueError):
            return cfg
        cfg.server_url = raw.get("server_url") or cfg.server_url
        cfg.token = raw.get("token", "")
        cfg.device = raw.get("device") or cfg.device
        if isinstance(raw.get("prefs"), dict):
            cfg.prefs = raw["prefs"]
        for p in raw.get("profiles", []):
            try:
                cfg.profiles.append(Profile(**p))
            except TypeError:
                continue
        return cfg

    def save(self):
        data = {"server_url": self.server_url, "token": self.token, "device": self.device,
                "profiles": [asdict(p) for p in self.profiles],
                "prefs": self.prefs}
        tmp = self.path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path())

    def pref(self, key: str, default=None):
        return self.prefs.get(key, default)

    def profile(self, name: str):
        return next((p for p in self.profiles if p.name == name), None)
