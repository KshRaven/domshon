"""Domshon FastAPI application: auth, files, sync manifest, admin, live events."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .. import APP_NAME, __version__
from ..common.scan import ignored, scan_tree
from ..config import Settings
from .mfa import MfaStore, SmsError
from .quota import MB, UsageCache, breakdown, check_space, disk_info
from .security import (Presence, Principal, RateLimiter, TokenStore, merge_devices, ct_eq, hash_password, sign, unsign,
                       verify_password)

WEB_DIR = Path(__file__).parent / "web"


class LoginBody(BaseModel):
    username: str
    password: str
    device: str | None = None
    code: str | None = None          # second factor (authenticator / text / e-mail / recovery code)


class MfaSendBody(BaseModel):
    username: str
    password: str
    method: str                       # "sms" | "email"


class CodeBody(BaseModel):
    code: str


class TargetBody(BaseModel):
    target: str                       # phone number or e-mail address


class PathBody(BaseModel):
    root: str = "main"
    path: str


class MoveBody(BaseModel):
    root: str = "main"
    src: str
    dst: str


class SettingsBody(BaseModel):
    changes: dict


class TextBody(BaseModel):
    root: str = "main"
    path: str
    content: str
    encoding: str = "utf-8"
    eol: str = "lf"
    mtime: float | None = None


MAX_TEXT_BYTES = 2 * 1024 * 1024  # largest file the in-browser editor will open


class PasswordBody(BaseModel):
    new_password: str


class TokenBody(BaseModel):
    name: str
    read_only: bool = False


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.meta: dict[WebSocket, dict] = {}

    async def broadcast(self, msg: dict):
        dead = []
        for c in list(self.clients):
            try:
                await c.send_json(msg)
            except Exception:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    tokens = TokenStore(settings.dir / "tokens.json")
    presence = Presence(settings.dir / "presence.json")
    limiter = RateLimiter()
    mfa = MfaStore(settings.dir / "mfa.json")
    hub = Hub()
    usage = UsageCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bootstrap_admin(settings)
        try:
            settings.storage_root()
        except OSError as exc:
            raise RuntimeError(
                f"Cannot create the storage folder ({exc}). The service runs as an unprivileged user: point "
                "DS_STORAGE_ROOT at a folder it can write (e.g. /var/lib/domshon/files), or give it ownership: "
                "sudo chown -R domshon:domshon <folder>. Folders under /home are not writable for the service.") from exc
        yield

    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                     "connect-src 'self' ws: wss:")
        if request.url.path.startswith("/static") or request.url.path == "/":
            h["Cache-Control"] = "no-cache"
        return resp

    def sid_of(token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()[:8]

    # ---- auth dependencies ---------------------------------------------------
    def authenticate(request: Request) -> Principal:
        settings.reload()
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        pr = None
        if token.startswith("dst_"):
            rec = tokens.verify(token)
            if rec:
                pr = Principal("user", rec["name"], bool(rec.get("read_only")))
                tokens.touch(rec["id"], request.client.host if request.client else "",
                             request.headers.get("user-agent", ""))
        elif token:
            data = unsign(settings.secret(), token)
            if data and data.get("typ") == "session":
                pr = Principal("admin", settings.get("admin_user"))
                presence.touch(sid_of(token), request.client.host if request.client else "",
                               request.headers.get("user-agent", ""))
        elif request.method == "GET" and request.query_params.get("ticket"):
            data = unsign(settings.secret(), request.query_params["ticket"])
            if data and data.get("typ") == "ticket":
                pr = Principal(data.get("role", "user"), data.get("name", ""), bool(data.get("ro")))
        if pr is None:
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if settings.get("lockdown") and pr.role != "admin":
            raise HTTPException(503, "Server is in lockdown mode")
        return pr

    def writer(pr: Principal = Depends(authenticate)) -> Principal:
        if settings.get("read_only"):
            raise HTTPException(403, "Server is in read-only mode")
        if pr.read_only:
            raise HTTPException(403, "This device token is read-only")
        if pr.role != "admin" and not settings.get("user_writes"):
            raise HTTPException(403, "Only the admin may change files on this server")
        return pr

    def admin_only(pr: Principal = Depends(authenticate)) -> Principal:
        if pr.role != "admin":
            raise HTTPException(403, "Admin only")
        return pr

    def admin_writer(pr: Principal = Depends(admin_only)) -> Principal:
        return pr

    def safe_path(root: str, rel: str) -> tuple[Path, Path]:
        roots = settings.roots()
        if root not in roots:
            raise HTTPException(404, "Unknown root")
        try:
            base = roots[root].resolve()
            rel = (rel or "").replace("\\", "/").strip("/")
            target = (base / rel).resolve() if rel else base
        except (ValueError, OSError):
            raise HTTPException(400, "Bad path")
        if target != base and base not in target.parents:
            raise HTTPException(400, "Path escapes root")
        return base, target

    def check_name(name: str):
        bad = settings.blocked_exts()
        ext = Path(name).suffix.lower()
        if bad and ext in bad:
            raise HTTPException(400, f"Files of type '{ext}' are not allowed on this server")

    def space_guard(root: str, base: Path, existing: int = 0):
        """Return check(size) that raises HTTP 507 when a write would break the quota / disk reserve."""
        quota = int(settings.get("quota_mb")) * MB if root == "main" else 0
        reserve = int(settings.get("min_free_mb")) * MB
        used = max(0, usage.used(base) - existing) if quota else 0
        free = disk_info(base).get("free", 0) + existing if reserve else 0

        def check(size: int):
            try:
                check_space(used=used, quota=quota, free=free, reserve=reserve, size=size)
            except ValueError as exc:
                raise HTTPException(507, str(exc))
        return check

    def storage_report() -> dict:
        base = settings.storage_root()
        return {"root": "main", "used": usage.used(base),
                "quota": int(settings.get("quota_mb")) * MB,
                "reserve": int(settings.get("min_free_mb")) * MB, "disk": disk_info(base)}

    def relpath(base: Path, target: Path) -> str:
        r = target.relative_to(base).as_posix()
        return "" if r == "." else r

    # ---- public --------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "name": APP_NAME, "version": __version__}

    def password_ok(username: str, password: str) -> bool:
        stored = settings.get("admin_password_hash")
        plain = settings.get("admin_password")
        # A plain DS_ADMIN_PASSWORD in .env / the environment is the owner's explicit choice and wins over an
        # older generated hash (otherwise editing .env after the first start silently does nothing).
        if plain:
            pw_ok = ct_eq(password, plain)
        else:
            pw_ok = bool(stored) and verify_password(password, stored)
        return bool(ct_eq(username, settings.get("admin_user")) and pw_ok)

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "?"
        limiter.check(ip)
        settings.reload()
        if not password_ok(body.username, body.password):
            limiter.fail(ip)
            raise HTTPException(401, "Invalid credentials")
        if mfa.enabled():
            if not (body.code or "").strip():
                # password was right: tell the client which second factors it can use (it then asks for a code)
                raise HTTPException(401, "mfa_required:" + ",".join(mfa.methods(settings)))
            if not mfa.verify_login(body.code):
                limiter.fail(ip)
                raise HTTPException(401, "Invalid or expired code")
        limiter.ok(ip)
        if body.device:
            _, tok = tokens.create(body.device.strip()[:64] or "device")
            return {"token": tok, "role": "user", "persistent": True}
        exp = time.time() + int(settings.get("session_hours")) * 3600
        return {"token": sign(settings.secret(), {"typ": "session", "exp": exp}),
                "role": "admin", "expires": exp}

    @app.post("/api/auth/mfa/send")
    def mfa_send(body: MfaSendBody, request: Request):
        """Text / e-mail a sign-in code. Needs the correct password, so it cannot be used to spam strangers."""
        ip = request.client.host if request.client else "?"
        limiter.check(ip)
        settings.reload()
        if not password_ok(body.username, body.password):
            limiter.fail(ip)
            raise HTTPException(401, "Invalid credentials")
        try:
            return {"sent": True, "to": mfa.send_login_code(settings, body.method)}
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except SmsError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/static/theme.css")
    def theme_css():
        from .. import theme
        return Response(theme.css_variables(), media_type="text/css")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # ---- session helpers -------------------------------------------------------
    @app.get("/api/me")
    def me(pr: Principal = Depends(authenticate)):
        return {"role": pr.role, "name": pr.name, "read_only": settings.get("read_only"),
                "domain": settings.get("domain"), "version": __version__}

    @app.get("/api/ticket")
    def ticket(request: Request, pr: Principal = Depends(authenticate)):
        tid = sid = ""
        hdr = request.headers.get("authorization", "")[7:].strip()
        if hdr.startswith("dst_"):
            parts = hdr.split("_", 2)
            tid = parts[1] if len(parts) == 3 else ""
        elif hdr:
            sid = sid_of(hdr)
        return {"ticket": sign(settings.secret(), {"typ": "ticket", "role": pr.role, "name": pr.name,
                                                   "tid": tid, "sid": sid, "ro": pr.read_only,
                                                   "exp": time.time() + 60})}

    @app.get("/api/usage")
    def usage_info(pr: Principal = Depends(authenticate)):
        return storage_report()

    @app.get("/api/roots")
    def roots(pr: Principal = Depends(authenticate)):
        return {"roots": list(settings.roots().keys())}

    # ---- files -------------------------------------------------------------------
    @app.get("/api/files")
    def list_files(root: str = "main", path: str = "", pr: Principal = Depends(authenticate)):
        base, target = safe_path(root, path)
        if not target.is_dir():
            raise HTTPException(404, "Not a folder")
        items = []
        for e in os.scandir(target):
            if ignored(e.name):
                continue
            try:
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                continue
            items.append({"name": e.name, "type": "dir" if is_dir else "file",
                          "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime})
        items.sort(key=lambda i: (i["type"] != "dir", i["name"].lower()))
        return {"root": root, "path": relpath(base, target), "items": items}

    @app.put("/api/files")
    async def upload(request: Request, root: str = "main", path: str = "",
                     pr: Principal = Depends(writer)):
        base, target = safe_path(root, path)
        if target == base:
            raise HTTPException(400, "Missing file path")
        if target.is_dir():
            raise HTTPException(409, "A folder with that name exists")
        check_name(target.name)
        existing = target.stat().st_size if target.is_file() else 0
        guard = await run_in_threadpool(space_guard, root, base, existing)
        declared = request.headers.get("content-length", "")
        if declared.isdigit():
            guard(int(declared))
        await run_in_threadpool(target.parent.mkdir, parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".dspart")
        limit = int(settings.get("max_upload_mb")) * 1024 * 1024
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    guard(size)
                    if limit and size > limit:
                        raise HTTPException(413, "File exceeds max upload size")
                    await run_in_threadpool(f.write, chunk)
            mt = request.headers.get("x-ds-mtime")
            if mt:
                try:
                    os.utime(tmp, (time.time(), float(mt)))
                except ValueError:
                    pass
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        usage.invalidate()
        await hub.broadcast({"event": "change", "root": root, "path": relpath(base, target.parent)})
        return {"ok": True, "size": size}

    @app.get("/api/text")
    def read_text(root: str = "main", path: str = "", pr: Principal = Depends(authenticate)):
        """Return a text file for the in-browser / in-app editor."""
        base, target = safe_path(root, path)
        if not target.is_file():
            raise HTTPException(404, "Not a file")
        st = target.stat()
        if st.st_size > MAX_TEXT_BYTES:
            raise HTTPException(413, "File is too large to edit online (limit 2 MB)")
        data = target.read_bytes()
        if b"\0" in data:
            raise HTTPException(415, "This looks like a binary file; download it instead")
        enc = "utf-8"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            enc, text = "latin-1", data.decode("latin-1")
        if text.startswith("\ufeff"):
            enc, text = "utf-8-sig", text[1:]
        eol = "crlf" if "\r\n" in text else "lf"
        if eol == "crlf":
            text = text.replace("\r\n", "\n")
        return {"content": text, "encoding": enc, "eol": eol, "mtime": st.st_mtime, "size": st.st_size}

    @app.put("/api/text")
    async def write_text(body: TextBody, pr: Principal = Depends(writer)):
        """Save an edited text file (atomic; refuses if it changed on the server meanwhile)."""
        base, target = safe_path(body.root, body.path)
        if target == base or target.is_dir():
            raise HTTPException(400, "Bad file path")
        check_name(target.name)
        if body.encoding not in ("utf-8", "utf-8-sig", "latin-1"):
            raise HTTPException(400, "Unsupported encoding")
        exists = target.is_file()
        if exists and body.mtime is not None and abs(target.stat().st_mtime - body.mtime) > 0.001:
            raise HTTPException(409, "The file changed on the server since you opened it")
        text = body.content.replace("\r\n", "\n")
        if body.eol == "crlf":
            text = text.replace("\n", "\r\n")
        try:
            data = text.encode(body.encoding)
        except UnicodeEncodeError:
            raise HTTPException(400, "Text cannot be saved in the file's original encoding")
        if body.encoding == "utf-8-sig":
            data = b"\xef\xbb\xbf" + data
        if len(data) > MAX_TEXT_BYTES:
            raise HTTPException(413, "File is too large to edit online (limit 2 MB)")
        guard = await run_in_threadpool(space_guard, body.root, base, target.stat().st_size if exists else 0)
        guard(len(data))
        await run_in_threadpool(target.parent.mkdir, parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".dspart")
        try:
            await run_in_threadpool(tmp.write_bytes, data)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        usage.invalidate()
        await hub.broadcast({"event": "change", "root": body.root, "path": relpath(base, target.parent)})
        return {"ok": True, "mtime": target.stat().st_mtime, "size": len(data)}

    @app.get("/api/download")
    def download(root: str = "main", path: str = "", pr: Principal = Depends(authenticate)):
        base, target = safe_path(root, path)
        if target.is_file():
            return FileResponse(target, filename=target.name)
        if target.is_dir():
            tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
            tmp.close()
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
                for rel, meta in scan_tree(target).items():
                    if meta["type"] == "file":
                        z.write(target / rel, rel)
                    else:
                        z.writestr(rel + "/", "")
            return FileResponse(tmp.name, filename=(target.name or root) + ".zip",
                                background=BackgroundTask(os.unlink, tmp.name))
        raise HTTPException(404, "Not found")

    @app.post("/api/mkdir")
    async def mkdir(body: PathBody, pr: Principal = Depends(writer)):
        base, target = safe_path(body.root, body.path)
        if target.is_file():
            raise HTTPException(409, "A file with that name exists")
        await run_in_threadpool(target.mkdir, parents=True, exist_ok=True)
        await hub.broadcast({"event": "change", "root": body.root, "path": relpath(base, target.parent)})
        return {"ok": True}

    @app.post("/api/move")
    async def move(body: MoveBody, pr: Principal = Depends(writer)):
        base, src = safe_path(body.root, body.src)
        _, dst = safe_path(body.root, body.dst)
        if src == base or dst == base:
            raise HTTPException(400, "Cannot move the root")
        if not src.exists():
            raise HTTPException(404, "Source not found")
        if dst.exists():
            raise HTTPException(409, "Destination exists")
        if src in dst.parents:
            raise HTTPException(400, "Cannot move a folder into itself")
        if src.is_file():
            check_name(dst.name)
        await run_in_threadpool(dst.parent.mkdir, parents=True, exist_ok=True)
        await run_in_threadpool(shutil.move, str(src), str(dst))
        await hub.broadcast({"event": "change", "root": body.root, "path": relpath(base, dst.parent)})
        return {"ok": True}

    @app.delete("/api/files")
    async def delete(root: str = "main", path: str = "", pr: Principal = Depends(writer)):
        base, target = safe_path(root, path)
        if target == base:
            raise HTTPException(400, "Cannot delete the root")
        if not target.exists():
            raise HTTPException(404, "Not found")
        if target.is_dir():
            await run_in_threadpool(shutil.rmtree, target)
        else:
            await run_in_threadpool(target.unlink)
        usage.invalidate()
        await hub.broadcast({"event": "change", "root": root, "path": relpath(base, target.parent)})
        return {"ok": True}

    @app.get("/api/manifest")
    def manifest(root: str = "main", path: str = "", pr: Principal = Depends(authenticate)):
        _, target = safe_path(root, path)
        return {"entries": scan_tree(target) if target.is_dir() else {}}

    @app.get("/api/hash")
    def file_hash(root: str = "main", path: str = "", pr: Principal = Depends(authenticate)):
        import hashlib
        _, target = safe_path(root, path)
        if not target.is_file():
            raise HTTPException(404, "Not a file")
        h = hashlib.sha256()
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return {"sha256": h.hexdigest()}

    # ---- admin -----------------------------------------------------------------------
    @app.get("/api/admin/settings")
    def admin_get(pr: Principal = Depends(admin_only)):
        root = settings.storage_root()
        try:
            du = shutil.disk_usage(root)
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            disk = {}
        return {"settings": settings.public(), "roots": list(settings.roots().keys()), "disk": disk,
                "usage": storage_report()}

    @app.get("/api/admin/usage")
    def admin_usage(pr: Principal = Depends(admin_only)):
        usage.invalidate()
        return {**storage_report(), "top": breakdown(settings.storage_root())}

    @app.patch("/api/admin/settings")
    def admin_patch(body: SettingsBody, pr: Principal = Depends(admin_only)):
        try:
            settings.update(body.changes)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc))
        usage.invalidate()
        return {"settings": settings.public()}

    @app.post("/api/admin/password")
    def admin_password(body: PasswordBody, pr: Principal = Depends(admin_only)):
        if len(body.new_password) < 10:
            raise HTTPException(400, "Password must be at least 10 characters")
        settings.set_internal("admin_password_hash", hash_password(body.new_password))
        return {"ok": True}

    @app.get("/api/admin/tokens")
    def tokens_list(pr: Principal = Depends(admin_only)):
        return {"tokens": tokens.list()}

    @app.post("/api/admin/tokens")
    def tokens_create(body: TokenBody, pr: Principal = Depends(admin_only)):
        tid, tok = tokens.create(body.name.strip()[:64] or "device", body.read_only)
        return {"id": tid, "token": tok, "read_only": body.read_only}

    @app.delete("/api/admin/tokens/{tid}")
    def tokens_revoke(tid: str, pr: Principal = Depends(admin_only)):
        if not tokens.revoke(tid):
            raise HTTPException(404, "No such token")
        return {"ok": True}

    @app.get("/api/admin/devices")
    def devices_list(request: Request, pr: Principal = Depends(admin_only)):
        live = {m.get("tid") for m in hub.meta.values() if m.get("tid")}
        live_s = {m.get("sid") for m in hub.meta.values() if m.get("sid")}
        hdr = request.headers.get("authorization", "")[7:].strip()
        cur = sid_of(hdr) if hdr and not hdr.startswith("dst_") else ""
        return merge_devices(tokens.devices(live_ids=live), presence.devices(live_s, cur))

    @app.post("/api/admin/rotate-secret")
    def rotate(pr: Principal = Depends(admin_only)):
        try:
            settings.rotate_secret()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        exp = time.time() + int(settings.get("session_hours")) * 3600
        return {"token": sign(settings.secret(), {"typ": "session", "exp": exp})}

    # ---- two-factor setup (admin session required) ---------------------------------------
    def mfa_guard(fn):
        try:
            return fn()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except SmsError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/admin/mfa")
    def mfa_status(pr: Principal = Depends(admin_only)):
        return mfa.status(settings)

    @app.post("/api/admin/mfa/totp/begin")
    def mfa_totp_begin(pr: Principal = Depends(admin_only)):
        from . import mfa as m
        sec = mfa.begin_totp()
        return {"secret": sec, "grouped": m.group(sec),
                "uri": m.otpauth_uri(sec, settings.get("admin_user"),
                                     "Domshon" + (" " + settings.get("domain") if settings.get("domain") else ""))}

    def confirmed(codes):
        if codes is None:
            raise HTTPException(400, "That code is wrong or has expired")
        return {"ok": True, "recovery": codes, "status": mfa.status(settings)}

    @app.post("/api/admin/mfa/totp/confirm")
    def mfa_totp_confirm(body: CodeBody, pr: Principal = Depends(admin_only)):
        return confirmed(mfa.confirm_totp(body.code))

    @app.post("/api/admin/mfa/sms/begin")
    def mfa_sms_begin(body: TargetBody, pr: Principal = Depends(admin_only)):
        mfa_guard(lambda: mfa.begin_sms(settings, body.target))
        return {"sent": True}

    @app.post("/api/admin/mfa/sms/confirm")
    def mfa_sms_confirm(body: CodeBody, pr: Principal = Depends(admin_only)):
        return confirmed(mfa.confirm_sms(body.code))

    @app.post("/api/admin/mfa/email/begin")
    def mfa_email_begin(body: TargetBody, pr: Principal = Depends(admin_only)):
        mfa_guard(lambda: mfa.begin_email(settings, body.target))
        return {"sent": True}

    @app.post("/api/admin/mfa/email/confirm")
    def mfa_email_confirm(body: CodeBody, pr: Principal = Depends(admin_only)):
        return confirmed(mfa.confirm_email(body.code))

    @app.delete("/api/admin/mfa/{method}")
    def mfa_remove(method: str, code: str = "", pr: Principal = Depends(admin_only)):
        """Remove one factor (totp | sms | email) or all of them (all). Needs a current code."""
        if mfa.enabled() and not mfa.verify_login(code):
            raise HTTPException(400, "Enter a current code to confirm")
        fn = {"totp": mfa.remove_totp, "sms": mfa.remove_sms, "email": mfa.remove_email, "all": mfa.disable}.get(method)
        if not fn:
            raise HTTPException(404, "Unknown method")
        fn()
        return {"ok": True, "status": mfa.status(settings)}

    @app.post("/api/admin/mfa/recovery")
    def mfa_recovery(body: CodeBody, pr: Principal = Depends(admin_only)):
        if not mfa.enabled():
            raise HTTPException(400, "Turn on a second factor first")
        if not mfa.verify_login(body.code):
            raise HTTPException(400, "Enter a current code to confirm")
        return {"recovery": mfa.new_recovery()}

    # ---- live events -----------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        data = unsign(settings.secret(), socket.query_params.get("ticket", ""))
        if not data or data.get("typ") != "ticket":
            await socket.close(code=1008)
            return
        await socket.accept()
        hub.clients.add(socket)
        hub.meta[socket] = {"tid": data.get("tid", ""), "sid": data.get("sid", ""), "name": data.get("name", "")}
        if data.get("sid"):
            presence.touch(data["sid"], socket.client.host if socket.client else "", "websocket", force=True)
        if data.get("tid"):
            tokens.touch(data["tid"], socket.client.host if socket.client else "", "websocket", force=True)
        try:
            while True:
                await socket.receive_text()
                if data.get("tid"):
                    tokens.touch(data["tid"])
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.clients.discard(socket)
            hub.meta.pop(socket, None)
            if data.get("tid"):
                tokens.touch(data["tid"], force=True)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app


def bootstrap_admin(settings: Settings):
    """Ensure an admin password exists; generate and print one on first start."""
    import secrets

    if settings.get("admin_password_hash") or settings.get("admin_password"):
        return
    pw = secrets.token_urlsafe(14)
    settings.set_internal("admin_password_hash", hash_password(pw))
    print("=" * 60)
    print(f" {APP_NAME} first start - generated admin credentials (shown once)")
    print(f"   user:     {settings.get('admin_user')}")
    print(f"   password: {pw}")
    print(" Change it with: domshon server passwd")
    print("=" * 60, flush=True)
