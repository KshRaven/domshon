"""Domshon terminal app: a full-screen, keyboard-driven menu for everything the CLI can do.

Run with plain `domshon` (or `ds`, or `domshon tui`).  Standard library only; works on Linux, macOS,
Windows (Terminal / PowerShell / cmd) and Android (Termux).  Every action is still available as a normal
command - see `domshon --help`.

Keys   Up/Down  move inside the page (wraps top <-> bottom)     Left/Right  previous / next page (wraps)
       Enter    run the highlighted option                       1-9  run that option      0 / Backspace  back
       Esc      back / cancel / terminate the running process    (Ctrl+C is treated like Esc)
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import APP_NAME, __version__
from .theme import TOKENS

PAGE = 9                     # options per page; key 0 is always "back"
IS_WIN = os.name == "nt"

# ------------------------------------------------------------------------------------ colours
C = {k: v for k, v in TOKENS.items() if isinstance(v, str) and v.startswith("#")}
NO_COLOR = bool(os.environ.get("NO_COLOR"))
TRUECOLOR = (os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
             or IS_WIN or bool(os.environ.get("WT_SESSION")))


def _rgb(hexcol: str) -> tuple[int, int, int]:
    h = hexcol.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _c256(r: int, g: int, b: int) -> int:
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


def sgr(fg: str | None = None, bg: str | None = None, bold: bool = False, dim: bool = False,
        rev: bool = False) -> str:
    if NO_COLOR:
        fg = bg = None
    parts = []
    if bold:
        parts.append("1")
    if dim:
        parts.append("2")
    if rev:
        parts.append("7")
    for col, base in ((fg, 38), (bg, 48)):
        if col:
            r, g, b = _rgb(C.get(col, col))
            parts.append(f"{base};2;{r};{g};{b}" if TRUECOLOR else f"{base};5;{_c256(r, g, b)}")
    return ("\x1b[" + ";".join(parts) + "m") if parts else ""


RESET = "\x1b[0m"
Seg = tuple  # (text, fg, bg, bold)


def gradient(text: str, stops=("cyan", "violet", "magenta")) -> str:
    """Colour a string left-to-right through the theme colours."""
    if NO_COLOR or not text:
        return text
    pts = [_rgb(C[s]) for s in stops]
    out, n = [], max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        t = i / n * (len(pts) - 1)
        a = min(int(t), len(pts) - 2)
        f = t - a
        r, g, b = (round(pts[a][k] + (pts[a + 1][k] - pts[a][k]) * f) for k in range(3))
        out.append((f"\x1b[38;2;{r};{g};{b}m" if TRUECOLOR else f"\x1b[38;5;{_c256(r, g, b)}m") + ch)
    return "".join(out) + RESET


# ------------------------------------------------------------------------------------ glyphs
def _utf_ok() -> bool:
    if os.environ.get("DS_ASCII"):
        return False
    if "utf" in (getattr(sys.stdout, "encoding", "") or "").lower():
        return True
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # type: ignore[attr-defined]
        return True
    except Exception:
        return False


UTF = _utf_ok()
G = dict(tl="╔", tr="╗", bl="╚", br="╝", h="═", v="║", ml="╠", mr="╣", sel="▶", on="●", off="○", up="↑", down="↓",
         left="←", right="→", sep="│", dir="▣", file="▤", ell="…") if UTF else \
    dict(tl="+", tr="+", bl="+", br="+", h="=", v="|", ml="+", mr="+", sel=">", on="*", off="o", up="^", down="v",
         left="<", right=">", sep="|", dir="[D]", file="   ", ell="...")

LOGO = ["╺┳┓┏━┓┏┳┓┏━┓╻ ╻┏━┓┏┓╻",
        " ┃┃┃ ┃┃┃┃┗━┓┣━┫┃ ┃┃┗┫",
        "╺┻┛┗━┛╹ ╹┗━┛╹ ╹┗━┛╹ ╹"]

# ------------------------------------------------------------------------------------ keys
UP, DOWN, LEFT, RIGHT, ENTER, ESC, BACK, PGUP, PGDN, HOME, END = (
    "UP", "DOWN", "LEFT", "RIGHT", "ENTER", "ESC", "BACKSPACE", "PGUP", "PGDN", "HOME", "END")


class Term:
    """Raw keyboard input + alternate screen, identical API on Windows and POSIX."""

    def __init__(self):
        self._old = None
        self.active = False
        self._dec = None
        if not IS_WIN:
            import codecs
            self._dec = codecs.getincrementaldecoder("utf-8")("replace")

    # ---- setup / teardown
    def start(self):
        if IS_WIN:
            try:
                import ctypes
                k = ctypes.windll.kernel32
                for h in (-11, -12):
                    hd = k.GetStdHandle(h)
                    mode = ctypes.c_uint32()
                    if k.GetConsoleMode(hd, ctypes.byref(mode)):
                        k.SetConsoleMode(hd, mode.value | 0x0004)      # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            except Exception:
                os.system("")
        else:
            import termios
            fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[0] &= ~(termios.ICRNL | termios.IXON | termios.INLCR)
            new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # type: ignore[attr-defined]
        except Exception:
            pass
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        self.active = True

    def stop(self):
        if not self.active:
            return
        sys.stdout.write(RESET + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if not IS_WIN and self._old is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
        self.active = False

    @contextlib.contextmanager
    def suspended(self):
        """Hand the real terminal back (cooked mode, normal screen) for prompts and plain output."""
        self.stop()
        try:
            yield
        finally:
            self.start()

    # ---- input
    def key(self, timeout: float | None = None) -> str | None:
        """Next key as a name (UP, ENTER, ESC, ...) or a single character; None on timeout."""
        try:
            return self._key_win(timeout) if IS_WIN else self._key_posix(timeout)
        except KeyboardInterrupt:
            return ESC

    def _key_win(self, timeout):
        import msvcrt
        end = None if timeout is None else time.time() + timeout
        while not msvcrt.kbhit():
            if end is not None and time.time() >= end:
                return None
            time.sleep(0.02)
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT, "I": PGUP, "Q": PGDN, "G": HOME, "O": END
                    }.get(msvcrt.getwch(), "?")
        return self._plain(ch)

    @staticmethod
    def _plain(ch):
        if ch in ("\r", "\n"):
            return ENTER
        if ch in ("\x1b", "\x03", "\x04"):
            return ESC
        if ch in ("\x7f", "\x08"):
            return BACK
        return ch

    def _read(self, timeout):
        import select
        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], timeout)[0]:
            return None
        return os.read(fd, 1)

    def _key_posix(self, timeout):
        b = self._read(timeout)
        if b is None:
            return None
        if b == b"\x1b":
            nxt = self._read(0.04)
            if nxt is None:
                return ESC
            if nxt in (b"[", b"O"):
                seq = b""
                while True:
                    c = self._read(0.04)
                    if c is None:
                        break
                    seq += c
                    if 0x40 <= c[0] <= 0x7e:
                        break
                s = seq.decode("ascii", "ignore")
                return {"A": UP, "B": DOWN, "C": RIGHT, "D": LEFT, "H": HOME, "F": END, "5~": PGUP, "6~": PGDN,
                        "1~": HOME, "7~": HOME, "4~": END, "8~": END}.get(s, "?")
            return ESC
        if b[0] < 0x80:
            return self._plain(b.decode())
        out = self._dec.decode(b)
        while not out:
            more = self._read(0.05)
            if more is None:
                break
            out = self._dec.decode(more)
        return out or "?"

    # ---- size / drawing
    @staticmethod
    def size() -> tuple[int, int]:
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines

    @staticmethod
    def draw(lines: list[str]):
        w, h = Term.size()
        buf = ["\x1b[H"]
        for i in range(h):
            buf.append((lines[i] if i < len(lines) else "") + "\x1b[K" + ("\r\n" if i < h - 1 else ""))
        sys.stdout.write("".join(buf) + RESET)
        sys.stdout.flush()


# ------------------------------------------------------------------------------------ frame builder
def clip(segs: list[Seg], width: int) -> list[Seg]:
    out, used = [], 0
    for seg in segs:
        text = seg[0]
        if used + len(text) > width:
            text = text[: max(width - used - 1, 0)] + (G["ell"] if width - used > 0 else "")
            out.append((text,) + tuple(seg[1:]))
            used = width
            break
        out.append(seg)
        used += len(text)
    return out


def paint(segs: list[Seg]) -> str:
    return "".join(sgr(s[1] if len(s) > 1 else None, s[2] if len(s) > 2 else None, bool(s[3]) if len(s) > 3 else False)
                   + s[0] + RESET for s in segs)


class Frame:
    """A bordered screen: logo, status row, titled body, footer."""

    def __init__(self, ui: "UI"):
        self.ui = ui
        cols, rows = Term.size()
        self.w = max(min(cols, 110), 44)
        self.inner = self.w - 2
        self.rows = rows
        self.compact = rows < 25
        self.lines: list[str] = []
        self.pad = max((cols - self.w) // 2, 0)

    def _edge(self, left, fill, right, col="cyan"):
        self.lines.append(" " * self.pad + paint([(left + fill * self.inner + right, col)]))

    def top(self):
        self._edge(G["tl"], G["h"], G["tr"])

    def mid(self):
        self._edge(G["ml"], G["h"], G["mr"])

    def bottom(self):
        self._edge(G["bl"], G["h"], G["br"])

    def row(self, segs: list[Seg], align: str = "left"):
        segs = clip(segs, self.inner - 2)
        used = sum(len(s[0]) for s in segs)
        space = self.inner - 2 - used
        lead = space // 2 if align == "center" else 0
        body = " " * lead + paint(segs) + " " * (space - lead)
        self.lines.append(" " * self.pad + paint([(G["v"], "cyan")]) + " " + body + " " + paint([(G["v"], "cyan")]))

    def blank(self):
        if not self.compact:
            self.row([("", None)])

    def logo(self):
        if self.compact:
            self.row([(f"{APP_NAME.upper()}  v{__version__}", "cyan", None, True)], "center")
        elif UTF and self.inner >= 30:
            for ln in LOGO:
                self.lines.append(" " * self.pad + paint([(G["v"], "cyan")]) + gradient(ln.center(self.inner))
                                  + paint([(G["v"], "cyan")]))
        else:
            self.row([(APP_NAME.upper(), "cyan", None, True)], "center")

    def status(self):
        st = self.ui.status
        state = st.get("state")
        dot, col, label = {"online": (G["on"], "ok", "ONLINE"), "offline": (G["off"], "danger", "OFFLINE"),
                           }.get(state, (G["off"], "yellow", "CHECKING"))
        segs: list[Seg] = [(f"{dot} {label}", col, None, True), (f" {G['sep']} ", "muted"),
                           (st.get("server", ""), "text"), (f" {G['sep']} ", "muted")]
        segs.append(("signed in", "ok") if st.get("login") else ("not signed in", "yellow"))
        if st.get("profiles") is not None:
            segs += [(f" {G['sep']} ", "muted"), (f"{st['profiles']} sync profile(s)", "muted")]
        if st.get("admin"):
            segs += [(f" {G['sep']} ", "muted"), ("admin", "magenta", None, True)]
        self.row(segs)

    def footer(self, hint: str):
        self.mid()
        toast = self.ui.toast
        if toast and time.time() - toast[2] < 8:
            self.row([(toast[0], {"ok": "ok", "err": "danger", "warn": "yellow"}.get(toast[1], "text"), None, True)])
        else:
            self.row([(hint, "muted")])
        self.bottom()

    def render(self):
        Term.draw(self.lines)


# ------------------------------------------------------------------------------------ menu model
@dataclass
class Item:
    label: str
    action: Callable[[], object] | None = None
    desc: str = ""
    tag: str = ""               # right-aligned note, e.g. "root", "live"
    color: str | None = None


@dataclass
class Menu:
    title: str
    items: Callable[[], list[Item]] | list[Item]
    hotkeys: dict = field(default_factory=dict)    # char -> (hint, callable)
    back_label: str = "Back"
    tick: Callable[[], bool] | None = None         # called on idle; return True to redraw/reload
    interval: float = 1.0
    index: int = 0

    def get_items(self) -> list[Item]:
        return self.items() if callable(self.items) else self.items


class UI:
    def __init__(self):
        self.term = Term()
        self.toast: tuple | None = None
        self.status: dict = {"state": "checking", "server": "", "login": False}
        self._status_lock = threading.Lock()
        self._stop = threading.Event()
        self.admin_client = None
        self.admin_creds: tuple[str, str] | None = None

    # ---- notifications
    def notify(self, text: str, kind: str = "ok"):
        self.toast = (text, kind, time.time())

    # ---- background status poller
    def poll_status(self):
        from .client.api import Client
        from .client.config import ClientConfig
        while not self._stop.is_set():
            try:
                cfg = ClientConfig.load()
                st = {"server": cfg.server_url, "login": bool(cfg.token), "profiles": len(cfg.profiles),
                      "admin": self.admin_client is not None}
                cl = Client(cfg.server_url)
                try:
                    cl.health()
                    st["state"] = "online"
                except Exception:
                    st["state"] = "offline"
                finally:
                    cl.close()
            except Exception:
                st = {"state": "offline", "server": "?", "login": False}
            with self._status_lock:
                self.status = st
            self._stop.wait(8)

    # ---- generic screens ------------------------------------------------------------------
    def run_menu(self, menu: Menu):
        """Paged, wrapping menu. Returns when the user goes back."""
        items = menu.get_items()
        last_tick = time.time()
        while True:
            psize = min(PAGE, max(Term.size()[1] - (10 if Term.size()[1] < 25 else 14), 3))
            pages = max((len(items) + psize - 1) // psize, 1)
            menu.index = min(menu.index, max(len(items) - 1, 0))
            page = menu.index // psize
            start = page * psize
            chunk = items[start:start + psize]
            pos = menu.index - start

            f = Frame(self)
            f.top()
            f.logo()
            f.mid()
            f.status()
            f.mid()
            f.row([(menu.title, "cyan", None, True), (f"   page {page + 1}/{pages}" if pages > 1 else "", "muted")])
            f.blank()
            width = f.inner - 6
            for i in range(psize):
                if i < len(chunk):
                    it = chunk[i]
                    sel = i == pos
                    mark = G["sel"] if sel else " "
                    fg = it.color or ("cyan" if sel else "text")
                    segs = [(f"{mark} ", "magenta", None, True), (f"{i + 1} ", "yellow", None, True),
                            (it.label, fg, None, sel)]
                    if it.desc:
                        segs.append((f"  {it.desc}", "muted"))
                    if it.tag:
                        pad = max(width - sum(len(s[0]) for s in segs) - len(it.tag), 1)
                        segs += [(" " * pad, None), (it.tag, "violet")]
                    f.row(segs)
                else:
                    f.blank()
            f.blank()
            f.row([("  0 ", "yellow", None, True), (menu.back_label, "muted")])
            hint = f"{G['up']}{G['down']} move  {G['left']}{G['right']} page  Enter/1-9 select  0/Esc back"
            for ch, (h, _) in menu.hotkeys.items():
                hint += f"  {ch}={h}"
            f.footer(hint)
            f.render()

            k = self.term.key(min(menu.interval, 1.0))
            if k is None:
                if menu.tick and time.time() - last_tick >= menu.interval:
                    last_tick = time.time()
                    if menu.tick():
                        items = menu.get_items()
                continue
            self.toast = None
            n = len(chunk)
            if k in (ESC, "0", BACK):
                return
            if k == UP and n:
                menu.index = start + (pos - 1) % n
            elif k == DOWN and n:
                menu.index = start + (pos + 1) % n
            elif k in (LEFT, PGUP) and pages > 1:
                np_ = (page - 1) % pages
                menu.index = min(np_ * psize + pos, len(items) - 1)
            elif k in (RIGHT, PGDN) and pages > 1:
                np_ = (page + 1) % pages
                menu.index = min(np_ * psize + pos, len(items) - 1)
            elif k == HOME:
                menu.index = start
            elif k == END and n:
                menu.index = start + n - 1
            elif k in menu.hotkeys:
                self._safe(menu.hotkeys[k][1])
                items = menu.get_items()
            elif k == ENTER or (len(k) == 1 and k in "123456789" and int(k) <= n):
                if k != ENTER:
                    menu.index = start + int(k) - 1
                if not n:
                    continue
                it = items[menu.index]
                res = self._safe(it.action) if it.action else None
                if res == "back":
                    return
                items = menu.get_items()

    def _safe(self, fn):
        try:
            return fn()
        except SystemExit as e:                          # die() inside CLI code
            if e.code not in (0, None):
                self.notify(f"command failed (exit {e.code})", "err")
        except KeyboardInterrupt:
            self.notify("cancelled", "warn")
        except Exception as e:                           # noqa: BLE001 - never crash the app
            self.notify(f"{type(e).__name__}: {e}", "err")
        return None

    def view(self, title: str, text: str, state: str = ""):
        """Scrollable read-only text. Up/Left previous page, Down/Right next page, both wrap."""
        lines = text.replace("\t", "    ").splitlines() or ["(no output)"]
        page = 0
        while True:
            f = Frame(self)
            f.top()
            f.logo()
            f.mid()
            body_h = max(f.rows - len(f.lines) - 5, 3)
            pages = max((len(lines) + body_h - 1) // body_h, 1)
            page %= pages
            f.row([(title, "cyan", None, True), (f"   {state}" if state else "", "ok"),
                   (f"   page {page + 1}/{pages}" if pages > 1 else "", "muted")])
            f.mid()
            for ln in lines[page * body_h:(page + 1) * body_h]:
                f.row([(ln, "text")])
            for _ in range(body_h - len(lines[page * body_h:(page + 1) * body_h])):
                f.row([("", None)])
            f.footer(f"{G['up']}{G['left']} previous page  {G['down']}{G['right']} next page  Enter/Esc/0 close")
            f.render()
            k = self.term.key(1.0)
            if k in (ENTER, ESC, "0", BACK):
                return
            if k in (UP, LEFT, PGUP):
                page -= 1
            elif k in (DOWN, RIGHT, PGDN, " "):
                page += 1

    def ask(self, label: str, default: str = "", secret: bool = False, hint: str = "") -> str | None:
        """One-line input. Enter accepts (empty -> default), Esc cancels (None)."""
        buf = ""
        while True:
            f = Frame(self)
            f.top()
            f.logo()
            f.mid()
            f.status()
            f.mid()
            f.blank()
            f.row([(label, "cyan", None, True)])
            if hint:
                f.row([(hint, "muted")])
            shown = "*" * len(buf) if secret else buf
            f.row([("> ", "magenta", None, True), (shown, "text"), ("█" if UTF else "_", "yellow"),
                   ("" if buf or not default or secret else f"   (default: {default})", "muted")])
            f.blank()
            f.footer("Enter accept   Esc cancel")
            f.render()
            k = self.term.key(None)
            if k == ENTER:
                return buf or default
            if k == ESC:
                return None
            if k == BACK:
                buf = buf[:-1]
            elif isinstance(k, str) and len(k) == 1 and k.isprintable():
                buf += k

    def confirm(self, question: str) -> bool:
        while True:
            f = Frame(self)
            f.top()
            f.logo()
            f.mid()
            f.blank()
            f.row([(question, "yellow", None, True)])
            f.row([("y = yes     n / Esc = no", "muted")])
            f.blank()
            f.footer("y / n")
            f.render()
            k = self.term.key(None)
            if k in ("y", "Y"):
                return True
            if k in ("n", "N", ESC, ENTER, "0"):
                return False

    # ---- running commands -----------------------------------------------------------------
    def quick(self, argv: list[str], title: str | None = None, show: bool = True) -> str:
        """Run a CLI command in-process, capture its output, show it in the viewer."""
        from .cli import main
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code in (None, 0) else 1)
        except Exception as e:                           # noqa: BLE001
            err.write(f"{type(e).__name__}: {e}\n")
            code = 1
        text = (out.getvalue() + err.getvalue()).rstrip() or "Done."
        if show:
            self.view(title or "domshon " + " ".join(argv), text, "OK" if code == 0 else f"FAILED ({code})")
        return text

    def stream(self, argv: list[str], title: str | None = None):
        """Run a long command as a child process with live output. Esc terminates it."""
        cmd = ([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "domshon"]) + argv
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "DS_NO_TUI": "1",
               "DS_NO_ESC_WATCH": "1"}
        kw: dict = {}
        if IS_WIN:
            kw["creationflags"] = 0x00000200               # CREATE_NEW_PROCESS_GROUP
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=env, **kw)
        except OSError as e:
            return self.notify(f"cannot start: {e}", "err")
        buf: list[str] = []

        def reader():
            for raw in iter(p.stdout.readline, b""):            # type: ignore[union-attr]
                buf.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        threading.Thread(target=reader, daemon=True).start()
        t0, terminated, offset = time.time(), False, 0          # offset: lines scrolled up from the tail
        title = title or "domshon " + " ".join(argv)
        while True:
            running = p.poll() is None
            f = Frame(self)
            f.top()
            f.logo()
            f.mid()
            body_h = max(f.rows - len(f.lines) - 5, 3)
            el = int(time.time() - t0)
            if running:
                st, col = f"{G['on']} RUNNING {el // 60:02d}:{el % 60:02d}", "yellow"
            elif terminated:
                st, col = f"{G['off']} TERMINATED", "danger"
            else:
                st, col = (f"{G['on']} DONE", "ok") if p.returncode == 0 else (f"{G['off']} FAILED ({p.returncode})", "danger")
            f.row([(title, "cyan", None, True), ("   " + st, col, None, True)])
            f.mid()
            end = len(buf) - offset
            shown = buf[max(end - body_h, 0):end]
            for ln in shown:
                f.row([(ln, "text")])
            for _ in range(body_h - len(shown)):
                f.row([("", None)])
            f.footer((f"Esc = terminate   {G['up']}{G['down']} scroll   End = follow" if running
                      else f"{G['up']}{G['down']} scroll   Enter/Esc/0 close"))
            f.render()
            k = self.term.key(0.3)
            if k == ESC or (k in (ENTER, "0") and not running):
                if running:
                    terminated = True
                    self._terminate(p)
                    continue
                return
            if k == UP:
                offset = min(offset + 1, max(len(buf) - 1, 0))
            elif k == DOWN or k == END:
                offset = 0 if k == END else max(offset - 1, 0)
            elif k == PGUP:
                offset = min(offset + body_h, max(len(buf) - 1, 0))
            elif k == PGDN:
                offset = max(offset - body_h, 0)

    @staticmethod
    def _terminate(p: subprocess.Popen):
        try:
            if IS_WIN:
                import signal
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.terminate()
            p.wait(timeout=4)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def interactive(self, argv: list[str]):
        """Suspend the TUI and run a command that prompts (passwords...) on the real terminal."""
        from .cli import main
        with self.term.suspended():
            try:
                main(argv)
            except SystemExit:
                pass
            except Exception as e:                          # noqa: BLE001
                print(f"{type(e).__name__}: {e}")
            try:
                input("\nPress Enter to return to the menu... ")
            except (EOFError, KeyboardInterrupt):
                pass

    # ---- remote admin client --------------------------------------------------------------
    def admin(self):
        """Admin API client, signing in once per session (credentials kept in memory only)."""
        from .client.api import Client
        from .client.config import ClientConfig
        if self.admin_client is not None:
            return self.admin_client
        cfg = ClientConfig.load()
        user = self.ask("Admin username", os.environ.get("DS_ADMIN_USER", "admin"), hint=f"server: {cfg.server_url}")
        if user is None:
            return None
        pw = self.ask("Admin password", os.environ.get("DS_ADMIN_PASSWORD", ""), secret=True)
        if pw is None:
            return None
        c = Client(cfg.server_url)
        try:
            c.http.headers["Authorization"] = "Bearer " + c.login(user, pw)["token"]
        except Exception as e:
            msg = str(e)
            if "mfa_required" in msg:
                code = self.ask("Two-factor code", hint="the server asks for a second factor")
                if code is None:
                    return None
                r = c.http.post("/api/auth/login", json={"username": user, "password": pw, "code": code})
                if r.status_code >= 400:
                    self.notify("sign-in failed: " + r.text[:80], "err")
                    return None
                c.http.headers["Authorization"] = "Bearer " + r.json()["token"]
            else:
                self.notify("sign-in failed: " + msg, "err")
                return None
        self.admin_client, self.admin_creds = c, (user, pw)
        self.notify("signed in as admin")
        return c

    def admin_call(self, fn):
        """Run fn(client); on an expired session sign in again once."""
        from .client.api import ApiError
        c = self.admin()
        if c is None:
            return None
        try:
            return fn(c)
        except ApiError as e:
            if e.status == 401:
                self.admin_client = None
                c = self.admin()
                return fn(c) if c else None
            raise


# ------------------------------------------------------------------------------------ time helpers
def ago(sec) -> str:
    if sec is None:
        return "never"
    if sec < 10:
        return "just now"
    if sec < 90:
        return f"{int(sec)}s ago"
    if sec < 5400:
        return f"{round(sec / 60)} min ago"
    if sec < 172800:
        return f"{round(sec / 3600)} h ago"
    return f"{round(sec / 86400)} d ago"


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


# ------------------------------------------------------------------------------------ menus
def build(ui: UI) -> Menu:
    from .client.api import Client
    from .client.config import ClientConfig, Profile, default_sync_dir

    def sub(title, items):          # opens a nested menu; stays alive after it returns
        return lambda: ui.run_menu(Menu(title, items))

    def cli(*argv, title=None):
        return lambda: ui.quick(list(argv), title)

    def run(*argv, title=None):
        return lambda: ui.stream(list(argv), title)

    def need_login() -> Client | None:
        cfg = ClientConfig.load()
        if not cfg.token:
            ui.notify("Not signed in. Use Account > Sign in first.", "warn")
            return None
        return Client(cfg.server_url, cfg.token)

    # ------------------------------------------------------------ account
    def sign_in():
        cfg = ClientConfig.load()
        url = ui.ask("Server address", cfg.server_url, hint="e.g. https://files.example.com")
        if url is None:
            return
        mode = ui.ask("Sign in with  1 = username/password   2 = device token", "1")
        if mode is None:
            return
        argv = ["login", "--server", url]
        if mode.strip() == "2":
            tok = ui.ask("Device token", secret=True, hint="starts with dst_")
            if not tok:
                return
            argv += ["--token", tok]
        else:
            user = ui.ask("Username", os.environ.get("DS_ADMIN_USER", "admin"))
            if user is None:
                return
            pw = ui.ask("Password", secret=True)
            if pw is None:
                return
            argv += ["--user", user, "--password", pw]
        ui.quick(argv, "Sign in")

    def sign_out():
        if ui.confirm("Forget the stored token on this device?"):
            ui.quick(["logout"], "Sign out")
            ui.admin_client = None

    # ------------------------------------------------------------ files
    def browse(root: str = "main", path: str = ""):
        c = need_login()
        if c is None:
            return
        state = {"root": root, "path": path}

        def items():
            try:
                lst = c.list(state["root"], state["path"])
            except Exception as e:                       # noqa: BLE001
                ui.notify(str(e), "err")
                lst = []
            lst.sort(key=lambda i: (i["type"] != "dir", i["name"].lower()))
            out = []
            for it in lst:
                isdir = it["type"] == "dir"
                out.append(Item((G["dir"] + " " if isdir else G["file"] + " ") + it["name"] + ("/" if isdir else ""),
                                (lambda it=it, isdir=isdir: enter(it, isdir)),
                                tag="" if isdir else f"{human(it['size'])}  {time.strftime('%Y-%m-%d', time.localtime(it['mtime']))}",
                                color="cyan" if isdir else None))
            if not out:
                out.append(Item("(empty folder)", None, color="muted"))
            return out

        def join(name):
            return "/".join(x for x in (state["path"], name) if x)

        def enter(it, isdir):
            if isdir:
                state["path"] = join(it["name"])
                m.title = f"Files  /{state['path']}"
                m.index = 0
                return None
            return file_menu(it)

        def file_menu(it):
            full = join(it["name"])

            def download():
                dest = ui.ask("Save to (folder or file path)", str(os.getcwd()))
                if dest:
                    ui.quick(["get", full, dest, "--root", state["root"]], "Download")

            def delete():
                if ui.confirm(f"Delete '{full}' on the server? This cannot be undone."):
                    ui.quick(["rm", full, "--root", state["root"]], "Delete")
                    return "back"

            def move():
                dst = ui.ask("New path on the server", full)
                if dst and dst != full:
                    ui.quick(["mv", full, dst.strip("/"), "--root", state["root"]], "Move")
                    return "back"
            ui.run_menu(Menu(it["name"], [Item("Download", download), Item("Move / rename", move),
                                          Item("Delete", delete, color="danger")]))

        def upload():
            src = ui.ask("Local file or folder to upload", hint="uploads into /" + state["path"])
            if src:
                ui.stream(["put", src, state["path"], "--root", state["root"]], "Upload")

        def mkdir():
            name = ui.ask("New folder name")
            if name:
                ui.quick(["mkdir", join(name), "--root", state["root"]], "New folder", show=False)
                ui.notify(f"created {name}")

        def up():
            if state["path"]:
                state["path"] = state["path"].rsplit("/", 1)[0] if "/" in state["path"] else ""
                m.title = f"Files  /{state['path']}"
                return None
            return "back"

        def roots():
            try:
                rs = c.roots()
            except Exception as e:                       # noqa: BLE001
                return ui.notify(str(e), "err")
            ui.run_menu(Menu("Choose a server folder", [Item(r, (lambda r=r: switch(r))) for r in rs]))

        def switch(r):
            state["root"], state["path"] = r, ""
            m.title = f"Files  /{state['path']}"
            return "back"
        m = Menu(f"Files  /{state['path']}", items,
                 hotkeys={"u": ("upload", upload), "n": ("new folder", mkdir), "r": ("refresh", lambda: None),
                          "f": ("folders", roots)}, back_label="Up one level / leave")
        # "back" (0/Esc) goes up a level first, then leaves
        while True:
            ui.run_menu(m)
            if not state["path"]:
                return
            up()
            m.index = 0

    # ------------------------------------------------------------ sync
    def profile_menu(title, fn):
        def items():
            ps = ClientConfig.load().profiles
            return [Item(p.name, (lambda p=p: (fn(p), "back")[1]), desc=f"{p.local_dir} <-> {p.remote_root}:/{p.remote_path}",
                         tag=f"{p.interval}s" + ("" if p.enabled else " off")) for p in ps] or [Item("(no profiles yet)", None, color="muted")]
        return lambda: ui.run_menu(Menu(title, items))

    def sync_add():
        name = ui.ask("Profile name", hint="a short label, e.g. work")
        if not name:
            return
        local = ui.ask("Local folder", str(default_sync_dir()))
        if not local:
            return
        path = ui.ask("Server sub-folder (empty = whole folder)", "")
        if path is None:
            return
        iv = ui.ask("Sync every N seconds", "30")
        if iv is None:
            return
        ui.quick(["sync", "add", name, local, "--path", path, "--interval", iv or "30"], "Add profile")

    def sync_rm(p):
        if ui.confirm(f"Remove profile '{p.name}'? Files are kept."):
            ui.quick(["sync", "rm", p.name], show=False)
            ui.notify(f"removed {p.name}")

    sync_items = lambda: [
        Item("Sync everything now", run("sync", "run"), "one pass over all profiles"),
        Item("Sync one profile now", profile_menu("Pick a profile", lambda p: ui.stream(["sync", "run", p.name]))),
        Item("Force full re-compare", run("sync", "run", "--force")),
        Item("Keep syncing (daemon)", run("daemon", title="Sync daemon"), "Esc stops it"),
        Item("List profiles", cli("sync", "list")),
        Item("Add a profile", sync_add),
        Item("Remove a profile", profile_menu("Remove which profile?", sync_rm), color="danger"),
    ]

    # ------------------------------------------------------------ this server
    def settings_menu():
        from .config import EDITABLE, Settings
        s = Settings()
        pub = s.public()

        def items():
            out = []
            for k in sorted(EDITABLE):
                v = pub.get(k)
                shown = "********" if "password" in k or "token" in k else ("" if v is None else str(v))
                out.append(Item(k, (lambda k=k, shown=shown: edit(k, shown)), tag=shown[:30]))
            return out

        def edit(k, shown):
            v = ui.ask(f"New value for {k}", "" if "password" in k or "token" in k else shown,
                       secret="password" in k or "token" in k)
            if v is not None:
                ui.quick(["server", "set", k, v], f"{k} updated")
                nonlocal pub
                pub = Settings().public()
        ui.run_menu(Menu("Server settings (live)", items))

    def toggle(key):
        def fn():
            from .config import Settings
            cur = bool(Settings().get(key))
            ui.quick(["server", "set", key, "false" if cur else "true"], show=False)
            ui.notify(f"{key} is now {'OFF' if cur else 'ON'}")
        return fn

    def local_tokens(action):
        def fn():
            from .config import Settings
            from .server.security import TokenStore
            ts = TokenStore(Settings().dir / "tokens.json")
            ui.run_menu(Menu("Revoke which token?", lambda: [
                Item(t["name"], (lambda t=t: (ts.revoke(t["id"]), ui.notify("revoked " + t["name"]), "back")[2]), tag=t["id"])
                for t in ts.list()] or [Item("(no tokens)", None, color="muted")]))
        return fn

    def token_add_local():
        name = ui.ask("Device name", "device")
        if name:
            ui.quick(["server", "token", "add", name], "New device token (shown once)")

    def devices_local():
        from .config import Settings
        from .server.security import TokenStore
        ts = TokenStore(Settings().dir / "tokens.json")
        ui.run_menu(devices_menu(lambda: ts.devices(), "Devices (this machine)"))

    server_items = lambda: [
        Item("Start the server", run("server", "serve", title="Domshon server"), "Esc stops it"),
        Item("Settings (live)", settings_menu),
        Item("Storage usage", cli("server", "usage")),
        Item("Show configuration", cli("server", "show")),
        Item("Set admin password", lambda: ui.interactive(["server", "passwd"])),
        Item("Create device token", token_add_local),
        Item("List device tokens", cli("server", "token", "list")),
        Item("Revoke device token", local_tokens("rm"), color="danger"),
        Item("Device monitor", devices_local, "connected + last seen"),
        Item("Toggle read-only mode", toggle("read_only")),
        Item("Toggle lockdown", toggle("lockdown")),
    ]

    # ------------------------------------------------------------ devices monitor (shared)
    def devices_menu(fetch, title):
        cache = {"d": {"devices": [], "online": 0, "total": 0, "last": None}, "t": 0.0}

        def refresh(force=False):
            if force or time.time() - cache["t"] > 4:
                try:
                    cache["d"] = fetch()
                except Exception as e:                   # noqa: BLE001
                    ui.notify(str(e), "err")
                cache["t"] = time.time()
                return True
            return False

        def items():
            refresh(True)
            d = cache["d"]
            head = Item(f"{d['online']} online of {d['total']}" + (
                f"   last: {d['last']['name']} ({d['last'].get('last_ip') or '?'}) {ago(d['last']['ago'])}" if d.get("last") else
                "   no device has connected yet"), None, color="magenta")
            rows = [head]
            for t in d["devices"]:
                state = "LIVE" if t.get("live") else "ONLINE" if t["online"] else "OFFLINE"
                rows.append(Item(f"{G['on'] if t['online'] else G['off']} {t['name']}", None,
                                 desc=f"{state}  {t.get('last_ip') or ''}", tag=ago(t["ago"]),
                                 color="ok" if t["online"] else "muted"))
            return rows
        return Menu(title, items, tick=lambda: True, interval=4.0)

    # ------------------------------------------------------------ remote admin
    def admin_devices():
        if ui.admin() is None:
            return
        ui.run_menu(devices_menu(lambda: ui.admin_call(lambda c: c.admin_devices()), "Devices (live, remote server)"))

    def admin_settings_menu():
        d = ui.admin_call(lambda c: c.admin_settings())
        if not d:
            return
        keys = sorted(k for k, v in d["settings"].items() if not isinstance(v, dict) or k == "mounts")

        def items():
            return [Item(k, (lambda k=k: edit(k)), tag=str(d["settings"].get(k))[:30]) for k in keys]

        def edit(k):
            v = ui.ask(f"New value for {k}", str(d["settings"].get(k, "")))
            if v is not None:
                ui.admin_call(lambda c: c.admin_update({k: v}))
                ui.notify(f"{k} updated")
                d["settings"][k] = v
        ui.run_menu(Menu("Remote settings (live)", items))

    def admin_usage():
        u = ui.admin_call(lambda c: c.admin_usage())
        if u:
            lines = [f"cloud usage: {human(u['used'])}" + (f" of {human(u['quota'])} quota" if u["quota"] else " (no quota)")]
            if u.get("disk"):
                lines.append(f"disk: {human(u['disk']['free'])} free of {human(u['disk']['total'])}")
            lines += [f"  {human(t['size']):>10}  {t['name']}{'/' if t['type'] == 'dir' else ''}" for t in u.get("top", [])]
            ui.view("Remote storage usage", "\n".join(lines))

    def admin_tokens_list():
        t = ui.admin_call(lambda c: c.admin_tokens())
        if t is not None:
            ui.view("Device tokens", "\n".join(
                f"{x['id']}  {x['name']:<20} created {time.strftime('%Y-%m-%d', time.localtime(x['created']))}"
                f"  last seen {ago(int(time.time() - x['last_seen'])) if x.get('last_seen') else 'never'}" for x in t) or "(none)")

    def admin_token_add():
        name = ui.ask("Device name", "device")
        if name:
            r = ui.admin_call(lambda c: c.admin_token_create(name))
            if r:
                ui.view("New token (shown once - copy it now)", r["token"])

    def admin_token_rm():
        toks = ui.admin_call(lambda c: c.admin_tokens())
        if toks is None:
            return

        def rm(t):
            if ui.confirm(f"Revoke '{t['name']}'?"):
                ui.admin_call(lambda c: c.admin_token_revoke(t["id"]))
                ui.notify("revoked " + t["name"])
            return "back"
        ui.run_menu(Menu("Revoke which token?", [Item(t["name"], (lambda t=t: rm(t)), tag=t["id"]) for t in toks]
                         or [Item("(no tokens)", None, color="muted")]))

    def admin_passwd():
        pw = ui.ask("New admin password (min 10 chars)", secret=True)
        if pw:
            ui.admin_call(lambda c: c.admin_password(pw))
            ui.notify("password updated")

    def admin_rotate():
        if ui.confirm("Invalidate ALL admin sessions (including this one)?"):
            ui.admin_call(lambda c: c.admin_rotate())
            ui.admin_client = None
            ui.notify("secret rotated - sign in again", "warn")

    def admin_signout():
        ui.admin_client, ui.admin_creds = None, None
        ui.notify("admin session forgotten")

    admin_items = lambda: [
        Item("Device monitor", admin_devices, "connected + last connected"),
        Item("Settings (live)", admin_settings_menu),
        Item("Storage usage", admin_usage),
        Item("List device tokens", admin_tokens_list),
        Item("Create device token", admin_token_add),
        Item("Revoke device token", admin_token_rm, color="danger"),
        Item("Change admin password", admin_passwd),
        Item("Rotate secret (log everyone out)", admin_rotate, color="danger"),
        Item("Forget admin sign-in", admin_signout),
    ]

    # ------------------------------------------------------------ hosting
    def svc(sub_, *, long=False, confirm=None):
        def fn():
            if confirm and not ui.confirm(confirm):
                return
            (ui.stream if long else ui.quick)(["service", sub_], f"service {sub_}")
        return fn

    def svc_install():
        dom = ui.ask("Public domain (empty = none yet)", "")
        if dom is None:
            return
        ui.stream(["service", "install"] + (["--domain", dom] if dom else []), "service install")

    def nginx_show():
        dom = ui.ask("Domain", "")
        if dom is not None:
            ui.quick(["nginx", "show"] + (["--domain", dom] if dom else []), "nginx config")

    def nginx_install():
        dom = ui.ask("Domain (DNS must already point here)")
        if not dom:
            return
        certbot = ui.confirm("Get a free HTTPS certificate with certbot?")
        email = ui.ask("Contact e-mail for certbot (optional)", "") if certbot else ""
        argv = ["nginx", "install", "--domain", dom, "--install-packages"]
        if certbot:
            argv.append("--certbot")
        if email:
            argv += ["--email", email]
        ui.stream(argv, "nginx install")

    hosting_items = lambda: [
        Item("Install server service", svc_install, "starts at boot"),
        Item("Service status", svc("status")),
        Item("Start service", svc("start")),
        Item("Stop service", svc("stop")),
        Item("Restart service", svc("restart")),
        Item("Service log", svc("logs")),
        Item("Uninstall service", svc("uninstall", confirm="Stop and remove the service? Files are kept."), color="danger"),
        Item("Show nginx config", nginx_show),
        Item("Install nginx + HTTPS", nginx_install),
        Item("Remove nginx site", lambda: ui.confirm("Remove the nginx site?") and ui.quick(["nginx", "remove"]), color="danger"),
    ]

    # ------------------------------------------------------------ desktop
    def spawn_gui(*extra):
        def fn():
            cmd = ([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "domshon"]) + list(extra)
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **({"creationflags": 0x00000008} if IS_WIN else {"start_new_session": True}))
                ui.notify("desktop app started")
            except OSError as e:
                ui.notify(str(e), "err")
        return fn

    desktop_items = lambda: [
        Item("Open desktop app", spawn_gui("gui")),
        Item("Start in the system tray", spawn_gui("tray", "--minimized")),
        Item("Create / repair launcher", cli("desktop", "install")),
        Item("Remove launcher", cli("desktop", "uninstall"), color="danger"),
        Item("Launcher status", cli("desktop", "status")),
        Item("Launch tray app on login: enable", cli("autostart", "enable")),
        Item("Launch tray app on login: disable", cli("autostart", "disable")),
        Item("Autostart status", cli("autostart", "status")),
        Item("Start server on boot: enable", cli("autostart", "enable", "--server")),
        Item("Start server on boot: disable", cli("autostart", "disable", "--server")),
    ]

    # ------------------------------------------------------------ help
    def help_menu():
        from .cli import build_parser
        p = build_parser()
        subs = next(a for a in p._actions if a.__class__.__name__ == "_SubParsersAction")
        helps = {a.dest: a.help for a in subs._choices_actions}

        def show(name):
            return lambda: ui.view(f"domshon {name} -h", subs.choices[name].format_help())
        items = [Item("Overview and examples", lambda: ui.view("domshon --help", p.format_help()), color="cyan"),
                 Item("Keyboard shortcuts", lambda: ui.view("Keyboard shortcuts", __doc__ or ""), color="cyan")]
        items += [Item(n, show(n), desc=helps.get(n, "")) for n in subs.choices]
        ui.run_menu(Menu("Command reference (the same commands work without this app)", items))

    account_items = lambda: [
        Item("Sign in / change server", sign_in),
        Item("Sign out", sign_out),
        Item("Status and health", cli("status")),
    ]

    return Menu("Main menu", lambda: [
        Item("Status and health", cli("status"), "server, login, profiles"),
        Item("Files on the server", browse, "browse, upload, download"),
        Item("Folder sync", sub("Folder sync", sync_items), "profiles, run, daemon"),
        Item("This server", sub("This server (local machine)", server_items), "run, settings, tokens"),
        Item("Remote admin", sub("Remote admin (needs admin sign-in)", admin_items), "devices, settings"),
        Item("Hosting", sub("Hosting: service and nginx", hosting_items), "boot service, HTTPS"),
        Item("Desktop app", sub("Desktop app and autostart", desktop_items), "launcher, tray"),
        Item("Account", sub("Account", account_items), "sign in / out"),
        Item("Help and commands", help_menu, "every command explained"),
    ], back_label="Exit")


# ------------------------------------------------------------------------------------ entry points
def available() -> bool:
    return (sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("DS_NO_TUI")
            and os.environ.get("TERM", "xterm") != "dumb")


def run_tui() -> int:
    if not available():
        print("The interactive terminal app needs a real terminal. Use 'domshon --help' for the commands.")
        return 1
    ui = UI()
    threading.Thread(target=ui.poll_status, daemon=True).start()
    ui.term.start()
    try:
        ui.run_menu(build(ui))
    except KeyboardInterrupt:
        pass
    finally:
        ui._stop.set()
        ui.term.stop()
    return 0


class EscStop:
    """Plain-CLI helper: while a long command runs, pressing Esc stops it like Ctrl+C (which can misbehave).

        with EscStop("Press Esc to stop."):
            run_forever()
    """

    def __init__(self, hint: str = ""):
        self.hint = hint
        self._run = False
        self._old = None

    def __enter__(self):
        if os.environ.get("DS_NO_ESC_WATCH") or not sys.stdin.isatty():
            return self
        try:
            if not IS_WIN:
                import termios
                fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(fd)
                new = termios.tcgetattr(fd)
                new[3] &= ~(termios.ICANON | termios.ECHO)          # keep ISIG: Ctrl+C still works
                new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
            self._run = True
            threading.Thread(target=self._watch, daemon=True).start()
            if self.hint:
                print(self.hint, flush=True)
        except Exception:
            self._run = False
        return self

    def _watch(self):
        import _thread
        t = Term()
        while self._run:
            try:
                k = t.key(0.2)
            except Exception:
                return
            if k == ESC and self._run:
                self._run = False
                _thread.interrupt_main()
                return

    def __exit__(self, *exc):
        self._run = False
        if self._old is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            except Exception:
                pass
        return False
