"""Server settings with LIVE reload.

Precedence (low -> high): defaults < process env (DS_*) < .env file < settings.json
The .env file and settings.json are polled by mtime (at most once per second), so
admin changes take effect without restarting the server.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

from dotenv import dotenv_values
from platformdirs import user_config_dir, user_data_dir

from . import APP_NAME

# name: (default, type)
SPECS: dict[str, tuple] = {
    "host": ("0.0.0.0", str),
    "port": (8787, int),
    "trust_proxy": (False, bool),
    "domain": ("", str),
    "storage_root": ("", str),
    "mounts": ({}, dict),
    "admin_user": ("admin", str),
    "admin_password": ("", str),
    "admin_password_hash": ("", str),
    "secret_key": ("", str),
    "max_upload_mb": (0, int),
    "session_hours": (12, int),
    "read_only": (False, bool),
    "lockdown": (False, bool),
    "quota_mb": (0, int),                 # cap for the main storage folder, 0 = unlimited
    "min_free_mb": (0, int),              # keep this much disk free, 0 = off
    "blocked_extensions": ("", str),      # e.g. ".exe,.php"
    "user_writes": (True, bool),          # may device users change files? (False = admin only)
    # second-factor delivery (see README: Two-factor login)
    "sms_twilio_sid": ("", str), "sms_twilio_token": ("", str), "sms_twilio_from": ("", str),
    "sms_webhook_url": ("", str), "sms_webhook_token": ("", str),
    "smtp_host": ("", str), "smtp_port": (587, int), "smtp_user": ("", str), "smtp_password": ("", str),
    "smtp_from": ("", str), "smtp_security": ("starttls", str),   # starttls | ssl | none
}
SECRET_KEYS = {"sms_twilio_token", "sms_webhook_token", "smtp_password"}
# Changeable at runtime through the admin API/GUI/CLI
EDITABLE = {"domain", "storage_root", "mounts", "admin_user", "max_upload_mb",
            "session_hours", "read_only", "lockdown", "quota_mb", "min_free_mb",
            "blocked_extensions", "user_writes", "sms_twilio_sid", "sms_twilio_token", "sms_twilio_from",
            "sms_webhook_url", "sms_webhook_token", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
            "smtp_from", "smtp_security"}
RESTART_REQUIRED = ["host", "port", "trust_proxy", "secret_key"]
MOUNT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _service_config_dir() -> Path | None:
    """Config folder of an installed OS service (so `sudo domshon server passwd` hits the real server)."""
    if sys.platform.startswith("win"):
        cand = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Domshon" / "config"
    elif sys.platform == "darwin":
        return None
    else:
        cand = Path("/var/lib/domshon/config")
    try:
        if cand.is_dir():
            with tempfile.TemporaryFile(dir=cand):
                pass
            return cand
    except OSError:
        pass
    return None


def server_config_dir() -> Path:
    p = os.environ.get("DS_CONFIG_DIR")
    if p:
        path = Path(p).expanduser()
    else:
        path = _service_config_dir() or Path(user_config_dir(APP_NAME)) / "server"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_storage_root() -> Path:
    return Path(user_data_dir(APP_NAME)) / "files"


def _coerce(kind, raw):
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(raw)
    if kind is dict:
        if isinstance(raw, dict):
            return raw
        s = str(raw).strip()
        if not s:
            return {}
        if s.startswith("{"):
            return json.loads(s)
        out = {}
        for part in s.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out
    return "" if raw is None else str(raw)


class Settings:
    def __init__(self):
        self.dir = server_config_dir()
        self.override_path = self.dir / "settings.json"
        self.env_file = self._pick_env_file()
        self._lock = threading.RLock()
        self._sig = None
        self._last = 0.0
        self._secret_cache: str | None = None
        self.values: dict = {}
        self.reload(force=True)

    @staticmethod
    def _pick_env_file() -> Path:
        """DS_ENV_FILE > ./.env > the installed Linux service's /etc/domshon/domshon.env."""
        if os.environ.get("DS_ENV_FILE"):
            return Path(os.environ["DS_ENV_FILE"])
        local = Path(".env")
        svc = Path("/etc/domshon/domshon.env")
        try:
            if not local.is_file() and svc.is_file() and os.access(svc, os.R_OK):
                return svc
        except OSError:
            pass
        return local

    # ---- loading -------------------------------------------------------
    def _sig_now(self):
        def m(p: Path):
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0
        return (m(self.env_file), m(self.override_path))

    def _read_override(self) -> dict:
        try:
            return json.loads(self.override_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def reload(self, force: bool = False):
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last < 1.0:
                return
            self._last = now
            sig = self._sig_now()
            if not force and sig == self._sig:
                return
            self._sig = sig
            raw = {k[3:].lower(): v for k, v in os.environ.items() if k.startswith("DS_")}
            if self.env_file.is_file():
                raw.update({k[3:].lower() if k.upper().startswith("DS_") else k.lower(): v
                            for k, v in dotenv_values(self.env_file).items() if v is not None})
            raw.update({k: v for k, v in self._read_override().items() if v is not None})
            vals = {}
            for name, (default, kind) in SPECS.items():
                vals[name] = default
                if name in raw and raw[name] != "":
                    try:
                        vals[name] = _coerce(kind, raw[name])
                    except Exception as exc:  # keep running with the default
                        print(f"[settings] bad value for {name}: {exc}", file=sys.stderr)
            self.values = vals

    def get(self, name: str):
        self.reload()
        return self.values[name]

    # ---- derived -------------------------------------------------------
    def storage_root(self) -> Path:
        v = self.get("storage_root")
        p = Path(v).expanduser() if v else default_storage_root()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def roots(self) -> dict[str, Path]:
        roots = {"main": self.storage_root()}
        for name, path in (self.get("mounts") or {}).items():
            if MOUNT_NAME.match(name) and name != "main" and Path(path).is_dir():
                roots[name] = Path(path)
        return roots

    def blocked_exts(self) -> set[str]:
        return set(_parse_exts(self.get("blocked_extensions")))

    def secret(self) -> str:
        s = self.get("secret_key")
        if s:
            return s
        if self._secret_cache:
            return self._secret_cache
        f = self.dir / "secret.key"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(48))
            _chmod600(f)
        self._secret_cache = f.read_text().strip()
        return self._secret_cache

    def rotate_secret(self):
        if self.get("secret_key"):
            raise ValueError("secret_key is fixed via environment; change it there and restart")
        f = self.dir / "secret.key"
        f.write_text(secrets.token_urlsafe(48))
        _chmod600(f)
        self._secret_cache = f.read_text().strip()

    # ---- writing -------------------------------------------------------
    def _write_override(self, data: dict):
        tmp = self.override_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        _chmod600(tmp)
        os.replace(tmp, self.override_path)

    def set_internal(self, name: str, value):
        with self._lock:
            cur = self._read_override()
            cur[name] = value
            self._write_override(cur)
            self.reload(force=True)

    def update(self, changes: dict):
        with self._lock:
            cur = self._read_override()
            for k, v in changes.items():
                if k in SECRET_KEYS and v == "********":      # the masked placeholder, not a new value
                    continue
                if k not in EDITABLE:
                    raise ValueError(f"'{k}' cannot be changed live")
                cur[k] = self._validate(k, v)
            self._write_override(cur)
            self.reload(force=True)

    @staticmethod
    def _validate(k, v):
        kind = SPECS[k][1]
        v = _coerce(kind, v)
        if k == "domain":
            v = re.sub(r"^https?://", "", v.strip()).strip("/")
        elif k == "storage_root":
            if v:
                p = Path(v).expanduser()
                if not p.is_absolute():
                    raise ValueError("storage_root must be an absolute path")
                p.mkdir(parents=True, exist_ok=True)
                v = str(p)
        elif k == "mounts":
            for name, path in v.items():
                if not MOUNT_NAME.match(name) or name == "main":
                    raise ValueError(f"invalid mount name '{name}'")
                if not Path(path).is_absolute() or not Path(path).is_dir():
                    raise ValueError(f"mount '{name}' must be an existing absolute directory")
        elif k == "max_upload_mb" and v < 0:
            raise ValueError("max_upload_mb must be >= 0")
        elif k in ("quota_mb", "min_free_mb") and v < 0:
            raise ValueError(f"{k} must be >= 0")
        elif k == "blocked_extensions":
            v = ",".join(_parse_exts(v))
        elif k == "session_hours" and not 1 <= v <= 720:
            raise ValueError("session_hours must be 1..720")
        elif k == "smtp_security" and v.lower() not in ("starttls", "ssl", "none"):
            raise ValueError("smtp_security must be starttls, ssl or none")
        elif k == "admin_user" and not v.strip():
            raise ValueError("admin_user cannot be empty")
        return v

    def public(self) -> dict:
        self.reload()
        out = {k: ("********" if k in SECRET_KEYS and self.values[k] else self.values[k]) for k in EDITABLE}
        out["storage_root"] = str(self.storage_root())
        out["host"], out["port"] = self.values["host"], self.values["port"]
        out["restart_required"] = RESTART_REQUIRED
        return out


def _parse_exts(raw) -> list[str]:
    """'exe, .PHP;sh' -> ['.exe', '.php', '.sh'] (validated, de-duplicated, order kept)."""
    out: list[str] = []
    for part in re.split(r"[,;\s]+", str(raw or "").lower()):
        part = part.strip()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        if not re.fullmatch(r"\.[a-z0-9_+-]{1,16}", part):
            raise ValueError(f"invalid file extension '{part}'")
        if part not in out:
            out.append(part)
    return out


def _chmod600(p: Path):
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
