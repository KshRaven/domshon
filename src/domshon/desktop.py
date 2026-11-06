"""Desktop integration: application-menu / Start-Menu launcher with the Domshon icon.

  Linux   ~/.local/share/applications/domshon.desktop + icons in ~/.local/share/icons/hicolor
  Windows Start Menu shortcut (Programs\\Domshon.lnk, optional Desktop shortcut) with a generated .ico
  macOS   ~/Applications/Domshon.app (a small launcher bundle; best effort) -> Launchpad / Spotlight / Dock

The launcher is created automatically the first time the desktop app runs, and can be managed with
`domshon desktop install | uninstall | status`. Needs the desktop extra (PySide6) to draw the icon.
"""
from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from platformdirs import user_data_dir

from . import APP_NAME, __version__

LINUX_SIZES = (16, 24, 32, 48, 64, 128, 256)
AUMID = "Domshon.Desktop"      # Windows taskbar grouping id (also set by the running app)
DESKTOP_FILE = "domshon.desktop"


def _xdg_data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def launch_command() -> list[str]:
    """The command that starts the desktop app, without a console window where possible."""
    if getattr(sys, "frozen", False):
        me = Path(sys.executable)
        if not me.stem.lower().endswith("-gui"):          # the console exe: point at its desktop sibling
            for cand in (me.with_name("domshon-gui" + me.suffix), me.with_name("domshon-gui")):
                if cand.is_file():
                    return [str(cand)]
        return [sys.executable]
    exe = Path(sys.executable)
    if sys.platform.startswith("win"):
        g = exe.parent / "domshon-gui.exe"
        if g.is_file():
            return [str(g)]
        pyw = exe.with_name("pythonw.exe")
        return [str(pyw if pyw.is_file() else exe), "-m", "domshon", "gui"]
    g = exe.parent / "domshon-gui"
    if g.is_file():
        return [str(g)]
    return [str(exe), "-m", "domshon", "gui"]


def _ensure_qt():
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # e.g. run over SSH
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError as e:
        raise SystemExit('The desktop launcher needs PySide6:  pip install "domshon[gui]"') from e
    return QGuiApplication.instance() or QGuiApplication(sys.argv[:1])


def _quote(cmd: list[str]) -> str:
    return " ".join('"' + c.replace("\\", "\\\\").replace('"', '\\"') + '"' if " " in c else c for c in cmd)


# ------------------------------------------------------------------ Linux
def _linux_install(desktop_shortcut: bool) -> str:
    _ensure_qt()
    from .gui import icon
    base = _xdg_data()
    for s in LINUX_SIZES:
        d = base / "icons" / "hicolor" / f"{s}x{s}" / "apps"
        d.mkdir(parents=True, exist_ok=True)
        (d / "domshon.png").write_bytes(icon.png_bytes(s))
    apps = base / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = ("[Desktop Entry]\nType=Application\nVersion=1.0\nName=Domshon\nGenericName=Cloud file manager\n"
             "Comment=Self-hosted cloud files and folder sync\n"
             f"Exec={_quote(launch_command())}\nIcon=domshon\nTerminal=false\n"
             "Categories=Network;FileTransfer;Utility;\nStartupNotify=true\nStartupWMClass=Domshon\n"
             "Keywords=cloud;files;sync;backup;\n")
    (apps / DESKTOP_FILE).write_text(entry, "utf-8")
    if desktop_shortcut:
        dd = Path(os.environ.get("XDG_DESKTOP_DIR", ""))  if os.environ.get("XDG_DESKTOP_DIR") else Path.home() / "Desktop"
        if shutil.which("xdg-user-dir"):
            r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                dd = Path(r.stdout.strip())
        if dd.is_dir():
            f = dd / DESKTOP_FILE
            f.write_text(entry, "utf-8")
            os.chmod(f, 0o755)
            if shutil.which("gio"):       # GNOME needs this to let the icon launch ("Allow Launching")
                subprocess.run(["gio", "set", str(f), "metadata::trusted", "true"], capture_output=True)
    for cmd in (["update-desktop-database", str(apps)],
                ["gtk-update-icon-cache", "-f", "-t", str(base / "icons" / "hicolor")]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)
    return f"Launcher installed: {apps / DESKTOP_FILE} (look for 'Domshon' in your application menu)"


def _linux_uninstall() -> str:
    base = _xdg_data()
    (base / "applications" / DESKTOP_FILE).unlink(missing_ok=True)
    (Path.home() / "Desktop" / DESKTOP_FILE).unlink(missing_ok=True)
    for s in LINUX_SIZES:
        (base / "icons" / "hicolor" / f"{s}x{s}" / "apps" / "domshon.png").unlink(missing_ok=True)
    return "Launcher removed."


def _linux_installed() -> bool:
    return (_xdg_data() / "applications" / DESKTOP_FILE).is_file()


# ------------------------------------------------------------------ Windows
def _win_dirs() -> tuple[Path, Path]:
    start = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    desk = Path.home() / "Desktop"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "[Environment]::GetFolderPath('Desktop')"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            desk = Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return start / f"{APP_NAME}.lnk", desk / f"{APP_NAME}.lnk"


def _win_icon_path() -> Path:
    return Path(user_data_dir(APP_NAME)) / "icons" / "domshon.ico"


def _win_shortcut(lnk: Path, cmd: list[str], ico: Path):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:DS_LNK);"
          "$s.TargetPath=$env:DS_TARGET;$s.Arguments=$env:DS_ARGS;$s.WorkingDirectory=$env:USERPROFILE;"
          "$s.IconLocation=$env:DS_ICO;$s.Description='Domshon cloud file manager';$s.Save()")
    env = dict(os.environ, DS_LNK=str(lnk), DS_TARGET=cmd[0], DS_ARGS=subprocess.list2cmdline(cmd[1:]), DS_ICO=str(ico))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("Could not create the shortcut: " + (r.stderr or r.stdout).strip())


def _win_install(desktop_shortcut: bool) -> str:
    _ensure_qt()
    from .gui import icon
    ico = _win_icon_path()
    ico.parent.mkdir(parents=True, exist_ok=True)
    ico.write_bytes(icon.ico_bytes())
    start, desk = _win_dirs()
    start.parent.mkdir(parents=True, exist_ok=True)
    cmd = launch_command()
    _win_shortcut(start, cmd, ico)
    if desktop_shortcut:
        _win_shortcut(desk, cmd, ico)
    extra = f" and Desktop shortcut: {desk}" if desktop_shortcut else ""
    return f"Start Menu shortcut created: {start}{extra} (search 'Domshon'; right-click it to pin to the taskbar)"


def _win_uninstall() -> str:
    start, desk = _win_dirs()
    for f in (start, desk, _win_icon_path()):
        f.unlink(missing_ok=True)
    return "Shortcuts removed."


# ------------------------------------------------------------------ macOS
def _mac_app_path() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def _mac_install() -> str:
    """A tiny .app that starts the GUI, so Domshon shows up in Launchpad / Spotlight and can be kept in the Dock."""
    _ensure_qt()
    from .gui import icon
    app = _mac_app_path()
    macos, res = app / "Contents" / "MacOS", app / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    script = macos / APP_NAME
    script.write_text("#!/bin/bash\nexec " + " ".join(shlex.quote(c) for c in launch_command()) + ' "$@"\n', "utf-8")
    os.chmod(script, 0o755)
    info = {"CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleIdentifier": "com.domshon.desktop",
            "CFBundleExecutable": APP_NAME, "CFBundlePackageType": "APPL", "CFBundleVersion": __version__,
            "CFBundleShortVersionString": __version__, "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True}
    note = ""
    if shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "domshon.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                (iconset / f"icon_{base}x{base}.png").write_bytes(icon.png_bytes(base))
                (iconset / f"icon_{base}x{base}@2x.png").write_bytes(icon.png_bytes(base * 2))
            r = subprocess.run(["iconutil", "-c", "icns", "-o", str(res / "domshon.icns"), str(iconset)],
                               capture_output=True, text=True)
        if r.returncode == 0:
            info["CFBundleIconFile"] = "domshon"
        else:
            note = " (icon not created)"
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return f"App created: {app}{note} - find 'Domshon' in Launchpad or Spotlight; drag it to the Dock to keep it there"


def _mac_uninstall() -> str:
    shutil.rmtree(_mac_app_path(), ignore_errors=True)
    return "Domshon.app removed."


# ------------------------------------------------------------------ public API
def _delegate_to_gui(desktop_shortcut: bool) -> str | None:
    """The packaged console exe has no Qt (needed to draw the icon): ask the sibling domshon-gui exe to do it."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        import PySide6  # noqa: F401
        return None
    except ImportError:
        pass
    target = launch_command()
    if Path(target[0]).resolve() == Path(sys.executable).resolve():
        return None
    args = [target[0], "desktop", "install"] + ([] if desktop_shortcut else ["--no-desktop-shortcut"])
    subprocess.run(args, capture_output=True, timeout=120)
    return "Launcher requested from domshon-gui (look for 'Domshon' in your Start Menu / application menu)."


def install(desktop_shortcut: bool = True) -> str:
    """Menu entry + (Windows and Linux) a desktop shortcut. Both are on by default."""
    delegated = _delegate_to_gui(desktop_shortcut)
    if delegated:
        return delegated
    if sys.platform.startswith("win"):
        return _win_install(desktop_shortcut)
    if sys.platform == "darwin":
        return _mac_install()
    return _linux_install(desktop_shortcut)


def uninstall() -> str:
    if sys.platform.startswith("win"):
        return _win_uninstall()
    if sys.platform == "darwin":
        return _mac_uninstall()
    return _linux_uninstall()


def is_installed() -> bool:
    if sys.platform.startswith("win"):
        try:
            return _win_dirs()[0].is_file()
        except KeyError:
            return False
    if sys.platform == "darwin":
        return (_mac_app_path() / "Contents" / "Info.plist").is_file()
    return _linux_installed()


def ensure_registered() -> str | None:
    """Create the launcher once per version/location (called by the GUI at start-up; never raises)."""
    if os.environ.get("DS_NO_DESKTOP_INTEGRATION"):
        return None
    try:
        from .client.config import client_config_dir
        marker = client_config_dir() / "desktop.json"
        want = {"version": __version__, "exec": launch_command()}
        try:
            if json.loads(marker.read_text("utf-8")) == want and is_installed():
                return None
        except (OSError, ValueError):
            pass
        msg = install(desktop_shortcut=True)
        marker.write_text(json.dumps(want), "utf-8")
        return msg
    except (Exception, SystemExit) as e:  # noqa: BLE001 - integration is a nicety; never block the app
        return f"(launcher not created: {e})"
