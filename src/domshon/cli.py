"""Domshon terminal interface:  `domshon ...`  (alias: `ds ...`)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from . import APP_NAME, __version__
from .client.api import ApiError, Client
from .client.config import ClientConfig, Profile, default_sync_dir


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------ server (local admin)
def cmd_server(a):
    from .config import EDITABLE, SPECS, Settings, _coerce
    from .server.security import TokenStore, hash_password

    if not a.sub:
        return build_parser().parse_args(["server", "-h"])
    s = Settings()
    if a.sub == "serve":
        from .server import run
        return run()
    if a.sub == "show":
        print(json.dumps(s.public(), indent=2))
        print(f"config dir: {s.dir}")
        return
    if a.sub == "usage":
        from .server.quota import MB, breakdown, dir_size, disk_info, human as hsz
        base = s.storage_root()
        used, quota = dir_size(base), int(s.get("quota_mb")) * MB
        print(f"storage folder: {base}")
        print(f"cloud usage:    {hsz(used)}" + (f" of {hsz(quota)} quota ({used * 100 // quota}%)" if quota else " (no quota)"))
        du = disk_info(base)
        if du:
            res = int(s.get("min_free_mb"))
            print(f"disk:           {hsz(du['free'])} free of {hsz(du['total'])}" + (f" (keeping {res} MB in reserve)" if res else ""))
        for t in breakdown(base, 15):
            print(f"  {hsz(t['size']):>10}  {t['name']}{'/' if t['type'] == 'dir' else ''}")
        return
    if a.sub == "set":
        key = a.key.lower().removeprefix("ds_")
        if key in EDITABLE:
            s.update({key: a.value})
        elif key in ("host", "port", "trust_proxy"):
            s.set_internal(key, _coerce(SPECS[key][1], a.value))
            print("(restart required for this key)")
        else:
            die(f"'{key}' cannot be set here. Editable: {', '.join(sorted(EDITABLE))}, host, port, trust_proxy")
        print(f"{key} updated (running servers pick this up within ~1s)")
        return
    if a.sub == "passwd":
        pw = a.password or getpass.getpass("New admin password (min 10 chars): ")
        if not a.password and getpass.getpass("Repeat: ") != pw:
            die("passwords differ")
        if len(pw) < 10:
            die("password too short")
        s.set_internal("admin_password_hash", hash_password(pw))
        print("Admin password updated.")
        return
    if a.sub == "token":
        ts = TokenStore(s.dir / "tokens.json")
        if a.action == "add":
            tid, tok = ts.create(a.name or "device")
            print(f"id: {tid}\ntoken (shown once): {tok}")
        elif a.action == "list":
            for t in ts.list():
                print(f"{t['id']}  {t['name']}  {time.strftime('%Y-%m-%d', time.localtime(t['created']))}")
        elif a.action == "rm":
            print("revoked" if ts.revoke(a.name) else "no such token")


# ------------------------------------------------------------------ client helpers
def client_cfg() -> tuple[ClientConfig, Client]:
    cfg = ClientConfig.load()
    if not cfg.token:
        die("not logged in. Run: ds login --server https://your.domain")
    return cfg, Client(cfg.server_url, cfg.token)


def admin_client(a) -> Client:
    cfg = ClientConfig.load()
    url = getattr(a, "server", None) or cfg.server_url
    user = os.environ.get("DS_ADMIN_USER") or input("Admin user: ")
    pw = os.environ.get("DS_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    c = Client(url)
    c.http.headers["Authorization"] = "Bearer " + c.login(user, pw)["token"]
    return c


def cmd_login(a):
    cfg = ClientConfig.load()
    if a.server:
        cfg.server_url = a.server.rstrip("/")
    if a.token:
        cfg.token = a.token
        Client(cfg.server_url, cfg.token).me()
    else:
        user = a.user or os.environ.get("DS_ADMIN_USER") or input("Username: ")
        pw = a.password or os.environ.get("DS_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        cfg.token = Client(cfg.server_url).login(user, pw, device=cfg.device)["token"]
    cfg.save()
    print(f"Logged in to {cfg.server_url} as device '{cfg.device}'.")


def cmd_logout(a):
    cfg = ClientConfig.load()
    cfg.token = ""
    cfg.save()
    print("Logged out (token removed locally).")


def cmd_status(a):
    cfg = ClientConfig.load()
    print(f"{APP_NAME} {__version__}\nserver: {cfg.server_url}\nlogged in: {'yes' if cfg.token else 'no'}")
    try:
        print("health:", Client(cfg.server_url).health())
        if cfg.token:
            print("me:", Client(cfg.server_url, cfg.token).me())
    except ApiError as e:
        print("server unreachable:", e)
    for p in cfg.profiles:
        print(f"profile {p.name}: {p.local_dir} <-> {p.remote_root}:/{p.remote_path} every {p.interval}s"
              f"{'' if p.enabled else ' (disabled)'}")


# ------------------------------------------------------------------ files
def cmd_ls(a):
    _, c = client_cfg()
    for it in c.list(a.root, a.path):
        kind = "d" if it["type"] == "dir" else "-"
        print(f"{kind} {human(it['size']):>10}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(it['mtime']))}  {it['name']}")


def cmd_mkdir(a):
    _, c = client_cfg()
    c.mkdir(a.root, a.path)


def cmd_rm(a):
    _, c = client_cfg()
    c.delete(a.root, a.path)


def cmd_mv(a):
    _, c = client_cfg()
    c.move(a.root, a.src, a.dst)


def cmd_put(a):
    _, c = client_cfg()
    src = Path(a.local).expanduser()
    if not src.exists():
        die(f"{src} not found")
    dest = a.remote.strip("/")
    if src.is_file():
        target = "/".join(x for x in (dest, src.name) if x)
        c.upload(a.root, target, src)
        print(f"↑ {target}")
        return
    base = "/".join(x for x in (dest, src.name) if x)
    c.mkdir(a.root, base)
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = Path(dirpath).relative_to(src).as_posix()
        for d in dirnames:
            c.mkdir(a.root, "/".join(x for x in (base, rel_dir if rel_dir != "." else "", d) if x))
        for f in filenames:
            rel = "/".join(x for x in (base, rel_dir if rel_dir != "." else "", f) if x)
            c.upload(a.root, rel, Path(dirpath) / f)
            print(f"↑ {rel}")


def cmd_get(a):
    _, c = client_cfg()
    path = a.remote.strip("/")
    name = path.rsplit("/", 1)[-1] or a.root
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    is_dir = not path or any(i["name"] == name and i["type"] == "dir" for i in c.list(a.root, parent))
    dest = Path(a.local).expanduser() if a.local else Path.cwd() / (name + (".zip" if is_dir else ""))
    if dest.is_dir():
        dest = dest / (name + (".zip" if is_dir else ""))
    c.download(a.root, path, dest)
    print(f"↓ {dest}")


# ------------------------------------------------------------------ sync
def cmd_sync(a):
    from .client.sync import Runner, SyncError, sync_once
    if not a.sub:
        return build_parser().parse_args(["sync", "-h"])

    cfg = ClientConfig.load()
    if a.sub == "add":
        if cfg.profile(a.name):
            die("profile exists")
        cfg.profiles.append(Profile(a.name, str(Path(a.local).expanduser().resolve()), a.root,
                                    a.path.strip("/"), a.interval))
        cfg.save()
        print("profile added")
    elif a.sub == "list":
        for p in cfg.profiles:
            print(f"{p.name}: {p.local_dir} <-> {p.remote_root}:/{p.remote_path} every {p.interval}s")
    elif a.sub == "rm":
        cfg.profiles = [p for p in cfg.profiles if p.name != a.name]
        cfg.save()
    elif a.sub == "run":
        _, c = client_cfg()
        targets = [p for p in cfg.profiles if not a.name or p.name == a.name]
        if not targets:
            die("no matching profiles (add one with: ds sync add NAME LOCAL_DIR)")
        for p in targets:
            try:
                r = sync_once(c, p, on_event=print, force=a.force)
                print(f"[{p.name}] {r.summary()}")
                for e in r.errors:
                    print("  !", e)
            except (SyncError, ApiError) as e:
                print(f"[{p.name}] failed: {e}")
    elif a.sub == "watch":
        run_daemon()


def run_daemon():
    from .client.sync import Runner
    r = Runner(on_status=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    r.start()
    print("Sync daemon running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        r.stop()


def cmd_daemon(a):
    run_daemon()


# ------------------------------------------------------------------ remote admin
def cmd_admin(a):
    if not a.sub:
        return build_parser().parse_args(["admin", "-h"])
    c = admin_client(a)
    if a.sub == "show":
        d = c.admin_settings()
        print(json.dumps(d["settings"], indent=2))
        if d["disk"]:
            print(f"disk: {human(d['disk']['used'])} used / {human(d['disk']['total'])}")
        u = d.get("usage")
        if u:
            print(f"cloud: {human(u['used'])}" + (f" of {human(u['quota'])} quota" if u["quota"] else " (no quota)"))
    elif a.sub == "usage":
        u = c.admin_usage()
        print(f"cloud usage: {human(u['used'])}" + (f" of {human(u['quota'])} quota" if u["quota"] else " (no quota)"))
        if u["disk"]:
            print(f"disk: {human(u['disk']['free'])} free of {human(u['disk']['total'])}"
                  + (f" (reserve {human(u['reserve'])})" if u["reserve"] else ""))
        for t in u["top"]:
            print(f"  {human(t['size']):>10}  {t['name']}{'/' if t['type'] == 'dir' else ''}")
    elif a.sub == "set":
        v = a.value
        print(json.dumps(c.admin_update({a.key.lower(): v})["settings"], indent=2))
    elif a.sub == "passwd":
        pw = getpass.getpass("New admin password (min 10 chars): ")
        c.admin_password(pw)
        print("password updated")
    elif a.sub == "tokens":
        for t in c.admin_tokens():
            print(f"{t['id']}  {t['name']}  {time.strftime('%Y-%m-%d', time.localtime(t['created']))}")
    elif a.sub == "token-add":
        print(c.admin_token_create(a.name)["token"])
    elif a.sub == "token-rm":
        c.admin_token_revoke(a.id)
    elif a.sub == "rotate":
        c.admin_rotate()
        print("secret rotated; all sessions invalidated")



# ------------------------------------------------------------------ hosting (service / nginx)
def cmd_service(a):
    from . import deploy
    if not a.sub:
        return build_parser().parse_args(["service", "-h"])
    try:
        print(deploy.service(a.sub, env_file=getattr(a, "env_file", None), domain=getattr(a, "domain", "") or "",
                             user_mode=getattr(a, "user", False), no_start=getattr(a, "no_start", False),
                             port=getattr(a, "port", None)))
    except deploy.DeployError as e:
        die(str(e))


def cmd_nginx(a):
    from . import deploy
    from .config import Settings
    if not a.sub:
        return build_parser().parse_args(["nginx", "-h"])
    try:
        if a.sub == "remove":
            return print(deploy.nginx_remove())
        s = Settings()
        domain = a.domain or s.get("domain")
        port = a.port or int(s.get("port"))
        mb = a.max_upload_mb if a.max_upload_mb is not None else int(s.get("max_upload_mb"))
        if a.sub == "show":
            return print(deploy.render_nginx(domain, port, mb, a.cert or "", a.key or ""))
        print(deploy.nginx_install(domain, port, mb, certbot=a.certbot, email=a.email or "", cert=a.cert or "",
                                   key=a.key or "", install_packages=a.install_packages, env_file=a.env_file))
    except deploy.DeployError as e:
        die(str(e))


def cmd_desktop(a):
    from . import desktop
    if not a.sub:
        return build_parser().parse_args(["desktop", "-h"])
    if a.sub == "install":
        print(desktop.install(desktop_shortcut=a.desktop_shortcut))
    elif a.sub == "uninstall":
        print(desktop.uninstall())
    else:
        print("installed" if desktop.is_installed() else "not installed")

# ------------------------------------------------------------------ gui / boot
def cmd_gui(a):
    try:
        from .gui.app import run_gui
    except ImportError as e:
        die(f"GUI dependencies missing ({e}). Install with: pip install 'domshon[gui]'")
    run_gui(minimized=False)


def cmd_tray(a):
    try:
        from .gui.app import run_gui
    except ImportError as e:
        die(f"GUI dependencies missing ({e}). Install with: pip install 'domshon[gui]'")
    run_gui(minimized=a.minimized)


def cmd_autostart(a):
    from . import autostart
    target = "server" if a.server else "tray"
    if a.action == "enable":
        print(autostart.enable(target))
    elif a.action == "disable":
        print(autostart.disable(target))
    else:
        print("enabled" if autostart.is_enabled(target) else "disabled")


# ------------------------------------------------------------------ parser
FMT = argparse.RawDescriptionHelpFormatter

TOP_DESC = f"""\
{APP_NAME} - self-hosted cloud file manager with cross-platform folder sync.

One program, four faces: a server you host, a web app, a desktop app and this terminal tool.
Every command and sub-command accepts -h / --help with its own explanation and examples.

Typical flow
  on the server :  domshon server passwd  ->  domshon server serve      (try it)
                   sudo domshon service install                       (keep it running at boot)
                   sudo domshon nginx install --domain D --certbot    (HTTPS on your domain)
  on each device:  domshon login --server https://files.example.com
                   domshon sync add work ~/Work --path projects/alpha
                   domshon daemon        (keep syncing in the background)
"""

TOP_EPILOG = """\
command groups
  server side   server         run and configure the server on THIS machine (live settings, tokens)
                service        install the server as a boot-time OS service (systemd / Windows Service)
                nginx          put nginx + free HTTPS certificates in front of it (Ubuntu/Debian)
                admin          configure a remote server over the network (admin login required)
  your account  login, logout, status
  your files    ls, mkdir, mv, rm, put, get
  sync          sync (profiles and one-off runs), daemon (background loop)
  desktop       gui, tray, autostart, desktop (launcher + icon)

get help for anything:
  domshon COMMAND -h               e.g.  domshon put -h
  domshon COMMAND SUBCOMMAND -h    e.g.  domshon sync add -h

environment variables (all optional)
  DS_SERVER          default server address for the client (https://files.example.com)
  DS_TOKEN           API token used by the Python package (domshon.connect)
  DS_ADMIN_USER / DS_ADMIN_PASSWORD   non-interactive credentials for 'login' and 'admin'
  DS_CONFIG_DIR      where the SERVER keeps settings.json, tokens.json, secret.key
  DS_CLIENT_DIR      where the CLIENT keeps its config.json (server address, token, sync profiles)
  DS_ENV_FILE        path of the server .env file (default: ./.env)
"""


def _ex(*lines: str) -> str:
    return "examples:\n" + "\n".join("  " + l for l in lines)


def _add(sub, name, help, desc=None, examples=(), fn=None):
    """Create a sub-command parser that always has a long description and examples for -h."""
    q = sub.add_parser(name, help=help, description=(desc or help).rstrip() + "\n",
                       epilog=_ex(*examples) if examples else None, formatter_class=FMT)
    if fn:
        q.set_defaults(fn=fn)
    return q


def _group(sub, name, help, desc, examples=()):
    q = _add(sub, name, help, desc, examples)
    ss = q.add_subparsers(dest="sub", metavar="SUBCOMMAND", title="subcommands")
    q.set_defaults(fn=lambda a, _q=q: _q.print_help())
    return q, ss


def _root(x):
    x.add_argument("--root", default="main", metavar="NAME",
                   help="which server folder to use: 'main' (the default storage folder) or the name "
                        "of an extra folder the admin exposed with DS_MOUNTS (default: main)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="domshon", description=TOP_DESC, epilog=TOP_EPILOG, formatter_class=FMT)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sp = p.add_subparsers(dest="cmd", metavar="COMMAND", title="commands")
    p.set_defaults(fn=lambda a: p.print_help())

    # ---- server (this machine)
    s, ss = _group(sp, "server", "run and configure the server on this machine",
        "Run and administer the Domshon server ON THE MACHINE THAT HOLDS YOUR FILES.\n"
        "These commands read and write the server's own config folder directly, so no network\n"
        "login is needed. Settings changed here apply to a running server within about a second\n"
        "(except host, port and trust_proxy, which need a restart).\n"
        "To manage a server from another computer use 'domshon admin' instead.",
        ["domshon server passwd", "domshon server serve", "domshon server show",
         "domshon server set quota_mb 400", "domshon server usage", "domshon server token add laptop"])
    s.set_defaults(fn=cmd_server)
    _add(ss, "serve", "start the server in the foreground",
         "Start the web/API server using the current settings (.env, environment, settings.json).\n"
         "On first start an admin password is generated and printed once if none is set.\n"
         "For a permanent setup run the server as an OS service instead (see the README).",
         ["domshon server serve"])
    _add(ss, "show", "print the live settings and the config folder",
         "Print every setting the server is currently using (secrets are never printed) and the\n"
         "folder where settings.json, tokens.json and secret.key are stored.",
         ["domshon server show"])
    _add(ss, "usage", "show cloud storage use, quota and the biggest folders",
         "Show how much space the cloud uses against its quota (quota_mb), the free disk space and the\n"
         "largest top-level folders of the storage folder. Runs on the server machine.",
         ["domshon server usage"])
    x = _add(ss, "set", "change one setting live",
             "Change a setting without restarting. Editable live: domain, storage_root, mounts,\n"
             "admin_user, max_upload_mb, session_hours, read_only, lockdown, and the disk-space\n"
             "controls: quota_mb (cap for the cloud, 0 = unlimited), min_free_mb (keep this much of\n"
             "the disk free), blocked_extensions (e.g. .exe,.php) and user_writes (false = only the\n"
             "admin may change files).\n"
             "Needs a restart: host, port, trust_proxy.\n"
             "The 'DS_' prefix is optional and keys are case-insensitive.",
             ["domshon server set domain files.example.com", "domshon server set read_only true",
              "domshon server set storage_root /srv/domshon/files",
              "domshon server set quota_mb 400", "domshon server set blocked_extensions .exe,.php",
              "domshon server set mounts \"photos=/srv/photos;docs=/srv/docs\""])
    x.add_argument("key", help="setting name, e.g. max_upload_mb")
    x.add_argument("value", help="new value (true/false for switches, a number, a path or text)")
    x = _add(ss, "passwd", "set or reset the admin password",
             "Set the admin password (minimum 10 characters). Without --password you are prompted\n"
             "twice and nothing is echoed. Existing admin sessions stay valid until they expire;\n"
             "use 'domshon admin rotate' to cut them all off.",
             ["domshon server passwd", "domshon server passwd --password 'a long passphrase'"])
    x.add_argument("--password", metavar="TEXT",
                   help="the new password (visible in shell history - prefer the prompt)")
    x = _add(ss, "token", "create, list or revoke device tokens",
             "Device tokens let a computer, script or app sync without knowing the admin password.\n"
             "A token is shown ONCE when created; revoke it at any time to cut that device off.",
             ["domshon server token add laptop", "domshon server token list", "domshon server token rm 1a2b3c4d"])
    x.add_argument("action", choices=["add", "list", "rm"], help="add = create, list = show, rm = revoke")
    x.add_argument("name", nargs="?", help="add: a label for the device; rm: the token id from 'list'")

    # ---- account
    x = _add(sp, "login", "log this device in to a server",
             "Sign this device in and store a device token locally (the password is never stored).\n"
             "Either give a username and password (a token is created for you) or paste an existing\n"
             "token made with 'domshon server token add'. Prompts are used for anything not given.",
             ["domshon login --server https://files.example.com",
              "domshon login --server https://files.example.com --token dst_xxxxxxxx_xxxxxxxx"], cmd_login)
    x.add_argument("--server", metavar="URL", help="server address, e.g. https://files.example.com")
    x.add_argument("--user", metavar="NAME", help="admin username (default: ask, or DS_ADMIN_USER)")
    x.add_argument("--password", metavar="TEXT", help="password (default: ask, or DS_ADMIN_PASSWORD)")
    x.add_argument("--token", metavar="TOKEN", help="use an existing device token instead of a password")
    _add(sp, "logout", "forget the stored token on this device",
         "Remove the token from this device's config. The token itself stays valid on the server\n"
         "until you revoke it ('domshon admin token-rm ID').", ["domshon logout"], cmd_logout)
    _add(sp, "status", "show server address, login state, health and sync profiles",
         "Show which server this device points to, whether it is logged in, whether the server is\n"
         "reachable, and every configured sync profile. First thing to run when something is off.",
         ["domshon status"], cmd_status)

    # ---- files
    x = _add(sp, "ls", "list a server folder",
             "List the contents of a folder on the server (default: the top of the 'main' folder).",
             ["domshon ls", "domshon ls projects/alpha", "domshon ls --root photos"], cmd_ls)
    x.add_argument("path", nargs="?", default="", help="folder path on the server (default: top level)")
    _root(x)
    x = _add(sp, "mkdir", "create a folder on the server",
             "Create a folder on the server, including any missing parents.",
             ["domshon mkdir projects/alpha/results"], cmd_mkdir)
    x.add_argument("path", help="folder path to create")
    _root(x)
    x = _add(sp, "rm", "delete a file or folder on the server",
             "Delete a file or a whole folder on the server. This cannot be undone.",
             ["domshon rm projects/alpha/old.zip"], cmd_rm)
    x.add_argument("path", help="file or folder to delete")
    _root(x)
    x = _add(sp, "mv", "move or rename on the server",
             "Move or rename a file or folder inside the server. Fails if the destination exists.",
             ["domshon mv notes/a.txt notes/archive/a.txt"], cmd_mv)
    x.add_argument("src", help="existing path")
    x.add_argument("dst", help="new path")
    _root(x)
    x = _add(sp, "put", "upload a file or a whole folder",
             "Upload a local file or folder (recursively) into a server folder.\n"
             "The item keeps its name: 'put ./report.pdf docs' creates docs/report.pdf.",
             ["domshon put ./report.pdf docs", "domshon put ~/Work/alpha projects"], cmd_put)
    x.add_argument("local", help="local file or folder to upload")
    x.add_argument("remote", nargs="?", default="", help="destination folder on the server (default: top level)")
    _root(x)
    x = _add(sp, "get", "download a file or a folder",
             "Download a file, or a folder as a .zip. With no LOCAL the item is saved in the current\n"
             "folder; if LOCAL is an existing folder the item is saved inside it.",
             ["domshon get docs/report.pdf", "domshon get projects/alpha ~/Downloads"], cmd_get)
    x.add_argument("remote", help="file or folder on the server")
    x.add_argument("local", nargs="?", help="where to save it (default: current folder)")
    _root(x)

    # ---- sync
    s, ss = _group(sp, "sync", "manage and run folder sync",
        "Two-way sync between a local folder and a folder on the server.\n"
        "A 'profile' ties one local folder to one server folder. Conflicting edits keep the newest\n"
        "copy and save the other beside it as NAME.conflict-<time>.EXT.\n"
        "Profiles are stored on this device; 'domshon daemon' (or the desktop app) runs them continuously.",
        ["domshon sync add work ~/Work --path projects/alpha", "domshon sync run", "domshon sync list"])
    s.set_defaults(fn=cmd_sync)
    x = _add(ss, "add", "add a sync profile",
             "Create a profile linking LOCAL (default ~/Domshon) with a server folder.\n"
             "Use --root to pick a server folder and --path to sync only a sub-folder of it.",
             ["domshon sync add work ~/Work --path projects/alpha --interval 60"])
    x.add_argument("name", help="short label for this profile")
    x.add_argument("local", nargs="?", default=str(default_sync_dir()),
                   help="local folder to sync (default: %(default)s)")
    x.add_argument("--root", default="main", metavar="NAME", help="server folder to use (default: main)")
    x.add_argument("--path", default="", metavar="SUBFOLDER", help="sub-folder on the server (default: the whole root)")
    x.add_argument("--interval", type=int, default=30, metavar="SECONDS",
                   help="how often the background sync runs (default: 30)")
    _add(ss, "list", "list sync profiles", "Show every sync profile configured on this device.", ["domshon sync list"])
    x = _add(ss, "rm", "remove a sync profile",
             "Forget a profile. Local and server files are NOT deleted.", ["domshon sync rm work"])
    x.add_argument("name", help="profile to remove")
    x = _add(ss, "run", "sync once, now",
             "Run one sync pass for every profile, or only NAME. Use --force to re-compare everything\n"
             "instead of trusting the saved state.",
             ["domshon sync run", "domshon sync run work --force"])
    x.add_argument("name", nargs="?", help="only this profile (default: all enabled profiles)")
    x.add_argument("--force", action="store_true", help="ignore saved state and compare all files again")
    _add(ss, "watch", "keep syncing until Ctrl+C", "Same as 'domshon daemon'.", ["domshon sync watch"])
    _add(sp, "daemon", "headless background sync loop",
         "Run all enabled sync profiles continuously in this terminal (Ctrl+C to stop). For a\n"
         "permanent background sync on a desktop use 'domshon autostart enable'.",
         ["domshon daemon"], cmd_daemon)

    # ---- remote admin
    s, ss = _group(sp, "admin", "manage a remote server (asks for admin credentials)",
        "Manage a server over the network. You are asked for the admin username and password\n"
        "(or set DS_ADMIN_USER and DS_ADMIN_PASSWORD). Nothing is stored. The server address\n"
        "comes from --server or from this device's login.\n"
        "For the machine you are sitting at, 'domshon server ...' works without a login.",
        ["domshon admin show", "domshon admin set max_upload_mb 200", "domshon admin tokens",
         "domshon admin --server https://files.example.com show   (--server goes BEFORE the subcommand)"])
    s.add_argument("--server", metavar="URL", help="server address (default: the one from 'domshon login')")
    s.set_defaults(fn=cmd_admin)
    _add(ss, "show", "show the server's live settings and disk usage", "Print the server's current settings and disk usage.")
    _add(ss, "usage", "show cloud storage use, quota and the biggest folders",
         "Show how much space the cloud uses against its quota, the free disk space and the largest\n"
         "top-level folders.", ["domshon admin usage"])
    x = _add(ss, "set", "change one server setting live",
             "Change a setting on the server. Same keys as 'domshon server set'.",
             ["domshon admin set read_only true", "domshon admin set domain files.example.com"])
    x.add_argument("key", help="setting name, e.g. lockdown")
    x.add_argument("value", help="new value")
    _add(ss, "passwd", "change the admin password", "Change the admin password (minimum 10 characters).")
    _add(ss, "tokens", "list device tokens", "List the device tokens that can currently sign in.")
    x = _add(ss, "token-add", "create a device token", "Create a token for a new device. It is shown once.",
             ["domshon admin token-add phone"])
    x.add_argument("name", help="a label for the device")
    x = _add(ss, "token-rm", "revoke a device token", "Revoke a token; that device can no longer sync.",
             ["domshon admin token-rm 1a2b3c4d"])
    x.add_argument("id", help="token id from 'domshon admin tokens'")
    _add(ss, "rotate", "invalidate every admin session",
         "Replace the server's signing key. All admin sessions (web and desktop) must sign in again.\n"
         "Use this if you think a session was exposed.")

    # ---- hosting
    s, ss = _group(sp, "service", "run the server as an OS service (starts at boot)",
        "Install the server as a proper background service that starts at boot and restarts on failure.\n"
        "  Linux    systemd system service, runs as the unprivileged user 'domshon' (use sudo)\n"
        "  Windows  real Windows Service; needs  pip install \"domshon[winservice]\"  and an Administrator prompt\n"
        "  macOS    launchd (best effort)\n"
        "Settings live in /etc/domshon/domshon.env (Linux) or %ProgramData%\\Domshon (Windows); the server\n"
        "reloads them live. For HTTPS in front of it see 'domshon nginx -h'.",
        ["sudo domshon service install --domain files.example.com", "sudo domshon service status",
         "sudo domshon service restart", "domshon service install --user      (no root: systemd user unit)"])
    s.set_defaults(fn=cmd_service)

    def _svc(name, help, desc, examples=(), install=False):
        x = _add(ss, name, help, desc, examples)
        x.add_argument("--user", action="store_true",
                       help="Linux without root: use a systemd *user* unit (starts at login unless 'loginctl enable-linger')")
        if install:
            x.add_argument("--env-file", metavar="PATH", help="settings file to use (default: ./.env if present)")
            x.add_argument("--domain", metavar="HOST", help="public domain to record in the settings (Linux)")
            x.add_argument("--port", type=int, metavar="N", help="Windows service port (default: DS_PORT or 8787)")
            x.add_argument("--no-start", action="store_true", help="register the service but do not start it now")
        return x
    _svc("install", "install and start the service",
         "Create the service, its data folders and settings file, enable it at boot and start it.\n"
         "Safe to re-run: it updates the service and keeps your settings and files.",
         ["sudo domshon service install", "sudo domshon service install --env-file /srv/domshon/.env",
          "domshon service install --port 8788  (Windows: choose another listening port)"], install=True)
    _svc("uninstall", "stop and remove the service", "Stop the service and remove it. Your files and settings are kept.")
    _svc("start", "start the service", "Start the installed service.")
    _svc("stop", "stop the service", "Stop the installed service.")
    _svc("restart", "restart the service",
         "Restart the service. Needed after changing host, port, trust_proxy or secret_key.")
    _svc("status", "show whether the service is running", "Show the service state (and, if stopped, why).")
    _svc("logs", "show the server log", "Print the most recent lines of the service's log "
         "(%ProgramData%\\Domshon\\logs\\server.log on Windows, the journal on Linux).\n"
         "Start here when the service will not stay running.", ["domshon service logs"])

    s, ss = _group(sp, "nginx", "put nginx (and free HTTPS certificates) in front of the server (Linux)",
        "Generate or install an nginx site that proxies your domain to the Domshon server, with\n"
        "WebSocket support, streaming uploads/downloads and no body-size cap. With --certbot it also gets\n"
        "a Let's Encrypt certificate and redirects HTTP to HTTPS. Targets Ubuntu/Debian; needs root.\n"
        "Point your domain's DNS at this machine first and open ports 80 and 443.",
        ["domshon nginx show --domain files.example.com",
         "sudo domshon nginx install --domain files.example.com --certbot --email you@example.com",
         "sudo domshon nginx remove"])
    s.set_defaults(fn=cmd_nginx)

    def _ngx(name, help, desc, examples=(), install=False):
        x = _add(ss, name, help, desc, examples)
        x.add_argument("--domain", metavar="HOST", help="your domain (default: the server's DS_DOMAIN setting)")
        x.add_argument("--port", type=int, metavar="N", help="the Domshon server port (default: DS_PORT, 8787)")
        x.add_argument("--max-upload-mb", type=int, metavar="MB", dest="max_upload_mb",
                       help="nginx request-size cap in MB, 0 = unlimited (default: the server's max_upload_mb)")
        x.add_argument("--cert", metavar="FILE", help="use this certificate file instead of certbot (needs --key)")
        x.add_argument("--key", metavar="FILE", help="private key file for --cert")
        if install:
            x.add_argument("--certbot", action="store_true", help="obtain and install a Let's Encrypt certificate")
            x.add_argument("--email", metavar="ADDR", help="contact address for certbot (recommended)")
            x.add_argument("--install-packages", action="store_true", dest="install_packages",
                           help="apt-get install nginx (and certbot) first")
            x.add_argument("--env-file", metavar="PATH",
                           help="server settings file to update (default: /etc/domshon/domshon.env)")
        return x
    _ngx("show", "print the nginx config without installing it", "Print the nginx server block this command would install.",
         ["domshon nginx show --domain files.example.com"])
    _ngx("install", "write the nginx site, reload nginx, optionally get HTTPS",
         "Write /etc/nginx/sites-available/domshon.conf (enabled via sites-enabled), test and reload nginx,\n"
         "and set DS_DOMAIN / DS_TRUST_PROXY=true / DS_HOST=127.0.0.1 in the service settings file.\n"
         "Restart the server afterwards ('sudo domshon service restart') so it picks up the proxy mode.",
         ["sudo domshon nginx install --domain files.example.com --install-packages --certbot --email you@example.com"],
         install=True)
    x = _add(ss, "remove", "remove the nginx site", "Remove the Domshon nginx site and reload nginx. Certificates are kept.")

    # ---- desktop
    s, ss = _group(sp, "desktop", "add or remove the application-menu / Start-Menu launcher",
        "The desktop app creates its launcher (name, icon, taskbar identity) the first time it runs, so\n"
        "after  pip install \"domshon[gui]\"  just start it once with 'domshon-gui' (or 'domshon gui').\n"
        "Use these commands to create, repair or remove the launcher by hand.\n"
        "  Linux    ~/.local/share/applications/domshon.desktop and icons (shows up in the app menu / dock)\n"
        "  Windows  Start Menu shortcut (right-click it -> Pin to taskbar)\n"
        "  Both also get a Desktop shortcut unless you pass --no-desktop-shortcut.\n"
        "Set DS_NO_DESKTOP_INTEGRATION=1 to stop the app from creating it automatically.",
        ["domshon desktop install", "domshon desktop install --no-desktop-shortcut", "domshon desktop status",
         "domshon desktop uninstall"])
    s.set_defaults(fn=cmd_desktop)
    x = _add(ss, "install", "create or refresh the launcher",
             "Create the launcher and icon files for the current user (no admin rights needed).",
             ["domshon desktop install --no-desktop-shortcut"])
    x.add_argument("--desktop-shortcut", action="store_true", dest="desktop_shortcut", default=True,
                   help=argparse.SUPPRESS)       # kept for old scripts: the Desktop shortcut is now the default
    x.add_argument("--no-desktop-shortcut", action="store_false", dest="desktop_shortcut",
                   help="only the Start Menu / application-menu entry, no Desktop shortcut")
    _add(ss, "uninstall", "remove the launcher and its icons", "Remove the launcher and icon files.")
    _add(ss, "status", "say whether the launcher exists", "Print 'installed' or 'not installed'.")

    _add(sp, "gui", "open the desktop app",
         "Open the desktop application (needs the optional desktop support:\n"
         "pip install 'domshon[gui]'). The app also keeps running in the system tray.",
         ["domshon gui"], cmd_gui)
    x = _add(sp, "tray", "start the desktop app in the system tray",
             "Start the desktop application with its tray icon. With --minimized the window stays\n"
             "hidden and only the tray icon appears - this is what launch-on-boot uses.",
             ["domshon tray --minimized"], cmd_tray)
    x.add_argument("--minimized", action="store_true", help="start hidden, tray icon only")
    x = _add(sp, "autostart", "launch on boot / login",
             "Start the tray app automatically when you log in (default), or with --server start the\n"
             "server itself on boot. Windows: per-user Run key (tray) or a scheduled task (server,\n"
             "needs an elevated shell). Linux: autostart entry (tray) or a systemd user unit (server).\n"
             "macOS: LaunchAgent. For a real boot-time server service see the README.",
             ["domshon autostart enable", "domshon autostart status", "domshon autostart disable",
              "domshon autostart enable --server"], cmd_autostart)
    x.add_argument("action", choices=["enable", "disable", "status"], help="what to do")
    x.add_argument("--server", action="store_true", help="manage the server instead of the tray app")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except ApiError as e:
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
