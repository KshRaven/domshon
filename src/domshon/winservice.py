"""Domshon server as a real Windows Service (pywin32).

Install the dependency once:   pip install "domshon[winservice]"
Then, from an elevated (Administrator) prompt:

    domshon service install        register + start (automatic, delayed start, restarts on failure)
    domshon service status | stop | start | restart | logs
    domshon service uninstall

The service keeps its settings and files in %ProgramData%\\Domshon (config\\, files\\, logs\\).
Run `domshon server passwd` from the same machine and it automatically uses that config folder.

A PyInstaller .exe cannot host this service (pywin32 needs the Python install); for the portable exe use
`domshon autostart enable --server` (Task Scheduler) instead.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
import threading
import time
from pathlib import Path

try:  # only available on Windows with pywin32 installed
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    HAVE_PYWIN32 = True
except ImportError:  # pragma: no cover - non-Windows or pywin32 missing
    HAVE_PYWIN32 = False

SERVICE_NAME = "DomshonServer"
DISPLAY_NAME = "Domshon Server"
TASK_NAME = "DomshonServer"
DESCRIPTION = "Self-hosted cloud file manager and sync server (Domshon)."


class PortInUseError(RuntimeError):
    pass


def program_data() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Domshon"


def log_path() -> Path:
    return program_data() / "logs" / "server.log"


def _service_settings(env_file: str | None = None, port_override: int | None = None) -> tuple[str, int]:
    from .config import Settings

    saved = {key: value for key, value in os.environ.items() if key.startswith("DS_")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ["DS_CONFIG_DIR"] = str(program_data() / "config")
        os.environ["DS_ENV_FILE"] = str(Path(env_file).resolve() if env_file else program_data() / ".no-env")
        settings = Settings()
        if port_override is not None:
            settings.set_internal("port", port_override)
        return settings.get("host"), int(settings.get("port"))
    finally:
        for key in (key for key in os.environ if key.startswith("DS_")):
            os.environ.pop(key, None)
        os.environ.update(saved)


def _port_in_use(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError:
        return False
    for family, socktype, proto, _, address in addresses:
        try:
            with socket.socket(family, socktype, proto) as probe:
                probe.bind(address)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                return True
    return False


def _port_conflict_message(host: str, port: int) -> str:
    return (f"Port {port} is currently in use on {host}; Domshon did not start.\n"
            "Stop the other server or choose another free port, for example:\n"
            "  domshon service install --port 8788\n"
            "Then update clients and any reverse-proxy upstream to use the new port.")


def boot(msg: str) -> None:
    """Append one line to logs\\boot.log. Written before anything can fail (no imports, never raises), so a
    service that dies right after launch still leaves a trace of how far it got."""
    try:
        d = program_data() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "boot.log", "a", encoding="utf-8") as f:
            f.write("%s pid=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:  # noqa: BLE001
        pass


boot("module loaded: python %s, exe %s" % (sys.version.split()[0], sys.executable))


def _setup_logging():
    import logging
    logdir = program_data() / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(logdir / "server.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return logging.getLogger("domshon.service")


def _tail(n: int = 25) -> str:
    out = []
    for f in (log_path(), log_path().with_name("boot.log")):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            out.append("--- %s\n%s" % (f, "\n".join(lines[-n:])))
        except OSError:
            out.append("--- %s (no log yet)" % f)
    return "\n".join(out)


def _restore_python_path() -> None:
    """A service runs as LocalSystem, which cannot see packages installed in a *user's* site-packages
    (pip falls back to that when the main one is not writable). `service install` records the installing
    interpreter's sys.path in the service's registry options; put it back before importing anything."""
    try:
        saved = win32serviceutil.GetServiceCustomOption(SERVICE_NAME, "pythonpath")
    except Exception:  # noqa: BLE001 - option not set
        saved = None
    for entry in (saved or "").split(os.pathsep):
        if entry and entry not in sys.path and os.path.isdir(entry):
            sys.path.append(entry)


class _LogStream:
    """pythonservice.exe has no console: sys.stdout / sys.stderr are None. Send them to the log file."""
    def __init__(self, logger, level):
        self._log, self._level = logger, level

    def write(self, text):
        text = (text or "").rstrip()
        if text:
            self._log.log(self._level, text)
        return len(text or "")

    def flush(self):
        pass

    def isatty(self):
        return False


if HAVE_PYWIN32:
    class DomshonService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._server = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._server is not None:
                self._server.should_exit = True
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self):
            boot("SvcDoRun entered")
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ""))
            log = _setup_logging()
            import logging
            sys.stdout = _LogStream(log, logging.INFO)
            sys.stderr = _LogStream(log, logging.ERROR)
            try:
                self._run(log)
            except PortInUseError as exc:
                boot(f"port conflict: {exc}")
                log.error("%s", exc)
                servicemanager.LogErrorMsg(str(exc))
            except BaseException:  # noqa: BLE001 - logged, then the service stops with an error
                import traceback
                boot("server crashed:\n" + traceback.format_exc())
                log.exception("server crashed")
                servicemanager.LogErrorMsg("Domshon server crashed - see %s" % log_path())
                raise
            finally:
                servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                                      (self._svc_name_, ""))

        def _run(self, log):
            boot("starting server")
            pd = program_data()
            os.chdir(pd)
            _restore_python_path()
            try:
                env_file = win32serviceutil.GetServiceCustomOption(SERVICE_NAME, "env_file")
            except Exception:  # noqa: BLE001 - option not set
                env_file = None
            if env_file:
                os.environ["DS_ENV_FILE"] = env_file
            os.environ.setdefault("DS_CONFIG_DIR", str(pd / "config"))
            os.environ.setdefault("DS_STORAGE_ROOT", str(pd / "files"))

            import uvicorn

            from domshon.config import Settings
            from domshon.server.app import create_app

            s = Settings()
            app = create_app(s)
            host, port = s.get("host"), int(s.get("port"))
            if _port_in_use(host, port):
                raise PortInUseError(_port_conflict_message(host, port))
            trust = bool(s.get("trust_proxy"))
            cfg = uvicorn.Config(app, host=host, port=port, log_config=None,
                                 proxy_headers=trust, forwarded_allow_ips="*" if trust else "127.0.0.1")
            self._server = uvicorn.Server(cfg)
            failure: list[BaseException] = []

            def serve():
                # uvicorn calls sys.exit() when it cannot bind the port; a bare thread would swallow that silently
                try:
                    self._server.run()
                except BaseException as exc:  # noqa: BLE001
                    failure.append(exc)
                    log.exception("uvicorn stopped with an error")

            t = threading.Thread(target=serve, name="uvicorn", daemon=True)
            t.start()
            for _ in range(100):           # wait up to ~10 s for the listener to come up
                if self._server.started or not t.is_alive():
                    break
                time.sleep(0.1)
            if not self._server.started:
                if _port_in_use(host, port):
                    raise PortInUseError(_port_conflict_message(host, port))
                raise RuntimeError("the server did not start (port %s in use?): %s"
                                   % (s.get("port"), failure[0] if failure else "see log above"))
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            log.info("Domshon server started on %s:%s", s.get("host"), s.get("port"))
            while t.is_alive():
                if win32event.WaitForSingleObject(self._stop_event, 1000) == win32event.WAIT_OBJECT_0:
                    break
            self._server.should_exit = True
            t.join(timeout=20)
            log.info("Domshon server stopped")


def _need_admin():
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise SystemExit("This needs an elevated prompt: open PowerShell or cmd 'as Administrator'.")


def control(action: str, env_file: str | None = None, no_start: bool = False,
            port: int | None = None) -> str:
    """Run a service action; Windows API errors become short messages instead of tracebacks."""
    try:
        return _control(action, env_file, no_start, port)
    except Exception as exc:  # noqa: BLE001 - pywintypes.error is not importable off Windows
        code = getattr(exc, "winerror", None)
        if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
            code = exc.args[0]
        if code == 1060:
            if action == "uninstall":
                return "Service/fallback task removed (nothing else was installed)."
            if action in ("start", "stop", "restart") and _task_exists():
                import subprocess
                if action in ("stop", "restart"):
                    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
                if action in ("start", "restart"):
                    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)
                return {"start": "started", "stop": "stopped", "restart": "restarted"}[action] + " (fallback task)"
            raise SystemExit("The service is not installed. Install it with:  domshon service install") from exc
        if code == 5:
            raise SystemExit("Access denied: open PowerShell 'as Administrator' and try again.") from exc
        if code == 1056 and action == "start":
            return "already running"
        if code == 1062 and action == "stop":
            return "already stopped"
        if type(exc).__name__ == "error" and code is not None:
            raise SystemExit(f"Windows service error {code}: {getattr(exc, 'strerror', exc)}") from exc
        raise


def _control(action: str, env_file: str | None = None, no_start: bool = False,
             port: int | None = None) -> str:
    if not sys.platform.startswith("win"):
        raise SystemExit("The Windows service wrapper only runs on Windows.")
    if not HAVE_PYWIN32:
        raise SystemExit('pywin32 is required for the Windows service.  Install it with:\n'
                         '    pip install "domshon[winservice]"')
    if getattr(sys, "frozen", False):
        raise SystemExit("The Windows service cannot be hosted by the packaged .exe. Install with pip "
                         "(pip install \"domshon[winservice]\") or use `domshon autostart enable --server`.")
    name = SERVICE_NAME
    if action not in ("status", "logs"):
        _need_admin()

    if action == "install":
        if port is not None and not 1 <= port <= 65535:
            raise SystemExit("Port must be between 1 and 65535.")
        pd = program_data()
        for d in ("config", "files", "logs"):
            (pd / d).mkdir(parents=True, exist_ok=True)
        import subprocess
        # config holds the signing key and device tokens: only SYSTEM (the service) and Administrators may read it
        subprocess.run(["icacls", str(pd / "config"), "/inheritance:r", "/grant:r",
                        "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"], capture_output=True)
        try:
            win32serviceutil.StopService(name)
        except Exception:  # noqa: BLE001 - not installed / not running
            pass
        cls = win32serviceutil.GetServiceClassString(DomshonService)
        try:
            win32serviceutil.InstallService(cls, name, DISPLAY_NAME, startType=win32service.SERVICE_AUTO_START,
                                            description=DESCRIPTION, delayedstart=True)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "winerror", None) != 1073:   # ERROR_SERVICE_EXISTS -> update it below
                raise SystemExit(f"Could not register the service: {exc}\n"
                                 "  If this mentions pythonservice.exe or a missing DLL, run once from an elevated prompt:\n"
                                 "      python -m pywin32_postinstall -install\n"
                                 "  then repeat `domshon service install`.") from exc
            win32serviceutil.ChangeServiceConfig(cls, name, startType=win32service.SERVICE_AUTO_START,
                                                 displayName=DISPLAY_NAME, description=DESCRIPTION)
        env = Path(env_file) if env_file else Path(".env")
        if env.is_file():
            win32serviceutil.SetServiceCustomOption(name, "env_file", str(env.resolve()))
        else:
            win32serviceutil.SetServiceCustomOption(name, "env_file", "")
        # let the LocalSystem service import the same packages this interpreter can (incl. user site-packages)
        win32serviceutil.SetServiceCustomOption(
            name, "pythonpath", os.pathsep.join(p for p in sys.path if p and os.path.isdir(p)))
        service_env = str(env.resolve()) if env.is_file() else None
        host, configured_port = _service_settings(service_env, port)
        subprocess.run(["sc", "failure", name, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/30000"],
                       capture_output=True)
        subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
        msg = (f"Service '{DISPLAY_NAME}' installed (automatic start, restarts on failure).\n"
               f"  data and logs : {pd}  (config\\ is readable by Administrators and the service only)\n"
               f"  settings file : {env.resolve() if env.is_file() else '(none - using defaults; settings.json via admin panel)'}\n"
               f"  first password: see {pd / 'logs' / 'server.log'} or run `domshon server passwd`")
        if not no_start:
            if _port_in_use(host, configured_port):
                return (msg + "\n\n" + _port_conflict_message(host, configured_port) +
                        "\nThe service remains installed but stopped.")
            try:
                win32serviceutil.StartService(name)
                state = _wait_state(name)
            except Exception as exc:  # noqa: BLE001
                boot("StartService failed: %r" % (exc,))
                state = "failed to start"
            if state != "running":
                if _port_in_use(host, configured_port):
                    return (msg + "\n\n" + _port_conflict_message(host, configured_port) +
                            "\nThe service remains installed but stopped.")
                tail = _tail(12)
                fb = _task_fallback(env)
                return (msg + f"\n\nThe service was installed but is {state}, not running.\n{tail}\n\n" + fb)
            msg += "\nService started and running."
        return msg
    if action == "uninstall":
        try:
            win32serviceutil.StopService(name)
        except Exception:  # noqa: BLE001
            pass
        import subprocess
        subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
        win32serviceutil.RemoveService(name)
        return f"Service removed. Your data in {program_data()} was kept."
    if action in ("start", "restart"):
        env_file = _registered_env_file()
        host, port = _service_settings(env_file)
        if action == "start" and _state(name) == "running":
            return "already running"
        if action == "restart":
            try:
                win32serviceutil.StopService(name)
                _wait_state(name, want="stopped")
            except Exception:  # noqa: BLE001 - was not running
                pass
        if _port_in_use(host, port):
            raise SystemExit(_port_conflict_message(host, port))
        win32serviceutil.StartService(name)
        state = _wait_state(name)
        if state != "running":
            raise SystemExit(f"The service is {state}, not running. Last lines of {log_path()}:\n{_tail()}")
        return "started" if action == "start" else "restarted"
    if action == "stop":
        win32serviceutil.StopService(name)
        return "stopped"
    if action == "logs":
        return f"{log_path()}\n{'-' * 60}\n{_tail(60)}"
    state = _state(name)
    if state in ("stopped", "not installed") and _task_exists():
        return ("running (fallback scheduled task)" if _task_running()
                else "fallback scheduled task installed but not running") + f"\n{_tail(12)}"
    if state == "stopped":
        state += f"\nLast lines of {log_path()}:\n{_tail(12)}"
    return state


def _registered_env_file() -> str | None:
    try:
        return win32serviceutil.GetServiceCustomOption(SERVICE_NAME, "env_file") or None
    except Exception:  # noqa: BLE001 - service option may not be configured
        return None


def _task_fallback(env: Path | None) -> str:
    """The service host (pythonservice.exe) could not run the server. Run it from Task Scheduler at boot
    instead (as SYSTEM, output appended to logs\\server.log) and start it now."""
    import subprocess
    pd = program_data()
    py = sys.executable
    if py.lower().endswith("pythonservice.exe"):
        py = sys.base_exec_prefix + r"\python.exe"
    envset = f'set "DS_ENV_FILE={env.resolve()}"&& ' if env and env.is_file() else ""
    inner = (f'set "DS_CONFIG_DIR={pd / "config"}"&& set "DS_STORAGE_ROOT={pd / "files"}"&& {envset}'
             f'"{py}" -m domshon server serve >> "{log_path()}" 2>&1')
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", f'cmd /c "{inner}"', "/SC", "ONSTART",
                        "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"], capture_output=True, text=True)
    if r.returncode:
        return ("Fallback failed too (" + (r.stderr or r.stdout).strip() + ").\n"
                "Run in this window to see the error:  python -m domshon server serve")
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)
    boot("fallback scheduled task created and started")
    return (f"FALLBACK ACTIVE: the server was registered as the scheduled task '{TASK_NAME}' (starts at boot, "
            f"runs as SYSTEM, logs to {log_path()}). It is starting now.\n"
            "Check:  domshon service status      Remove:  domshon service uninstall")


def _task_exists() -> bool:
    import subprocess
    return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True).returncode == 0


def _task_running() -> bool:
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True)
    return "Running" in (r.stdout or "")


def _state(name: str) -> str:
    try:
        code = win32serviceutil.QueryServiceStatus(name)[1]
    except Exception:  # noqa: BLE001
        return "not installed"
    names = {win32service.SERVICE_RUNNING: "running", win32service.SERVICE_STOPPED: "stopped",
             win32service.SERVICE_START_PENDING: "starting", win32service.SERVICE_STOP_PENDING: "stopping"}
    return names.get(code, f"state {code}")


def _wait_state(name: str, want: str = "running", timeout: float = 15.0) -> str:
    """Poll until the service reaches `want`; returns the last state seen. A service that is 'stopped'
    for three polls in a row while we wait for 'running' has crashed, so give up early."""
    end, stopped_polls, state = time.time() + timeout, 0, _state(name)
    while state != want and time.time() < end:
        stopped_polls = stopped_polls + 1 if state == "stopped" else 0
        if want == "running" and stopped_polls >= 3:
            break
        time.sleep(0.5)
        state = _state(name)
    return state


if __name__ == "__main__":  # python -m domshon.winservice install|start|stop|remove  (pywin32's own CLI)
    if not HAVE_PYWIN32:
        raise SystemExit('pip install "domshon[winservice]"')
    win32serviceutil.HandleCommandLine(DomshonService)
