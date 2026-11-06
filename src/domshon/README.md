# Domshon Python Client

Domshon lets Python projects and command-line tools store, retrieve, and synchronize files with a
Domshon server that you control. The `domshon` package includes the Python client, CLI, and server;
desktop dependencies are optional.

## Install

```bash
python -m pip install domshon
```

Python 3.10 or later is required. Optional extras:

```bash
python -m pip install "domshon[gui]"         # desktop app
python -m pip install "domshon[winservice]"  # Windows Service support
```

For server deployment, Docker, desktop, and development instructions, see the
[full repository guide](https://github.com/KshRaven/domshon/blob/main/README.md).

## Connect your project

Create a separate device token on the server for each application or computer:

```bash
domshon server token add my-project
```

The token is displayed once. Store it as a secret, not in source control. Configure the client with
environment variables:

```bash
# Linux/macOS
export DS_SERVER="https://files.example.com"
export DS_TOKEN="dst_YOUR_DEVICE_TOKEN"
```

```powershell
# Windows PowerShell
$env:DS_SERVER = 'https://files.example.com'
$env:DS_TOKEN = 'dst_YOUR_DEVICE_TOKEN'
```

Then use the Python API:

```python
import domshon

with domshon.connect() as cloud:
    cloud.pull("projects/analysis", "./workdir")
    cloud.push("./workdir/results", "projects/analysis/results")
    result = cloud.sync("./workdir", "projects/analysis")
    print(result.summary())
```

You can also pass connection details directly:

```python
with domshon.connect(
    "https://files.example.com",
    token="dst_YOUR_DEVICE_TOKEN",
) as cloud:
    cloud.write_text("projects/analysis/status.txt", "complete")
    print(cloud.read_text("projects/analysis/status.txt"))
```

`domshon.connect()` resolves values in this order: explicit arguments, `DS_SERVER` / `DS_TOKEN`,
then the local login saved by `domshon login`. The default server address is
`http://localhost:8787`. Use HTTPS for remote servers. A saved CLI login is local to the current
user profile; set `DS_CLIENT_DIR` to use a different client configuration directory.

## Python API

`import domshon` exposes these main names:

| Name | Use |
|---|---|
| `domshon.connect(server=None, token=None, user=None, password=None, root="main")` | Create a client. Prefer a device token over an admin password. |
| `domshon.Remote` | Client type returned by `connect()`. |
| `domshon.Watcher` | Background polling handle returned by `watch()`. |
| `domshon.SyncResult` | Counts uploaded/downloaded/deleted files, conflicts, and errors. |
| `domshon.ApiError` | HTTP/API error, with `status` and `detail` attributes. |
| `domshon.RemoteError` | Local configuration or sync safety error. |

Common operations include:

```python
with domshon.connect() as cloud:
    print(cloud.me())
    print(cloud.roots())
    print(cloud.ls("projects"))
    cloud.mkdir("projects/new-run")
    cloud.upload_file("./report.csv", "projects/new-run/report.csv")
    cloud.download_file("projects/new-run/report.csv", "./downloaded.csv")

    cloud.write_bytes("projects/new-run/data.bin", b"sample")
    raw = cloud.read_bytes("projects/new-run/data.bin")
    cloud.write_text("projects/new-run/notes.txt", "hello")
    text = cloud.read_text("projects/new-run/notes.txt")

    # One-way folder transfers. mirror=True deletes destination-only files.
    cloud.push("./results", "projects/new-run/results")
    cloud.pull("projects/new-run/results", "./downloaded-results")

    # Poll continuously in a background thread; call stop() when finished.
    watcher = cloud.watch("./workdir", "projects/analysis", interval=30)
    watcher.stop()
```

Methods that accept `root=` can target a named server root exposed by the administrator through
`DS_MOUNTS`; the default root is `main`. Folder synchronization is polling-based. It keeps both
sides' changes and records conflicts as `*.conflict-*`. Review `SyncResult.errors`, and use
`mirror=True` only when you intend to remove files that exist only at the destination.

Password-based login is available with `domshon.connect(server=..., user=..., password=...)` or
`DS_ADMIN_USER` and `DS_ADMIN_PASSWORD`. The password is not saved by the client, but device tokens
are preferable. Included password-login clients do not currently submit MFA codes.

## Command line

The package installs both `domshon` and its alias `ds`:

```bash
domshon --help
domshon --version
```

Connect a device, browse, and transfer files:

```bash
domshon login --server https://files.example.com --token "dst_YOUR_DEVICE_TOKEN"
domshon status
domshon ls
domshon put ./report.csv projects/analysis
domshon get projects/analysis/report.csv ./downloaded.csv
domshon logout
```

`put` uploads a file or directory recursively; `get` downloads a file or a folder archive. Other
file commands include `mkdir`, `mv`, and `rm`. Use `--root NAME` on file commands for a named
server root.

Manage saved sync profiles:

```bash
domshon sync add analysis ./workdir --path projects/analysis --interval 30
domshon sync list
domshon sync run analysis
domshon daemon
```

The daemon keeps enabled profiles synchronized until stopped with `Ctrl+C`. Every command and
subcommand has detailed help:

```bash
domshon put --help
domshon sync add --help
```

## Server commands

Commands on the server host administer its settings and device tokens:

```bash
domshon server show
domshon server passwd
domshon server token add my-project
domshon server token list
domshon server usage
domshon server serve
```

Use `domshon admin --help` for remote administration. Never expose a plain HTTP server directly to
the public internet; use HTTPS through a trusted reverse proxy.
