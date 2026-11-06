#!/usr/bin/env python3
"""Build the standalone applications with PyInstaller (run on the OS you are targeting).

  python scripts/build.py                          build console + desktop apps into dist/
  python scripts/build.py --server https://files.example.com
                                                   bake a default server address into the apps
  python scripts/build.py --package                also create dist/domshon-<ver>-<os>-<arch>.zip|.tar.gz
  python scripts/build.py --only gui|cli           build just one of them

The server address can also come from the DS_BUILD_SERVER environment variable.
It is only a default: users can still change it in the app or with DS_SERVER.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PKG = SRC / "domshon"
DEFAULTS = PKG / "_build_defaults.json"
SEP = os.pathsep


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (PKG / "__init__.py").read_text("utf-8"))
    return m.group(1) if m else "0.0.0"


def target_tag() -> str:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), platform.system().lower())
    arch = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(), platform.machine().lower())
    return f"{system}-{arch}"


def make_icon_file() -> Path | None:
    """Draw the app icon with the same code the running app uses; .ico on Windows, .icns on macOS."""
    system = platform.system()
    if system not in ("Windows", "Darwin"):
        return None                      # Linux executables carry no icon; the launcher installs PNGs
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, str(SRC))
        from PySide6.QtGui import QGuiApplication
        _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
        from domshon.gui import icon
        out = ROOT / "build"
        out.mkdir(exist_ok=True)
        if system == "Windows":
            path = out / "domshon.ico"
            path.write_bytes(icon.ico_bytes())
            return path
        iconset = out / "domshon.iconset"
        shutil.rmtree(iconset, ignore_errors=True)
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            (iconset / f"icon_{base}x{base}.png").write_bytes(icon.png_bytes(base))
            (iconset / f"icon_{base}x{base}@2x.png").write_bytes(icon.png_bytes(base * 2))
        path = out / "domshon.icns"
        subprocess.run(["iconutil", "-c", "icns", "-o", str(path), str(iconset)], check=True)
        return path
    except Exception as exc:  # noqa: BLE001 - an icon is a nicety, never fail the build for it
        print(f"(icon not generated: {exc})")
        return None


def pyinstaller(name: str, entry: Path, windowed: bool, exclude: list[str]) -> None:
    mac_app = windowed and platform.system() == "Darwin"   # macOS GUI = a real Domshon.app bundle (onedir)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir" if mac_app else "--onefile",
        "--name", name,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / name),
        "--specpath", str(ROOT / "build"),
        "--paths", str(SRC),
        "--collect-submodules", "domshon",
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "anyio",
        "--add-data", f"{PKG / 'server' / 'web'}{SEP}domshon/server/web",
    ]
    if DEFAULTS.exists():
        cmd += ["--add-data", f"{DEFAULTS}{SEP}domshon"]
    if windowed:
        cmd.append("--windowed")
    if mac_app:
        cmd += ["--osx-bundle-identifier", "com.domshon.desktop"]
    ico = make_icon_file() if windowed else None
    if ico:
        cmd += ["--icon", str(ico)]
    for m in exclude:
        cmd += ["--exclude-module", m]
    cmd.append(str(entry))
    print("\n>>", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def package(names: list[str]) -> list[Path]:
    mac = platform.system() == "Darwin"
    exe = ".exe" if platform.system() == "Windows" else ""
    base = f"domshon-{version()}-{target_tag()}"
    out_dir = ROOT / "dist"
    outputs: list[Path] = []
    if mac and "domshon-gui" in names:   # the .app is zipped with ditto (keeps permissions and symlinks)
        app_zip = out_dir / f"{base}-app.zip"
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(out_dir / "domshon-gui.app"), str(app_zip)], check=True)
        outputs.append(app_zip)
        names = [n for n in names if n != "domshon-gui"]
    if not names:
        return outputs
    files = [out_dir / (n + exe) for n in names]
    extras = [ROOT / "README.md", ROOT / ".env.example"]
    if platform.system() == "Windows":
        out = out_dir / f"{base}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files + [e for e in extras if e.exists()]:
                z.write(f, f"{base}/{f.name}")
        outputs.append(out)
        return outputs
    else:
        out = out_dir / f"{base}.tar.gz"
        with tarfile.open(out, "w:gz") as t:
            for f in files + [e for e in extras if e.exists()]:
                t.add(f, f"{base}/{f.name}")
    outputs.append(out)
    return outputs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("DS_BUILD_SERVER", ""), help="default server URL to bake in")
    ap.add_argument("--only", choices=["cli", "gui"])
    ap.add_argument("--package", action="store_true", help="also create a release archive")
    ap.add_argument("--android", action="store_true", help="build the Android APK instead (needs JDK 17 + Android SDK + Gradle)")
    a = ap.parse_args()

    if a.android:
        script = ROOT / "android" / "build-apk.sh"
        if not shutil.which("bash") or not script.is_file():
            print("Run android/build-apk.sh from a bash shell (Linux, macOS, WSL or Git Bash).", file=sys.stderr)
            return 1
        return subprocess.run(["bash", str(script)] + ([a.server] if a.server else []), cwd=ROOT / "android").returncode
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller missing. Run: pip install -r requirements-build.txt", file=sys.stderr)
        return 1

    baked = False
    if a.server:
        DEFAULTS.write_text(json.dumps({"server_url": a.server.rstrip("/")}), "utf-8")
        baked = True
        print(f"Baking default server address: {a.server.rstrip('/')}")
    elif DEFAULTS.exists():
        DEFAULTS.unlink()  # never ship a stale address from an earlier build
    try:
        names: list[str] = []
        if a.only != "gui":
            pyinstaller("domshon", ROOT / "packaging" / "launcher.py", False, ["PySide6", "shiboken6"])
            names.append("domshon")
        if a.only != "cli":
            pyinstaller("domshon-gui", ROOT / "packaging" / "launcher_gui.py", True, [])
            names.append("domshon-gui")
    finally:
        if baked:
            DEFAULTS.unlink(missing_ok=True)
    if a.package:
        print("\nPackage:", ", ".join(str(p) for p in package(names)))
    print("\nDone. Output in:", ROOT / "dist")
    if "domshon-gui" in names:
        print("\nTip: start domshon-gui once and it adds itself to the Start Menu / application menu and the\n"
              "desktop (Windows and Linux). To do it without opening the window:  domshon desktop install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
