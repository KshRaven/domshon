"""Two-way file AND folder synchronisation.

Algorithm: three-way compare of local tree, remote tree and the last agreed snapshot
("base"), which is what lets us tell a deletion from a new file, and a one-sided edit
from a real conflict.

  local+remote equal           -> nothing
  one side changed vs base     -> propagate that change
  both changed / no base       -> conflict: newest mtime wins, loser kept as *.conflict-*
  missing on one side + in base-> it was deleted there; delete here if unmodified
  missing on one side, no base -> new file; copy it over

Safety: refuses to run when a previously synced side is suddenly empty (unmounted drive,
swapped server storage) unless force=True.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from ..common.scan import scan_tree
from .api import ApiError, Client
from .config import ClientConfig, Profile, client_config_dir, safe_name

TOL = 2.0  # seconds; covers FAT/exFAT and network rounding


class SyncError(Exception):
    pass


@dataclass
class SyncResult:
    uploaded: int = 0
    downloaded: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    conflicts: int = 0
    errors: list = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (self.uploaded + self.downloaded + self.deleted_local + self.deleted_remote)

    def summary(self) -> str:
        s = (f"↑{self.uploaded} ↓{self.downloaded} "
             f"del(local {self.deleted_local}, remote {self.deleted_remote}) conflicts {self.conflicts}")
        return s + (f" errors {len(self.errors)}" if self.errors else "")


def _eq(a: dict, b: dict) -> bool:
    if a["type"] != b["type"]:
        return False
    if a["type"] == "dir":
        return True
    return a["size"] == b["size"] and abs(a["mtime"] - b["mtime"]) <= TOL


def _ok_rel(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return bool(parts) and not PurePosixPath(rel).is_absolute() and ".." not in parts and "\\" not in rel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _state_path(p: Profile) -> Path:
    d = client_config_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe_name(p.name)}.json"


def load_state(p: Profile) -> dict:
    import json
    try:
        return json.loads(_state_path(p).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(p: Profile, base: dict):
    import json
    tmp = _state_path(p).with_suffix(".tmp")
    tmp.write_text(json.dumps(base), "utf-8")
    os.replace(tmp, _state_path(p))


def _conflict_name(rel: str) -> str:
    pp = PurePosixPath(rel)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return str(pp.with_name(f"{pp.stem}.conflict-{stamp}{pp.suffix}"))


def sync_once(client: Client, p: Profile, on_event: Callable[[str], None] | None = None,
              should_stop: Callable[[], bool] | None = None, force: bool = False) -> SyncResult:
    emit = on_event or (lambda m: None)
    stop = should_stop or (lambda: False)
    res = SyncResult()
    lroot = Path(p.local_dir).expanduser()
    lroot.mkdir(parents=True, exist_ok=True)
    rbase = p.remote_path.strip("/")

    def lp(rel: str) -> Path:
        return lroot / rel

    def rp(rel: str) -> str:
        return "/".join(x for x in (rbase, rel) if x)

    local = {k: v for k, v in scan_tree(lroot).items() if _ok_rel(k)}
    remote = {k: v for k, v in client.manifest(p.remote_root, rbase).items() if _ok_rel(k)}
    base = load_state(p)

    if base and not force:
        if not local:
            raise SyncError("Local folder is empty but was synced before; refusing to delete remote "
                            "data. Re-run with force to override.")
        if not remote:
            raise SyncError("Remote folder is empty but was synced before; refusing to delete local "
                            "data. Re-run with force to override.")

    bad: set[str] = set()
    dirs: list[str] = []
    files: list[str] = []
    for rel in sorted(set(local) | set(remote)):
        l, r = local.get(rel), remote.get(rel)
        types = {e["type"] for e in (l, r) if e}
        if len(types) > 1:
            res.errors.append(f"{rel}: file/folder type mismatch")
            bad.add(rel)
            continue
        (dirs if types == {"dir"} else files).append(rel)

    # 1) create missing folders (shallow first); remember deletions for later
    del_local_dirs, del_remote_dirs = [], []
    for rel in sorted(dirs, key=lambda x: x.count("/")):
        l, r, b = local.get(rel), remote.get(rel), base.get(rel)
        if l and r:
            continue
        try:
            if l:
                if b:
                    del_local_dirs.append(rel)
                else:
                    client.mkdir(p.remote_root, rp(rel))
            else:
                if b:
                    del_remote_dirs.append(rel)
                else:
                    lp(rel).mkdir(parents=True, exist_ok=True)
        except (ApiError, OSError) as exc:
            res.errors.append(f"{rel}: {exc}")
            bad.add(rel)

    # 2) files
    remote_deleted: set[str] = set()

    def upload(rel):
        client.upload(p.remote_root, rp(rel), lp(rel))
        res.uploaded += 1
        emit(f"↑ {rel}")

    def download(rel):
        client.download(p.remote_root, rp(rel), lp(rel), mtime=remote[rel]["mtime"])
        res.downloaded += 1
        emit(f"↓ {rel}")

    for rel in files:
        if stop():
            break
        l, r, b = local.get(rel), remote.get(rel), base.get(rel)
        try:
            if l and r:
                if _eq(l, r):
                    continue
                if not b and l["size"] == r["size"]:
                    try:
                        if _sha256(lp(rel)) == client.hash(p.remote_root, rp(rel)):
                            os.utime(lp(rel), (time.time(), r["mtime"]))  # identical: adopt
                            continue
                    except (ApiError, OSError):
                        pass
                l_changed = not (b and _eq(l, b))
                r_changed = not (b and _eq(r, b))
                if l_changed and not r_changed:
                    upload(rel)
                elif r_changed and not l_changed:
                    download(rel)
                else:
                    res.conflicts += 1
                    cname = _conflict_name(rel)
                    if l["mtime"] >= r["mtime"]:
                        client.move(p.remote_root, rp(rel), rp(cname))
                        upload(rel)
                    else:
                        os.replace(lp(rel), lp(cname))
                        download(rel)
                    emit(f"⚠ conflict: {rel} (older copy kept as {cname})")
            elif l:
                if b and _eq(l, b):
                    lp(rel).unlink()
                    res.deleted_local += 1
                    emit(f"✕ local {rel}")
                else:
                    upload(rel)
            elif r:
                if b and _eq(r, b):
                    client.delete(p.remote_root, rp(rel))
                    remote_deleted.add(rel)
                    res.deleted_remote += 1
                    emit(f"✕ remote {rel}")
                else:
                    download(rel)
        except (ApiError, OSError) as exc:
            res.errors.append(f"{rel}: {exc}")
            bad.add(rel)

    # 3) folder deletions (deepest first); never remove anything that still has content
    for rel in sorted(del_local_dirs, key=lambda x: -x.count("/")):
        try:
            lp(rel).rmdir()
            res.deleted_local += 1
        except OSError:
            pass
    for rel in sorted(del_remote_dirs, key=lambda x: -x.count("/")):
        prefix = rel + "/"
        if any(k.startswith(prefix) and k not in remote_deleted for k in remote):
            continue
        try:
            client.delete(p.remote_root, rp(rel))
            res.deleted_remote += 1
        except ApiError:
            pass

    # 4) new agreed snapshot
    local2 = {k: v for k, v in scan_tree(lroot).items() if _ok_rel(k)}
    remote2 = {k: v for k, v in client.manifest(p.remote_root, rbase).items() if _ok_rel(k)}
    newbase = {k: v for k, v in local2.items() if k in remote2 and _eq(v, remote2[k])}
    for k in bad:  # keep old knowledge for failed items so deletions aren't misread as new files
        if k in base:
            newbase[k] = base[k]
    save_state(p, newbase)
    return res


class Runner(threading.Thread):
    """Background loop that syncs every enabled profile on its interval."""

    def __init__(self, on_status: Callable[[str], None] | None = None,
                 on_result: Callable[[str, SyncResult | None, str], None] | None = None):
        super().__init__(daemon=True, name="ds-sync")
        self.on_status = on_status or (lambda m: None)
        self.on_result = on_result or (lambda name, res, err: None)
        self._wake = threading.Event()
        self._halt = threading.Event()
        self.paused = False
        self.last_run: dict[str, float] = {}
        self.busy = False

    def sync_now(self):
        self.last_run.clear()
        self._wake.set()

    def stop(self):
        self._halt.set()
        self._wake.set()

    def run(self):
        while not self._halt.is_set():
            cfg = ClientConfig.load()
            if not self.paused and cfg.token and cfg.profiles:
                client = Client(cfg.server_url, cfg.token)
                try:
                    for p in cfg.profiles:
                        if self._halt.is_set() or self.paused:
                            break
                        if not p.enabled or time.time() - self.last_run.get(p.name, 0) < max(5, p.interval):
                            continue
                        self._one(client, p)
                finally:
                    client.close()
            self._wake.wait(timeout=2)
            self._wake.clear()

    def _one(self, client: Client, p: Profile):
        self.last_run[p.name] = time.time()
        self.busy = True
        self.on_status(f"Syncing {p.name}…")
        try:
            res = sync_once(client, p, on_event=self.on_status, should_stop=self._halt.is_set)
            self.on_status(f"{p.name}: {res.summary()}")
            self.on_result(p.name, res, "")
        except (SyncError, ApiError, OSError) as exc:
            self.on_status(f"{p.name}: {exc}")
            self.on_result(p.name, None, str(exc))
        finally:
            self.busy = False
