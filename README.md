# Domshon

Self-hosted cloud file manager with cross-platform folder synchronisation.

- **Server** (Python / FastAPI) with its own admin web panel, admin desktop tab and admin CLI
- **Web app** with drag & drop for files and folders
- **Desktop app** for Windows and Linux (system tray, notifications, launch on boot)
- **Terminal interface** (`domshon` / `ds`): run it with no arguments for the full-screen menu app (arrow keys, numbers, Enter, Esc; 0 = back; wrap-around paging) or use the normal commands (`domshon --help`, `domshon help sync add`)
- **Python package**: `import domshon` to pull / push / sync project files from your own code
- **Android app**: a WebView client for the server web app, with native upload and download handling

The repository is the source for both halves: run the **server** on the machine that holds your
files, then use the **apps** (built from the same source, optionally with your server address baked
in) on every device.

## Install

| Command | You get | Use it for |
|---|---|---|
| `pip install domshon` / `pip install -e .` | server, command line (`domshon`, `ds`) and the Python package | servers, new systems, scripts. No Qt is installed |
| `pip install -e ".[gui]"` | + desktop app (`domshon-gui`) with launcher, taskbar icon and tray | Windows / Linux desktops |
| `pip install -e ".[winservice]"` | + Windows Service for the server | Windows servers |
| `pip install -e ".[dev]"` / `".[build]"` | tests and lint / PyInstaller, `build`, `twine` | working on Domshon itself only |

Pick one, in a virtualenv (`python -m venv .venv`, then `. .venv/bin/activate` or `.venv\Scripts\activate`).

## Quick start

Everything can be driven by the interactive manager, which also runs as direct commands:

| OS | Command |
|---|---|
| Linux / macOS | `bash scripts/pypimgr/pypimgr.sh` |
| Windows | `scripts\pypimgr\pypimgr.bat` |

Menu: **Setup** (venv + dependencies) -> **Initialize** (`.env`, domain, admin password) ->
**Run server / desktop app** -> **Build** -> **Update** -> **Docker** -> **Boot service** -> **PyPI**
(build, check, upload to TestPyPI / PyPI).

Manual equivalent (server on this machine, for a try-out):

```bash
pip install -e .                                       # add ".[gui]" if you also want the desktop app
cp .env.example .env                                   # edit DS_DOMAIN etc.
domshon server serve                                   # prints a one-time admin password on first start
```

Open `http://localhost:8787`, sign in as `admin`. To keep it running at boot, with HTTPS, see the next section.

### Local development setup

Use a virtual environment so Domshon and its dependencies stay separate from system Python.

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[gui,dev,build]"
cp .env.example .env
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[gui,dev,build]"
Copy-Item .env.example .env
notepad .env
```

For a machine-only test, set `DS_HOST=127.0.0.1` in `.env`; the example file listens on all
interfaces. Set a long unique `DS_ADMIN_PASSWORD`, or leave it empty to get a randomly generated
password printed once at first start. Start the server with `python -m domshon server serve` and
stop it with `Ctrl+C`. If PowerShell blocks activation, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` as a separate command, or call
`.\.venv\Scripts\python.exe` directly.

Check the health endpoint:

```bash
curl --fail http://127.0.0.1:8787/api/health
```

PowerShell equivalent:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/health | ConvertTo-Json
```

The web UI can upload a sample file by drag-and-drop. For a CLI test, create a device token on the
server and use it from a client:

```bash
domshon server token add sample-client
domshon login --server http://127.0.0.1:8787 --token "dst_TOKEN_FROM_SERVER"
printf 'Hello from Domshon\n' > sample.txt
domshon put ./sample.txt
domshon ls
domshon get sample.txt ./sample-downloaded.txt
```

The token is shown only once. Store it privately and revoke it if the device is lost. For public
servers use HTTPS rather than plain HTTP.

## Hosting the cloud on your server

The server is a Python program (FastAPI/uvicorn). It is built for an **Ubuntu/Debian Linux server**
(any systemd distro works); Windows is supported as a real Windows Service, macOS on a best-effort basis.
Pick one path below, then add HTTPS. Everything is also available as commands: `domshon service -h`,
`domshon nginx -h`.

### What you need first

1. A server with Python 3.10+ (Ubuntu: `sudo apt install python3 python3-venv python3-pip`).
2. A domain name whose DNS `A` record points at the server (for HTTPS).
3. Ports 80 and 443 open (`sudo ufw allow 'Nginx Full'`).

If the server is behind a router, forward TCP 80/443 to it. Only publish an `AAAA` record if IPv6
connectivity and firewall rules are configured too. Let's Encrypt HTTP validation requires the
domain to resolve to this server and port 80 to be reachable.

### Path A - one command on Ubuntu (recommended)

From a checkout of this repository on the server:

```bash
sudo bash scripts/install-service.sh install --nginx files.example.com --certbot you@example.com
```

That script: creates the unprivileged user `domshon`, builds a virtualenv in `/opt/domshon`, installs the
app, writes `/etc/domshon/domshon.env`, installs a **systemd service** that starts at boot and restarts
on failure, installs **nginx**, proxies your domain to the server and gets a free **Let's Encrypt**
certificate. Leave out `--nginx`/`--certbot` to do it in steps (below). Then:

```bash
sudo journalctl -u domshon-server --no-pager | grep -A4 'first start'   # one-time admin password
sudo domshon server passwd                                              # ...or set your own
```

Open `https://files.example.com` and sign in as `admin`. Without `--nginx` the server listens on
`127.0.0.1` only (the safe default); add nginx later with `sudo domshon nginx install ...`, or set
`DS_HOST=0.0.0.0` in `/etc/domshon/domshon.env` to use port 8787 directly. The systemd unit is generated by
the same code as `domshon service install` and is sandboxed (`NoNewPrivileges`, `ProtectSystem`, private
`/tmp`, `UMask 0027`, ...).

| Task | Command |
|---|---|
| Status / restart / logs | `sudo bash scripts/install-service.sh status` / `restart` / `logs` |
| Update to this checkout (or PyPI) | `sudo bash scripts/install-service.sh update` |
| Remove the service (keeps your files) | `sudo bash scripts/install-service.sh uninstall` |
| Data locations | files `/var/lib/domshon/files`, settings `/var/lib/domshon/config`, env file `/etc/domshon/domshon.env` |

### Path B - the same, as commands (any install of the package)

```bash
pip install domshon                      # or: pip install -e .   from the repository
sudo domshon service install --domain files.example.com     # systemd service, starts at boot
sudo domshon nginx install --domain files.example.com --install-packages --certbot --email you@example.com
sudo domshon service restart             # picks up proxy mode written by the nginx step
domshon service status
```

`domshon nginx show --domain D` prints the nginx config without installing anything, so you can copy it
into a setup you manage yourself. The generated site keeps WebSocket live updates, streams uploads and
downloads (no temp copies in nginx) and removes nginx's body-size limit; the **server** enforces upload
size and storage quota (see *Limiting the space the cloud can use*). No root? `domshon service install --user` installs a systemd
*user* unit (for boot without login: `sudo loginctl enable-linger $USER`).

### Windows server

```powershell
pip install "domshon[winservice]"
domshon service install      # run from an Administrator prompt
domshon service status | stop | start | restart | uninstall
```

The service uses `DS_PORT` (8787 by default). If another process already owns that port, stop that
server or install the service on a different port, for example `domshon service install --port 8788`.
Update clients and any reverse-proxy upstream to use the selected port.

A real Windows Service (automatic, delayed start, restarts on failure, runs as LocalSystem). Data and logs live
in `%ProgramData%\Domshon`; its `config\` folder (signing key, device tokens) is locked to Administrators and the
service, so run `domshon server passwd` on that machine from an Administrator prompt - it uses the same folder
automatically. Settings: put a `.env` in the folder you run `install` from (or pass `--env-file`), or use the admin
panel. If registering fails with a `pythonservice.exe` or DLL message, run once as Administrator
`python -m pywin32_postinstall -install` and repeat. To reach the server from other machines set
`DS_HOST=0.0.0.0` and allow the port, e.g.
`netsh advfirewall firewall add rule name="Domshon" dir=in action=allow protocol=TCP localport=8787`.
The packaged `.exe` cannot host the service - use `domshon autostart enable --server` (Task Scheduler) for it.
For HTTPS on Windows put any reverse proxy in front (the nginx config from `domshon nginx show` works).

### Docker (testing or production)

```bash
docker compose up -d --build                         # http://localhost:8787
```

#### Docker with HTTPS (nginx + Let's Encrypt)

nginx is the proxy everywhere in Domshon (it is what most servers already run). Set `DS_DOMAIN=your.domain`
and `DS_TRUST_PROXY=true` in `.env`, point the domain's DNS at the server, open ports 80 and 443, then:

```bash
docker compose --profile proxy up -d --build      # 1. nginx on :80, serves the certificate challenge
docker compose --profile certbot run --rm certbot certonly --webroot -w /var/www/certbot \
    -d your.domain --email you@example.com --agree-tos --no-eff-email         # 2. get the certificate
docker compose --profile proxy down && docker compose --profile https up -d   # 3. switch to HTTPS
```

Renew from cron about monthly: `docker compose --profile certbot run --rm certbot renew && docker compose
exec nginx nginx -s reload`. Without a certificate (LAN, or TLS handled by Cloudflare / a load balancer) just
stay on `--profile proxy`. Not using Docker? `sudo domshon nginx install --domain your.domain --certbot ...`
does the same on the host (see above).

### Configuration and live changes

All settings are `DS_*` variables (see `.env.example`). Precedence: defaults < process environment <
`.env` < `settings.json`. The server re-reads `.env` and `settings.json` within about a second, so
these change **live**: domain, storage location, extra folders, upload limit, storage quota and
disk reserve, blocked file types, user write access, session length, read-only mode, lockdown. Host, port, proxy mode and secret key need a restart.

Change them from the web admin panel, the desktop **Admin** tab, `domshon admin set KEY VALUE`
(remote) or `domshon server set KEY VALUE` (on the host).

### Limiting the space the cloud can use

Small hosts (for example plans with ~500 MB of disk shared with a website) need a ceiling. The admin
decides it, live, from the web **Admin** tab, the desktop **Admin** tab, `domshon admin set` or
`domshon server set`:

| Setting (`DS_*` in `.env`) | Effect |
|---|---|
| `quota_mb` | Hard cap for the main storage folder. Uploads past it get HTTP 507 and a clear message. `0` = unlimited. |
| `min_free_mb` | Refuse writes that would leave less than this much of the disk free. Keeps the web server and OS healthy. |
| `max_upload_mb` | Largest single file. |
| `blocked_extensions` | File types that may not be stored, e.g. `.exe,.php`. |
| `user_writes` | `false` = only the admin can change files; device users become read-only. |

```bash
domshon server set quota_mb 400            # on the server, e.g. for a 500 MB host
domshon server set min_free_mb 50
domshon server usage                       # usage vs quota, free disk, biggest folders
domshon admin usage                        # the same, from another computer
```

The quota covers the main storage folder only (extra `DS_MOUNTS` folders are existing folders you own and
are not counted). Usage is measured by scanning the folder and cached for ~15 seconds, so two uploads started
at the same instant can overshoot by their own size. Everyone sees their usage next to the file list in the
web app; the admin panels also show the largest folders.

Storage: files live in a dedicated per-OS data folder by default; set `DS_STORAGE_ROOT` to move
them, and `DS_MOUNTS="name=/abs/path;other=/abs/path"` to expose more server folders to sync with.

Security notes: passwords are hashed, sessions are signed, devices use revocable tokens, login
attempts are rate limited, paths cannot escape their root. Always serve over HTTPS on the internet.

### Multi-factor authentication

The server provides optional admin multi-factor authentication through its API: authenticator-app
codes (TOTP), SMS through Twilio or a webhook, e-mail through SMTP, and one-time recovery codes.
Configure delivery credentials with the `sms_twilio_*`, `sms_webhook_*`, or `smtp_*` server settings;
manage factors through the `/api/admin/mfa/...` endpoints. SMS and e-mail sign-in codes use
`/api/auth/mfa/send`.

The server enforces a second factor on password-based logins when MFA is enabled. The included web
(and Android), desktop, CLI, and Python password-login flows do not yet prompt for or submit that
second factor, so do not enable MFA for accounts that rely on those flows. Existing device-token
authentication is unaffected.

### Backups and operations

Back up both file data and server configuration. Configuration contains settings, device tokens,
the signing key, and MFA state; losing it can invalidate sessions and tokens. The Linux installer
stores data under `/var/lib/domshon` and environment settings in `/etc/domshon/domshon.env`;
Windows uses `%ProgramData%\Domshon`; Docker stores `/data/config` and `/data/files` in the
`domshon-data` volume. The configured storage root may be elsewhere. Stop the service/container or
use a consistent filesystem snapshot while backing up. Test restoration before relying on backups.
`docker compose down` preserves named volumes; `docker compose down -v` deletes them.

For common issues, check `domshon service status` and `domshon service logs` (or `docker compose
ps` and `docker compose logs`). Verify `/api/health`, DNS A/AAAA records, NAT/firewall rules, and
ports 80/443 before debugging the client. Check `DS_MAX_UPLOAD_MB`, `DS_QUOTA_MB`,
`DS_MIN_FREE_MB`, `DS_BLOCKED_EXTENSIONS`, `DS_USER_WRITES`, and disk space if an upload is
rejected. If a setting appears unchanged, `settings.json` overrides `.env`; use `domshon server
show`. Restart after changing `DS_HOST`, `DS_PORT`, `DS_TRUST_PROXY`, or `DS_SECRET_KEY`.

## Using the apps

```bash
domshon login --server https://your.domain        # or: --token dst_...
domshon ls | put FILE_OR_DIR [REMOTE] | get REMOTE [LOCAL] | mkdir | mv | rm
domshon sync add work ~/Work --path projects/alpha     # local folder <-> server folder
domshon sync run          # one pass;  `domshon daemon` keeps syncing in the background
domshon gui               # desktop app;  `domshon tray --minimized` for the tray only
domshon autostart enable  # launch the tray app at login (add --server for the server)
```

**Settings and Account buttons.** The web app and the desktop app both have a **Settings** and an
**Account** button in the top bar (the CLI equivalents are `domshon login | logout | status`).
*Account* shows who you are and which server you are on (desktop: server URL, sign in/out, device
token; web: also the admin password change and logout). *Settings* holds per-computer or
per-browser preferences (confirm before delete, date format, parallel uploads, tray notifications,
launch on boot) and, for admins, a shortcut to the server-wide **Admin** panel (live settings,
device tokens, secret rotation).

Sync is two-way for files and folders. Conflicting edits keep the newest copy and save the other
as `name.conflict-<time>.ext`. The default local folder is `~/Domshon`; change it per sync
profile, and choose which server folder each profile syncs with.

## Android app

The Android app is a native WebView shell around the server's web app, not an independent sync
engine. It lets you choose a server address, use the web file manager and admin panel, select files
for upload, and download files through Android's DownloadManager. Native background folder sync is
not included; see `TODO.md` for deferred Android work.

Build an APK with JDK 17, the Android SDK, and Gradle installed:

```bash
bash android/build-apk.sh
bash android/build-apk.sh https://files.example.com  # optional default server address
```

The script writes `dist/domshon-<version>-android.apk`. Without a keystore configured, the release
APK is signed with the debug key for sideloading; provide the `DS_KEYSTORE_FILE`,
`DS_KEYSTORE_PASSWORD`, `DS_KEY_ALIAS`, and `DS_KEY_PASSWORD` environment variables for your own
release signing.

## Desktop app (Windows and Linux)

```bash
pip install -e ".[gui]"        # or: pip install "domshon[gui]"
domshon-gui                    # opens the app; the first run also creates its launcher
```

The first run creates an application-menu entry with the Domshon icon (Linux: `domshon.desktop` + icons
under `~/.local/share`; Windows: a Start Menu shortcut - right-click it to pin to the taskbar) and gives the
window its own taskbar identity, so the taskbar/dock shows the Domshon icon and not Python's. The app keeps
running in the **system tray** when you close the window. Manage the launcher yourself with
`domshon desktop install | uninstall | status`. Launch at login: `domshon autostart enable`.

A plain `pip install -e .` (no `[gui]`) is for the command line and the Python package only - on a new
machine or a server you do not need Qt. `[dev]` and `[build]` are for working on Domshon itself.

**macOS (best effort, not a primary target).** Everything above works on macOS 11+ with Python 3.10+:
`pip install "domshon[gui]"`, then `domshon-gui`. The first run creates `~/Applications/Domshon.app`
(Launchpad / Spotlight; drag it to the Dock; manage it with `domshon desktop install | uninstall | status`).
`domshon autostart enable` adds a LaunchAgent so the tray app starts at login (menu-bar icon), and
`domshon service install` runs the server as a LaunchAgent (starts when you log in, restarts on failure;
logs in `~/Library/Logs/Domshon`). `bash scripts/pypimgr/pypimgr.sh` works with the stock macOS bash.
Release builds include an Apple Silicon `Domshon.app` zip. It is **unsigned**: the first time, right-click
the app and choose *Open*, or run `xattr -dr com.apple.quarantine Domshon.app`. For a server that
must run before anyone logs in, use a Linux machine.

Linux notes (Ubuntu): Qt needs `sudo apt install libxcb-cursor0 libegl1`; GNOME shows tray icons only with the
*AppIndicator and KStatusNotifierItem* extension (`sudo apt install gnome-shell-extension-appindicator`).

## Building the applications

```bash
python scripts/build.py --package                        # dist/domshon, dist/domshon-gui + archive
python scripts/build.py --server https://your.domain     # bake your server address in as the default
```

Build on the OS you are targeting. Pushing a tag such as `v0.1.0` makes GitHub Actions build the
Windows and Linux apps, attach them to a GitHub Release, publish the Python package and push the
server image (`.github/workflows/release.yml`). Set the repository variable `DS_BUILD_SERVER` to
bake your address into the released apps.

## Python package

```python
import domshon

cloud = domshon.connect("https://your.domain", token="dst_...")   # or set DS_SERVER / DS_TOKEN
cloud.pull("projects/alpha", "./workdir")          # get your progress on another device
cloud.push("./workdir/results", "projects/alpha/results")
cloud.sync("./workdir", "projects/alpha")          # two-way
watcher = cloud.watch("./workdir", "projects/alpha", interval=30)   # background; watcher.stop()
cloud.write_text("notes/todo.txt", "hello"); print(cloud.read_text("notes/todo.txt"))
```

Create a token for each device with `domshon server token add NAME`; revoke it anytime.

### Publish the Python package

Package versions must be unique on PyPI. Update the version in both `pyproject.toml` and
`src/domshon/__init__.py`, then build and check the wheel and source distribution:

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
```

Upload to TestPyPI first, using a TestPyPI API token when prompted (username `__token__`):

```bash
python -m twine upload --repository testpypi dist/*
```

Then upload to PyPI with a PyPI API token:

```bash
python -m twine upload --repository pypi dist/*
```

The interactive managers also provide these steps: `bash scripts/pypimgr/pypimgr.sh pypi` on
Linux/macOS, or `scripts\pypimgr\pypimgr.bat pypi` on Windows. Do not commit API tokens. Review
the package metadata and `LICENSE` before publishing publicly.

### Build and publish application releases

Build desktop and CLI packages on the operating system you are targeting:

```bash
python -m pip install -e ".[gui,build]"
python scripts/build.py --package
python scripts/build.py --server https://files.example.com --package
```

Build an Android APK with JDK 17, Android SDK tools, and Gradle:

```bash
bash android/build-apk.sh
bash android/build-apk.sh https://files.example.com
```

The GitHub Actions workflow builds desktop/CLI archives for Windows and Linux, best-effort macOS
and Linux ARM builds, and a best-effort Android APK. To publish, configure a PyPI Trusted Publisher
for this repository, workflow `release.yml`, and environment `pypi`. Optional repository variable
`DS_BUILD_SERVER` bakes a default server URL into client apps. Android signing secrets are
`ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and
`ANDROID_KEY_PASSWORD`; without them the APK is debug-signed. Create and push a version tag after
updating the package version:

```bash
git tag v0.1.1
git push origin v0.1.1
```

Tag builds attach app downloads to the GitHub Release, publish the Python package to PyPI, and push
the server image to GitHub Container Registry. A manual **Run workflow** builds app artifacts only;
it does not publish to PyPI or create a release. Inspect all jobs and attached artifacts before
announcing a release. macOS builds are unsigned/not notarized unless handled separately.

## Tests and known limitations

Run the local checks with `python -m pip install -e ".[dev]"`, then `python -m compileall -q src
scripts packaging` and `pytest -q`. CI runs compile/import checks, focused Ruff checks, and tests on
Linux/macOS with Python 3.10 and 3.12. Tests cover server API authentication and file rules, quota
checks, config parsing, and service port handling; they are not a complete deployment or
cross-device integration test.

See [TODO.md](TODO.md) for deferred work and clean-machine verification gaps. Current limitations
include one admin account with device tokens rather than multiple users, global quotas, no
per-device folder permissions, polling rather than filesystem-watcher sync, no block-level delta
transfers or server trash/version history, no Android background folder sync, and incomplete MFA
support in password-login clients.

## Layout

```
src/domshon/       server/ (API, web UI, MFA)  client/ (API client, sync)  gui/  cli.py  remote.py  vault.py
android/           Kotlin WebView app and APK build script
scripts/           pypimgr/ menu managers, build.py, install-service.sh
packaging/         entry points for built executables
docker/            nginx templates; Dockerfile and docker-compose.yml at the repository root
.github/workflows/ CI and release automation
tests/             server and storage tests
```
