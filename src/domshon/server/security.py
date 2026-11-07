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
    read_only: bool = False     # read-only device token: may list/download but never change files


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

    def create(self, name: str, read_only: bool = False) -> tuple[str, str]:
        tid, sec = secrets.token_hex(4), secrets.token_urlsafe(32)
        with self.lock:
            data = self._load()
            data[tid] = {"name": name, "hash": hashlib.sha256(sec.encode()).hexdigest(),
                         "created": int(time.time()), "read_only": bool(read_only)}
            self._save(data)
        return tid, f"dst_{tid}_{sec}"

    def verify(self, token: str) -> dict | None:
        try:
            _, tid, sec = token.split("_", 2)
        except ValueError:
            return None
        rec = self._load().get(tid)
        if rec and hmac.compare_digest(rec["hash"], hashlib.sha256(sec.encode()).hexdigest()):
            return {**rec, "id": tid}
        return None

    _seen: dict = {}          # tid -> last time we wrote to disk (throttle)

    def touch(self, tid: str, ip: str = "", agent: str = "", force: bool = False):
        """Record that a device token was just used (writes to disk at most every 20s per token)."""
        now = time.time()
        if not force and now - self._seen.get(tid, 0) < 20:
            return
        with self.lock:
            data = self._load()
            rec = data.get(tid)
            if not rec:
                return
            rec["last_seen"] = int(now)
            if ip:
                rec["last_ip"] = ip[:64]
            if agent:
                rec["agent"] = agent[:160]
            rec["requests"] = int(rec.get("requests", 0)) + 1
            try:
                self._save(data)
            except OSError:
                return
            self._seen[tid] = now

    def list(self) -> list[dict]:
        return [{"id": k, "name": v["name"], "created": v["created"],
                 "last_seen": v.get("last_seen", 0), "last_ip": v.get("last_ip", ""),
                 "agent": v.get("agent", ""), "read_only": bool(v.get("read_only"))}
                for k, v in self._load().items()]

    def devices(self, online_window: int = 150, live_ids: set | None = None) -> dict:
        """Connected devices + the most recently connected one, newest first."""
        now = time.time()
        live_ids = live_ids or set()
        out = []
        for d in self.list():
            seen = d["last_seen"]
            d["online"] = d["id"] in live_ids or (seen and now - seen <= online_window)
            d["live"] = d["id"] in live_ids
            d["ago"] = int(now - seen) if seen else None
            out.append(d)
        out.sort(key=lambda d: d["last_seen"], reverse=True)
        last = out[0] if out and out[0]["last_seen"] else None
        return {"devices": out, "online": sum(1 for d in out if d["online"]), "total": len(out), "last": last}

    def revoke(self, tid: str) -> bool:
        with self.lock:
            data = self._load()
            if tid not in data:
                return False
            del data[tid]
            self._save(data)
            return True


def agent_label(ua: str) -> str:
    """Human label for a User-Agent: 'Android app', 'Web browser (Windows)', 'Desktop / CLI'..."""
    ua = ua or ""
    if "DomshonAndroid" in ua:
        return "Android app"
    if "httpx" in ua.lower() or "python" in ua.lower():
        return "Desktop / CLI (admin)"
    for key, os_ in (("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iOS"), ("Windows", "Windows"),
                     ("Mac OS", "macOS"), ("Linux", "Linux"), ("X11", "Linux")):
        if key in ua:
            return f"Web browser ({os_})"
    return "Web browser" if ua else "Unknown client"


class Presence:
    """Admin-session devices (web browser, Android app, desktop app, CLI): in-memory + small JSON file so the
    'last connected device' survives restarts. Device tokens are tracked by TokenStore instead."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.rec: dict[str, dict] = {}
        self._written = 0.0
        try:
            self.rec = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            self.rec = {}

    def touch(self, sid: str, ip: str = "", agent: str = "", force: bool = False):
        now = time.time()
        with self.lock:
            r = self.rec.setdefault(sid, {"first": int(now), "requests": 0})
            r.update(last_seen=int(now), last_ip=(ip or r.get("last_ip", ""))[:64],
                     agent=(agent or r.get("agent", ""))[:160])
            r["requests"] = r.get("requests", 0) + 1
            if force or now - self._written > 20:
                cutoff = now - 30 * 86400
                for k in [k for k, v in self.rec.items() if v.get("last_seen", 0) < cutoff]:
                    del self.rec[k]
                if len(self.rec) > 60:
                    for k in sorted(self.rec, key=lambda k: self.rec[k].get("last_seen", 0))[:-60]:
                        del self.rec[k]
                try:
                    tmp = self.path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.rec), "utf-8")
                    os.replace(tmp, self.path)
                    self._written = now
                except OSError:
                    pass

    def devices(self, live: set, current: str = "", window: int = 150) -> list[dict]:
        now = time.time()
        out = []
        for sid, r in self.rec.items():
            seen = r.get("last_seen", 0)
            label = agent_label(r.get("agent", ""))
            out.append({"id": "s_" + sid, "name": label + (" (this device)" if sid == current else ""),
                        "created": r.get("first", seen), "last_seen": seen, "last_ip": r.get("last_ip", ""),
                        "agent": r.get("agent", ""), "read_only": False, "session": True,
                        "current": sid == current, "live": sid in live,
                        "online": sid in live or bool(seen and now - seen <= window),
                        "ago": int(now - seen) if seen else None})
        return out


def merge_devices(tokens_view: dict, sessions: list[dict]) -> dict:
    """Combine token devices and admin-session devices; 'last' ignores the viewer's own session."""
    devs = tokens_view["devices"] + sessions
    devs.sort(key=lambda d: d["last_seen"], reverse=True)
    others = [d for d in devs if d["last_seen"] and not d.get("current")]
    return {"devices": devs, "online": sum(1 for d in devs if d["online"]), "total": len(devs),
            "last": others[0] if others else None}


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
