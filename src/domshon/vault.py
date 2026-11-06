"""Use a Domshon server from your own code.

    import domshon

    vault = domshon.connect("https://files.example.com", token="dst_...")
    vault.pull("projects/alpha", "./workdir")        # fetch your progress on a new machine
    ...                                              # run / train / edit
    vault.push("./workdir/results", "projects/alpha/results")

Nothing needs to be passed if the environment is configured:
    DS_SERVER   server address            DS_TOKEN      device token (preferred)
    DS_ADMIN_USER / DS_ADMIN_PASSWORD     login details (used when no token is given)
When nothing is given at all, the login saved by `domshon login` on this machine is used.

Device tokens are created with `domshon server token add NAME` (on the server) or from the
admin panel, and can be revoked at any time.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import warnings
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .client.api import ApiError, Client
from .client.config import ClientConfig, Profile
from .client.sync import TOL, SyncError, SyncResult, _ok_rel, sync_once
from .common.scan import scan_tree

__all__ = ["Vault", "Watcher", "connect", "VaultError", "ApiError", "SyncResult"]

Events = Callable[[str], None]


class VaultError(Exception):
    """Raised for configuration problems and refused-for-safety operations."""


def _clean(p: str) -> str:
    return (str(p) if p else "").replace("\\", "/").strip("/")


def _join(*parts: str) -> str:
    return "/".join(x for x in (_clean(p) for p in parts) if x)


def connect(server: str | None = None, token: str | None = None, user: str | None = None,
            password: str | None = None, root: str = "main") -> "Vault":
    """Connect to a server. Every argument is optional (see module docstring)."""
    return Vault(server, token, user, password, root)


class Vault:
    def __init__(self, server: str | None = None, token: str | None = None, user: str | None = None,
                 password: str | None = None, root: str = "main"):
        saved = ClientConfig.load()
        self.server = (server or os.environ.get("DS_SERVER") or saved.server_url).rstrip("/")
        self.root = root
        self._token = token or os.environ.get("DS_TOKEN") or ""
        self._user = user or os.environ.get("DS_ADMIN_USER") or ""
        self._password = password or os.environ.get("DS_ADMIN_PASSWORD") or ""
        if not self._token and not (self._user and self._password):
            # Only reuse the saved device login for the server it was issued by.
            if saved.token and saved.server_url.rstrip("/") == self.server:
                self._token = saved.token
        if not self._token and not (self._user and self._password):
            raise VaultError("No credentials: pass token=... (or user=/password=), set DS_TOKEN, "
                             "or run `domshon login` on this machine.")
        url = urlparse(self.server)
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            warnings.warn(f"{self.server} is not HTTPS: credentials and files travel unencrypted.",
                          stacklevel=2)
        self._client = Client(self.server, self._token)
        if not self._token:
            self._login()

    # ---- plumbing ---------------------------------------------------------
    def _login(self):
        tmp = Client(self.server)
        try:
            tok = tmp.login(self._user, self._password)["token"]
        finally:
            tmp.close()
        self._client.http.headers["Authorization"] = "Bearer " + tok

    def _call(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except ApiError as exc:
            if exc.status == 401 and self._user and self._password:
                self._login()  # expired session: sign in again, retry once
                return fn(*args, **kw)
            raise

    def _root(self, root: str | None) -> str:
        return root or self.root

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- info -------------------------------------------------------------
    def me(self) -> dict:
        return self._call(self._client.me)

    def roots(self) -> list[str]:
        return self._call(self._client.roots)

    # ---- single files / folders -------------------------------------------
    def ls(self, path: str = "", root: str | None = None) -> list[dict]:
        return self._call(self._client.list, self._root(root), _clean(path))

    def _kind(self, path: str, root: str | None = None) -> str | None:
        path = _clean(path)
        if not path:
            return "dir"
        parent, _, name = path.rpartition("/")
        try:
            for it in self.ls(parent, root):
                if it["name"] == name:
                    return it["type"]
        except ApiError as exc:
            if exc.status != 404:
                raise
        return None

    def exists(self, path: str, root: str | None = None) -> bool:
        return self._kind(path, root) is not None

    def mkdir(self, path: str, root: str | None = None):
        self._call(self._client.mkdir, self._root(root), _clean(path))

    def delete(self, path: str, root: str | None = None):
        self._call(self._client.delete, self._root(root), _clean(path))

    def move(self, src: str, dst: str, root: str | None = None):
        self._call(self._client.move, self._root(root), _clean(src), _clean(dst))

    def upload_file(self, local: str | os.PathLike, remote: str, root: str | None = None):
        self._call(self._client.upload, self._root(root), _clean(remote), Path(local))

    def download_file(self, remote: str, local: str | os.PathLike, root: str | None = None):
        self._call(self._client.download, self._root(root), _clean(remote), Path(local))

    def read_bytes(self, path: str, root: str | None = None) -> bytes:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "f"
            self.download_file(path, tmp, root)
            return tmp.read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8", root: str | None = None) -> str:
        return self.read_bytes(path, root).decode(encoding)

    def write_bytes(self, path: str, data: bytes, root: str | None = None):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "f"
            tmp.write_bytes(data)
            self.upload_file(tmp, path, root)

    def write_text(self, path: str, text: str, encoding: str = "utf-8", root: str | None = None):
        self.write_bytes(path, text.encode(encoding), root)

    # ---- folders: one-way ---------------------------------------------------
    def pull(self, remote: str = "", local: str | os.PathLike = ".", *, root: str | None = None,
             overwrite: bool = False, mirror: bool = False, on_event: Events | None = None) -> SyncResult:
        """Download a server folder into `local`.

        Missing files are fetched; changed files are replaced only when the server copy is newer
        (so unsaved local work is not clobbered) unless overwrite=True. mirror=True also deletes
        local files that do not exist on the server.
        """
        root, rbase = self._root(root), _clean(remote)
        lroot = Path(local).expanduser()
        lroot.mkdir(parents=True, exist_ok=True)
        emit = on_event or (lambda m: None)
        res = SyncResult()
        rtree = {k: v for k, v in self._call(self._client.manifest, root, rbase).items() if _ok_rel(k)}
        if not rtree:
            kind = self._kind(rbase, root)
            if kind is None:
                raise VaultError(f"Remote folder not found: {root}:/{rbase}")
            if kind == "file":
                raise VaultError(f"{root}:/{rbase} is a file; use download_file()")
        ltree = {k: v for k, v in scan_tree(lroot).items() if _ok_rel(k)}
        if mirror and not rtree and ltree:
            raise VaultError("Remote folder is empty; refusing to mirror-delete the local folder.")

        for rel, meta in sorted(rtree.items()):
            dest = lroot / rel
            mine = ltree.get(rel)
            try:
                if meta["type"] == "dir":
                    if mine and mine["type"] == "file":
                        res.errors.append(f"{rel}: a local file blocks this folder")
                        continue
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                if mine and mine["type"] == "dir":
                    res.errors.append(f"{rel}: a local folder blocks this file")
                    continue
                if mine:
                    if mine["size"] == meta["size"] and abs(mine["mtime"] - meta["mtime"]) <= TOL:
                        continue
                    if not overwrite and mine["mtime"] > meta["mtime"] + TOL:
                        emit(f"kept newer local copy: {rel}")
                        continue
                self._call(self._client.download, root, _join(rbase, rel), dest, meta["mtime"])
                res.downloaded += 1
                emit(f"down {rel}")
            except (ApiError, OSError) as exc:
                res.errors.append(f"{rel}: {exc}")

        if mirror:
            for rel in sorted((k for k in ltree if k not in rtree), key=len, reverse=True):
                try:
                    target = lroot / rel
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                    res.deleted_local += 1
                    emit(f"deleted local {rel}")
                except OSError as exc:
                    res.errors.append(f"{rel}: {exc}")
        return res

    def push(self, local: str | os.PathLike = ".", remote: str = "", *, root: str | None = None,
             overwrite: bool = False, mirror: bool = False, on_event: Events | None = None) -> SyncResult:
        """Upload a local folder to the server.

        Changed files are replaced only when the local copy is newer unless overwrite=True.
        mirror=True also deletes server files that do not exist locally.
        """
        root, rbase = self._root(root), _clean(remote)
        lroot = Path(local).expanduser()
        if not lroot.is_dir():
            raise VaultError(f"Local folder not found: {lroot}")
        emit = on_event or (lambda m: None)
        res = SyncResult()
        ltree = {k: v for k, v in scan_tree(lroot).items() if _ok_rel(k)}
        if mirror and not ltree:
            raise VaultError("Local folder is empty; refusing to mirror-delete the server folder.")
        if rbase:
            self._call(self._client.mkdir, root, rbase)
        rtree = {k: v for k, v in self._call(self._client.manifest, root, rbase).items() if _ok_rel(k)}

        for rel, meta in sorted(ltree.items()):
            theirs = rtree.get(rel)
            try:
                if meta["type"] == "dir":
                    if theirs and theirs["type"] == "file":
                        res.errors.append(f"{rel}: a server file blocks this folder")
                    elif not theirs:
                        self._call(self._client.mkdir, root, _join(rbase, rel))
                    continue
                if theirs and theirs["type"] == "dir":
                    res.errors.append(f"{rel}: a server folder blocks this file")
                    continue
                if theirs:
                    if theirs["size"] == meta["size"] and abs(theirs["mtime"] - meta["mtime"]) <= TOL:
                        continue
                    if not overwrite and theirs["mtime"] > meta["mtime"] + TOL:
                        emit(f"kept newer server copy: {rel}")
                        continue
                self._call(self._client.upload, root, _join(rbase, rel), lroot / rel)
                res.uploaded += 1
                emit(f"up {rel}")
            except (ApiError, OSError) as exc:
                res.errors.append(f"{rel}: {exc}")

        if mirror:
            for rel in sorted((k for k in rtree if k not in ltree), key=len, reverse=True):
                try:
                    self._call(self._client.delete, root, _join(rbase, rel))
                    res.deleted_remote += 1
                    emit(f"deleted remote {rel}")
                except ApiError as exc:
                    if exc.status != 404:  # already gone with its parent folder
                        res.errors.append(f"{rel}: {exc}")
        return res

    # ---- folders: two-way -----------------------------------------------------
    def _profile(self, local: str | os.PathLike, remote: str, root: str) -> Profile:
        lroot = Path(local).expanduser().resolve()
        key = f"{self.server}|{root}|{_clean(remote)}|{lroot}"
        return Profile(name="lib-" + hashlib.sha1(key.encode()).hexdigest()[:12], local_dir=str(lroot),
                       remote_root=root, remote_path=_clean(remote), interval=0)

    def sync(self, local: str | os.PathLike, remote: str = "", *, root: str | None = None,
             force: bool = False, on_event: Events | None = None) -> SyncResult:
        """Two-way sync of `local` with a server folder (same engine as the apps).

        Edits, new files, deletions and renames on either side are carried over; real conflicts
        keep the newest copy and save the other as *.conflict-<time>.
        """
        p = self._profile(local, remote, self._root(root))
        try:
            return self._call(sync_once, self._client, p, on_event=on_event, force=force)
        except SyncError as exc:
            raise VaultError(str(exc)) from exc

    def watch(self, local: str | os.PathLike, remote: str = "", *, root: str | None = None,
              interval: int = 30, on_event: Events | None = None,
              on_result: Callable[[SyncResult], None] | None = None,
              on_error: Callable[[Exception], None] | None = None) -> "Watcher":
        """Keep a folder in sync in a background thread. Call .stop() on the returned handle."""
        w = Watcher(self, local, remote, self._root(root), max(5, int(interval)), on_event, on_result, on_error)
        w.start()
        return w


class Watcher(threading.Thread):
    def __init__(self, vault: Vault, local, remote: str, root: str, interval: int,
                 on_event, on_result, on_error):
        super().__init__(daemon=True, name="domshon-watch")
        self.vault, self.local, self.remote, self.root = vault, local, remote, root
        self.interval, self.on_event, self.on_result, self.on_error = interval, on_event, on_result, on_error
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                res = self.vault.sync(self.local, self.remote, root=self.root, on_event=self.on_event)
                if self.on_result:
                    self.on_result(res)
            except (VaultError, ApiError, OSError) as exc:
                if self.on_error:
                    self.on_error(exc)
            self._halt.wait(self.interval)

    def sync_now(self):
        """Run one extra sync immediately (blocking)."""
        return self.vault.sync(self.local, self.remote, root=self.root, on_event=self.on_event)

    def stop(self):
        self._halt.set()
