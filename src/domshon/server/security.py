"""Passwords, signed tokens, API tokens and login rate limiting (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException


@dataclass
class Principal:
    role: str  # "admin" | "user"
    name: str


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def ct_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---- passwords -------------------------------------------------------------
ITERS = 310_000


def hash_password(pw: str, iters: int = ITERS) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${b64e(salt)}${b64e(dk)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), b64d(salt), int(iters))
        return hmac.compare_digest(calc, b64d(dk))
    except Exception:
        return False


# ---- signed (session / ticket) tokens --------------------------------------
def sign(secret: str, payload: dict) -> str:
    body = b64e(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{b64e(mac)}"


def unsign(secret: str, token: str) -> dict | None:
    try:
        body, mac = token.split(".")
        good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(good, b64d(mac)):
            return None
        data = json.loads(b64d(body))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


# ---- long-lived API tokens for devices -------------------------------------
class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def create(self, name: str) -> tuple[str, str]:
        tid, sec = secrets.token_hex(4), secrets.token_urlsafe(32)
        with self.lock:
            data = self._load()
            data[tid] = {"name": name, "hash": hashlib.sha256(sec.encode()).hexdigest(),
                         "created": int(time.time())}
            self._save(data)
        return tid, f"dst_{tid}_{sec}"

    def verify(self, token: str) -> dict | None:
        try:
            _, tid, sec = token.split("_", 2)
        except ValueError:
            return None
        rec = self._load().get(tid)
        if rec and hmac.compare_digest(rec["hash"], hashlib.sha256(sec.encode()).hexdigest()):
            return rec
        return None

    def list(self) -> list[dict]:
        return [{"id": k, "name": v["name"], "created": v["created"]} for k, v in self._load().items()]

    def revoke(self, tid: str) -> bool:
        with self.lock:
            data = self._load()
            if tid not in data:
                return False
            del data[tid]
            self._save(data)
            return True


class RateLimiter:
    def __init__(self, max_fail: int = 5, lock_seconds: int = 60):
        self.max_fail, self.lock_seconds = max_fail, lock_seconds
        self.rec: dict[str, tuple[int, float]] = {}

    def check(self, key: str):
        n, until = self.rec.get(key, (0, 0.0))
        if n >= self.max_fail and time.time() < until:
            raise HTTPException(429, "Too many failed attempts. Try again later.")

    def fail(self, key: str):
        if len(self.rec) > 10000:
            self.rec.clear()
        n, until = self.rec.get(key, (0, 0.0))
        n += 1
        if n >= self.max_fail:
            until = time.time() + self.lock_seconds
        self.rec[key] = (n, until)

    def ok(self, key: str):
        self.rec.pop(key, None)
