"""Launch-on-boot / login for the tray app (target 'tray') or the server (target 'server').

Windows : tray -> HKCU Run key; server -> Task Scheduler ONSTART (needs an elevated shell)
Linux   : tray -> ~/.config/autostart .desktop; server -> systemd *user* unit
          (for a boot-time system service use scripts/install-service.sh)
macOS   : LaunchAgent plist
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

NAMES = {"tray": "Domshon", "server": "DomshonServer"}


def _python_exe(target: str) -> str:
    """pythonw.exe for the tray on Windows so no console window flashes at login."""
    exe = Path(sys.executable)
    if target == "tray" and sys.platform.startswith("win"):
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            return str(pyw)
    return str(exe)


def _command(target: str) -> list[str]:
    base = [sys.executable] if getattr(sys, "frozen", False) else [_python_exe(target), "-m", "domshon"]
    return base + (["tray", "--minimized"] if target == "tray" else ["server", "serve"])


def _env_file() -> str | None:
    f = os.environ.get("DS_ENV_FILE")
    p = Path(f) if f else Path(".env")
    return str(p.resolve()) if p.is_file() else None


# ---------------- Windows
def _win_enable(target):
    cmd = subprocess.list2cmdline(_command(target))
    if target == "tray":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, NAMES[target], 0, winreg.REG_SZ, cmd)
        return "Tray app will start at login."
    env = _env_file()
    tr = f'cmd /c "set DS_ENV_FILE={env}&& {cmd}"' if env else cmd
    r = subprocess.run(["schtasks", "/Create", "/TN", NAMES[target], "/TR", tr, "/SC", "ONSTART",
                        "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("Could not create scheduled task (run as Administrator): " + r.stderr.strip())
    return "Server scheduled to start at boot."


def _win_disable(target):
    if target == "tray":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, NAMES[target])
        except FileNotFoundError:
            pass
    else:
        subprocess.run(["schtasks", "/Delete", "/TN", NAMES[target], "/F"], capture_output=True)
    return "Disabled."


def _win_enabled(target):
    if target == "tray":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, NAMES[target])
                return True
        except FileNotFoundError:
            return False
    return subprocess.run(["schtasks", "/Query", "/TN", NAMES[target]], capture_output=True).returncode == 0


# ---------------- Linux
def _desktop_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "domshon.desktop"


def _unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "domshon-server.service"


def _quote(cmd):
    return " ".join('"' + c.replace('"', '\\"') + '"' if " " in c else c for c in cmd)


def _linux_enable(target):
    if target == "tray":
        p = _desktop_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[Desktop Entry]\nType=Application\nName=Domshon\n"
                     f"Exec={_quote(_command('tray'))}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n"
                     "Comment=Domshon sync and file manager\n")
        return f"Autostart entry written: {p}"
    u = _unit_path()
    u.parent.mkdir(parents=True, exist_ok=True)
    env = _env_file()
    u.write_text("[Unit]\nDescription=Domshon server\nAfter=network-online.target\n\n[Service]\n"
                 f"ExecStart={_quote(_command('server'))}\nRestart=on-failure\n"
                 + (f"Environment=DS_ENV_FILE={env}\n" if env else "") +
                 "\n[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "--now", "domshon-server.service"])
    return ("User service enabled. To start at BOOT without logging in run: "
            "sudo loginctl enable-linger $USER   (or use scripts/install-service.sh)")


def _linux_disable(target):
    if target == "tray":
        _desktop_path().unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", "domshon-server.service"])
        _unit_path().unlink(missing_ok=True)
    return "Disabled."


def _linux_enabled(target):
    return _desktop_path().exists() if target == "tray" else _unit_path().exists()


# ---------------- macOS
def _plist_path(target):
    return Path.home() / "Library" / "LaunchAgents" / f"com.domshon.{target}.plist"


def _mac_enable(target):
    p = _plist_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": f"com.domshon.{target}", "ProgramArguments": _command(target), "RunAtLoad": True,
            "KeepAlive": target == "server"}
    if _env_file():
        data["EnvironmentVariables"] = {"DS_ENV_FILE": _env_file()}
    logs = Path.home() / "Library" / "Logs" / "Domshon"
    logs.mkdir(parents=True, exist_ok=True)
    data["StandardOutPath"] = str(logs / f"{target}.log")
    data["StandardErrorPath"] = str(logs / f"{target}.err.log")
    p.write_bytes(plistlib.dumps(data))
    subprocess.run(["launchctl", "load", "-w", str(p)])
    return f"LaunchAgent installed: {p} (logs: ~/Library/Logs/Domshon)"


def _mac_disable(target):
    p = _plist_path(target)
    subprocess.run(["launchctl", "unload", "-w", str(p)], capture_output=True)
    p.unlink(missing_ok=True)
    return "Disabled."


# ---------------- public API
def enable(target: str = "tray") -> str:
    if sys.platform.startswith("win"):
        return _win_enable(target)
    return _mac_enable(target) if sys.platform == "darwin" else _linux_enable(target)


def disable(target: str = "tray") -> str:
    if sys.platform.startswith("win"):
        return _win_disable(target)
    return _mac_disable(target) if sys.platform == "darwin" else _linux_disable(target)


def is_enabled(target: str = "tray") -> bool:
    if sys.platform.startswith("win"):
        return _win_enabled(target)
    return _plist_path(target).exists() if sys.platform == "darwin" else _linux_enabled(target)
