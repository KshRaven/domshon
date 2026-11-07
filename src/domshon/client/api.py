"""Thin HTTP client for the Domshon server API."""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"[{status}] {detail}" if status else detail)
        self.status, self.detail = status, detail


def _chunks(fp, size=1 << 20):
    while True:
        b = fp.read(size)
        if not b:
            return
        yield b


class Client:
    def __init__(self, base_url: str, token: str = ""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, follow_redirects=True,
                                 timeout=httpx.Timeout(30.0, read=600.0, write=600.0))

    def close(self):
        self.http.close()

    def _req(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = self.http.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text[:200]
            raise ApiError(r.status_code, str(detail))
        return r

    # ---- session ----
    def health(self) -> dict:
        return self._req("GET", "/api/health").json()

    def login(self, user: str, password: str, device: str | None = None) -> dict:
        return self._req("POST", "/api/auth/login",
                         json={"username": user, "password": password, "device": device}).json()

    def me(self) -> dict:
        return self._req("GET", "/api/me").json()

    def usage(self) -> dict:
        return self._req("GET", "/api/usage").json()

    def roots(self) -> list[str]:
        return self._req("GET", "/api/roots").json()["roots"]

    # ---- files ----
    def list(self, root: str = "main", path: str = "") -> list[dict]:
        return self._req("GET", "/api/files", params={"root": root, "path": path}).json()["items"]

    def manifest(self, root: str, path: str = "") -> dict:
        return self._req("GET", "/api/manifest", params={"root": root, "path": path}).json()["entries"]

    def hash(self, root: str, path: str) -> str:
        return self._req("GET", "/api/hash", params={"root": root, "path": path}).json()["sha256"]

    def upload(self, root: str, path: str, local: Path):
        st = os.stat(local)
        with open(local, "rb") as fp:
            self._req("PUT", "/api/files", params={"root": root, "path": path}, content=_chunks(fp),
                      headers={"X-DS-Mtime": str(st.st_mtime), "Content-Length": str(st.st_size)})

    def download(self, root: str, path: str, dest: Path, mtime: float | None = None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".dspart")
        try:
            with self.http.stream("GET", "/api/download", params={"root": root, "path": path}) as r:
                if r.status_code >= 400:
                    r.read()
                    raise ApiError(r.status_code, r.text[:200])
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            if mtime:
                os.utime(tmp, (time.time(), mtime))
            os.replace(tmp, dest)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def mkdir(self, root: str, path: str):
        self._req("POST", "/api/mkdir", json={"root": root, "path": path})

    def move(self, root: str, src: str, dst: str):
        self._req("POST", "/api/move", json={"root": root, "src": src, "dst": dst})

    def delete(self, root: str, path: str):
        self._req("DELETE", "/api/files", params={"root": root, "path": path})

    # ---- admin ----
    def admin_settings(self) -> dict:
        return self._req("GET", "/api/admin/settings").json()

    def admin_usage(self) -> dict:
        return self._req("GET", "/api/admin/usage").json()

    def admin_update(self, changes: dict) -> dict:
        return self._req("PATCH", "/api/admin/settings", json={"changes": changes}).json()

    def admin_password(self, new_password: str):
        self._req("POST", "/api/admin/password", json={"new_password": new_password})

    def admin_tokens(self) -> list[dict]:
        return self._req("GET", "/api/admin/tokens").json()["tokens"]

    def admin_token_create(self, name: str, read_only: bool = False) -> dict:
        return self._req("POST", "/api/admin/tokens", json={"name": name, "read_only": read_only}).json()

    def admin_token_revoke(self, tid: str):
        self._req("DELETE", f"/api/admin/tokens/{tid}")

    def admin_rotate(self) -> str:
        return self._req("POST", "/api/admin/rotate-secret").json()["token"]

    def admin_devices(self) -> dict:
        return self._req("GET", "/api/admin/devices").json()
