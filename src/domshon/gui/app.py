"""Domshon desktop application (PySide6): file browser with drag & drop, sync manager,
admin panel, header Settings + Account buttons, system-tray icon with pop-up notifications, launch-on-boot toggle."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QDir, QLockFile, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMessageBox, QMenu, QPlainTextEdit, QPushButton, QSpinBox,
                               QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__, autostart, desktop, theme
from ..client.api import ApiError, Client
from ..client.config import ClientConfig, Profile, default_sync_dir
from ..client.sync import Runner
from .icon import make_icon

BUS: "Bus | None" = None


class Bus(QObject):
    """Marshals callables from worker threads onto the GUI thread."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    @Slot(object)
    def _run(self, fn):
        fn()


def run_async(fn, cb):
    def job():
        try:
            r, e = fn(), None
        except Exception as ex:  # noqa: BLE001 - surfaced to the UI
            r, e = None, ex
        BUS.call.emit(lambda: cb(r, e))
    threading.Thread(target=job, daemon=True).start()


def call_api(fn, cb):
    """Run fn(client) off the GUI thread; cb(result, error) runs on the GUI thread."""
    def job():
        cfg = ClientConfig.load()
        if not cfg.token:
            raise ApiError(0, "Not signed in - open Account (top right).")
        c = Client(cfg.server_url, cfg.token)
        try:
            return fn(c)
        finally:
            c.close()
    run_async(job, cb)


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def jp(*parts) -> str:
    return "/".join(x for x in parts if x)


class DropTable(QTableWidget):
    dropped = Signal(list)

    def __init__(self, *a):
        super().__init__(*a)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.dropped.emit(paths)


def card_title(text: str) -> QLabel:
    l = QLabel(text)
    l.setObjectName("title")
    return l


# ======================================================================== files
class FilesTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.root, self.path = win, "main", ""
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.rootBox = QComboBox()
        self.rootBox.currentTextChanged.connect(self.on_root)
        self.pathLbl = QLabel("/")
        self.pathLbl.setObjectName("muted")
        top.addWidget(self.rootBox)
        top.addWidget(self.pathLbl, 1)
        for text, fn in (("↑ Upload", self.pick_files), ("↑ Folder", self.pick_folder), ("+ Folder", self.mkdir),
                         ("↓ Download", self.download), ("✎ Rename", self.rename), ("✕ Delete", self.delete),
                         ("⟳", self.refresh)):
            b = QPushButton(text)
            if "Delete" in text:
                b.setObjectName("danger")
            b.clicked.connect(fn)
            top.addWidget(b)
        lay.addLayout(top)
        self.table = DropTable(0, 3)
        self.table.setHorizontalHeaderLabels(["NAME", "SIZE", "MODIFIED"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self.open_row)
        self.table.dropped.connect(self.upload_paths)
        lay.addWidget(self.table, 1)
        hint = QLabel("Drag & drop files or folders onto the list to upload.")
        hint.setObjectName("muted")
        lay.addWidget(hint)

    # -- loading
    def load(self):
        def done(roots, err):
            if err:
                return self.win.status(f"Cannot load: {err}", True)
            self.rootBox.blockSignals(True)
            self.rootBox.clear()
            self.rootBox.addItems(roots)
            if self.root in roots:
                self.rootBox.setCurrentText(self.root)
            else:
                self.root = roots[0]
            self.rootBox.blockSignals(False)
            self.refresh()
        call_api(lambda c: c.roots(), done)

    def on_root(self, name):
        if name:
            self.root, self.path = name, ""
            self.refresh()

    def refresh(self):
        root, path = self.root, self.path

        def done(items, err):
            if err:
                return self.win.status(str(err), True)
            self.pathLbl.setText(f"{root}:/{path}")
            self.table.setRowCount(0)
            rows = ([("dir", "..", 0, 0)] if path else []) + \
                   [(i["type"], i["name"], i["size"], i["mtime"]) for i in items]
            for typ, name, size, mtime in rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                it = QTableWidgetItem(("▣ " if typ == "dir" else "▤ ") + name)
                it.setData(Qt.ItemDataRole.UserRole, (typ, name))
                self.table.setItem(r, 0, it)
                self.table.setItem(r, 1, QTableWidgetItem("" if typ == "dir" else human(size)))
                self.table.setItem(r, 2, QTableWidgetItem("" if not mtime else time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))))
            self.win.status(f"{len(items)} items")
        call_api(lambda c: c.list(root, path), done)

    def selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        out = []
        for r in sorted(rows):
            typ, name = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            if name != "..":
                out.append((typ, name))
        return out

    def open_row(self, row, _col):
        typ, name = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if typ == "dir":
            self.path = self.path.rsplit("/", 1)[0] if name == ".." and "/" in self.path else ("" if name == ".." else jp(self.path, name))
            self.refresh()
        else:
            self.download()

    # -- actions
    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        if files:
            self.upload_paths(files)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Upload folder")
        if d:
            self.upload_paths([d])

    def upload_paths(self, paths):
        root, base = self.root, self.path

        def job(c: Client):
            n = 0
            for p in map(Path, paths):
                if p.is_file():
                    c.upload(root, jp(base, p.name), p)
                    n += 1
                elif p.is_dir():
                    top = jp(base, p.name)
                    c.mkdir(root, top)
                    for dp, dns, fns in os.walk(p):
                        rel = Path(dp).relative_to(p).as_posix()
                        rel = "" if rel == "." else rel
                        for d in dns:
                            c.mkdir(root, jp(top, rel, d))
                        for f in fns:
                            c.upload(root, jp(top, rel, f), Path(dp) / f)
                            n += 1
            return n

        self.win.status("Uploading…")

        def done(n, err):
            if err:
                return self.win.status(f"Upload failed: {err}", True)
            self.win.status(f"Uploaded {n} file(s)")
            self.win.notify("Upload complete", f"{n} file(s) uploaded")
            self.refresh()
        call_api(job, done)

    def mkdir(self):
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            call_api(lambda c: c.mkdir(self.root, jp(self.path, name.strip())),
                     lambda r, e: self.win.status(str(e), True) if e else self.refresh())

    def rename(self):
        sel = self.selected()
        if len(sel) != 1:
            return self.win.status("Select exactly one item to rename", True)
        old = jp(self.path, sel[0][1])
        new, ok = QInputDialog.getText(self, "Rename / move", "New path (relative to root):", text=old)
        if ok and new and new != old:
            call_api(lambda c: c.move(self.root, old, new.strip("/")),
                     lambda r, e: self.win.status(str(e), True) if e else self.refresh())

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        if ClientConfig.load().pref("confirm_delete", True) and QMessageBox.question(
                self, "Delete", f"Delete {len(sel)} item(s)? This cannot be undone.") \
                != QMessageBox.StandardButton.Yes:
            return
        root, base = self.root, self.path

        def job(c):
            for _, name in sel:
                c.delete(root, jp(base, name))
        call_api(job, lambda r, e: self.win.status(str(e), True) if e else self.refresh())

    def download(self):
        sel = self.selected()
        if len(sel) != 1:
            return self.win.status("Select exactly one item to download", True)
        typ, name = sel[0]
        dest, _ = QFileDialog.getSaveFileName(self, "Save as", str(Path.home() / (name + (".zip" if typ == "dir" else ""))))
        if not dest:
            return
        root, full = self.root, jp(self.path, name)
        self.win.status("Downloading…")
        call_api(lambda c: c.download(root, full, Path(dest)),
                 lambda r, e: self.win.status(str(e), True) if e else self.win.status(f"Saved {dest}"))


# ======================================================================== sync
class ProfileDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add sync folder")
        f = QFormLayout(self)
        self.name = QLineEdit("documents")
        self.local = QLineEdit(str(default_sync_dir()))
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: self.local.setText(QFileDialog.getExistingDirectory(self, "Local folder") or self.local.text()))
        row = QHBoxLayout()
        row.addWidget(self.local, 1)
        row.addWidget(b)
        self.root = QLineEdit("main")
        self.rpath = QLineEdit("")
        self.interval = QSpinBox()
        self.interval.setRange(5, 86400)
        self.interval.setValue(30)
        f.addRow("Name", self.name)
        f.addRow("Local folder", row)
        f.addRow("Server folder (root)", self.root)
        f.addRow("Subfolder on server", self.rpath)
        f.addRow("Interval (s)", self.interval)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def profile(self) -> Profile:
        return Profile(self.name.text().strip(), str(Path(self.local.text()).expanduser()),
                       self.root.text().strip() or "main", self.rpath.text().strip("/ "), self.interval.value())


class SyncTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["NAME", "LOCAL FOLDER", "SERVER FOLDER", "EVERY"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 2)
        row = QHBoxLayout()
        for text, fn in (("+ Add", self.add), ("Remove", self.remove), ("Enable/Disable", self.toggle),
                         ("⟳ Sync now", win.sync_now), ("Pause/Resume", win.toggle_pause)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        lay.addWidget(self.log, 3)
        self.reload()

    def reload(self):
        cfg = ClientConfig.load()
        self.table.setRowCount(0)
        for p in cfg.profiles:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate([p.name + ("" if p.enabled else " (off)"), p.local_dir,
                                   f"{p.remote_root}:/{p.remote_path}", f"{p.interval}s"]):
                self.table.setItem(r, c, QTableWidgetItem(v))

    def add(self):
        d = ProfileDialog(self)
        if d.exec() == QDialog.DialogCode.Accepted:
            p = d.profile()
            cfg = ClientConfig.load()
            if not p.name or cfg.profile(p.name):
                return self.win.status("Profile name empty or already exists", True)
            cfg.profiles.append(p)
            cfg.save()
            self.reload()
            self.win.sync_now()

    def _sel(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        return sorted(rows)

    def remove(self):
        cfg = ClientConfig.load()
        names = [self.table.item(r, 0).text().replace(" (off)", "") for r in self._sel()]
        cfg.profiles = [p for p in cfg.profiles if p.name not in names]
        cfg.save()
        self.reload()

    def toggle(self):
        cfg = ClientConfig.load()
        names = [self.table.item(r, 0).text().replace(" (off)", "") for r in self._sel()]
        for p in cfg.profiles:
            if p.name in names:
                p.enabled = not p.enabled
        cfg.save()
        self.reload()

    def append(self, msg: str):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")


# ======================================================================== admin
class LoginDialog(QDialog):
    def __init__(self, parent=None, title="Admin sign-in"):
        super().__init__(parent)
        self.setWindowTitle(title)
        f = QFormLayout(self)
        self.user = QLineEdit(os.environ.get("DS_ADMIN_USER", "admin"))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Username", self.user)
        f.addRow("Password", self.pw)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class AdminTab(QWidget):
    FIELDS = [("domain", "Public domain", "s"), ("storage_root", "Storage location", "s"),
              ("mounts", "Extra folders (name=/path;…)", "s"), ("admin_user", "Admin user", "s"),
              ("max_upload_mb", "Max upload MB (0=∞)", "i"), ("session_hours", "Session hours", "i"),
              ("quota_mb", "Cloud storage quota MB (0=∞)", "i"), ("min_free_mb", "Keep disk free MB (0=off)", "i"),
              ("blocked_extensions", "Blocked file types (.exe,.php)", "s"),
              ("user_writes", "Device users may change files", "b"),
              ("read_only", "Read-only mode", "b"), ("lockdown", "Lockdown (admin session only)", "b")]

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.client, self.widgets = win, None, {}
        self.lay = QVBoxLayout(self)
        self.info = QLabel("Sign in with the server admin account to manage live settings.")
        self.info.setObjectName("muted")
        self.signin = QPushButton("Sign in as admin")
        self.signin.clicked.connect(self.sign_in)
        self.lay.addWidget(self.info)
        self.lay.addWidget(self.signin)
        self.body = QWidget()
        self.lay.addWidget(self.body, 1)
        self.body.hide()

    def sign_in(self):
        d = LoginDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = ClientConfig.load()
        user, pw = d.user.text(), d.pw.text()

        def job():
            c = Client(cfg.server_url)
            c.http.headers["Authorization"] = "Bearer " + c.login(user, pw)["token"]
            return c

        def done(c, err):
            if err:
                return self.win.status(f"Admin sign-in failed: {err}", True)
            self.client = c
            self.signin.hide()
            self.info.setText(f"Signed in to {cfg.server_url} as admin")
            self.build()
        run_async(job, done)

    def build(self):
        self.body.deleteLater()
        self.body = QWidget()
        self.lay.addWidget(self.body, 1)
        v = QVBoxLayout(self.body)
        g = QGroupBox("Live settings (applied instantly)")
        self.form = QFormLayout(g)
        v.addWidget(g)
        apply = QPushButton("Apply live")
        apply.clicked.connect(self.apply)
        v.addWidget(apply)
        h = QHBoxLayout()
        pw = QPushButton("Change admin password")
        pw.clicked.connect(self.change_pw)
        rot = QPushButton("Rotate secret")
        rot.setObjectName("danger")
        rot.clicked.connect(self.rotate)
        h.addWidget(pw)
        h.addWidget(rot)
        v.addLayout(h)
        tg = QGroupBox("Device tokens")
        tl = QVBoxLayout(tg)
        self.tokens = QListWidget()
        tl.addWidget(self.tokens)
        th = QHBoxLayout()
        nt = QPushButton("+ Create token")
        nt.clicked.connect(self.new_token)
        rt = QPushButton("Revoke selected")
        rt.setObjectName("danger")
        rt.clicked.connect(self.revoke)
        th.addWidget(nt)
        th.addWidget(rt)
        tl.addLayout(th)
        v.addWidget(tg, 1)
        self.disk = QLabel("")
        self.disk.setObjectName("muted")
        v.addWidget(self.disk)
        self.body.show()
        self.load()

    def load(self):
        def done(d, err):
            if err:
                return self.win.status(str(err), True)
            s = d[0]["settings"]
            while self.form.rowCount():
                self.form.removeRow(0)
            self.widgets = {}
            for key, label, kind in self.FIELDS:
                v = s.get(key)
                if kind == "b":
                    w = QCheckBox()
                    w.setChecked(bool(v))
                elif kind == "i":
                    w = QSpinBox()
                    w.setRange(0, 10**7)
                    w.setValue(int(v or 0))
                else:
                    if key == "mounts":
                        v = ";".join(f"{a}={b}" for a, b in (v or {}).items())
                    w = QLineEdit(str(v or ""))
                self.widgets[key] = (w, kind)
                self.form.addRow(label, w)
            self.tokens.clear()
            for t in d[1]:
                it = self.tokens.addItem(f"{t['id']}   {t['name']}")
            du = d[0].get("disk") or {}
            us, parts = d[0].get("usage") or {}, []
            if us:
                q = us.get("quota") or 0
                parts.append(f"Cloud: {human(us['used'])}" + (f" of {human(q)} quota" if q else " (no quota)"))
            if du:
                parts.append(f"Disk: {human(du['used'])} used / {human(du['total'])} ({human(du['free'])} free)")
            self.disk.setText("   |   ".join(parts))
        run_async(lambda: (self.client.admin_settings(), self.client.admin_tokens()), done)

    def apply(self):
        ch = {}
        for k, (w, kind) in self.widgets.items():
            ch[k] = w.isChecked() if kind == "b" else w.value() if kind == "i" else w.text()
        run_async(lambda: self.client.admin_update(ch),
                  lambda r, e: self.win.status(str(e), True) if e else (self.win.status("Settings applied live"), self.load()))

    def change_pw(self):
        pw, ok = QInputDialog.getText(self, "Admin password", "New password (min 10 chars):", QLineEdit.EchoMode.Password)
        if ok and pw:
            run_async(lambda: self.client.admin_password(pw),
                      lambda r, e: self.win.status(str(e), True) if e else self.win.status("Password changed"))

    def rotate(self):
        if QMessageBox.question(self, "Rotate", "Invalidate ALL admin sessions?") != QMessageBox.StandardButton.Yes:
            return

        def done(tok, e):
            if e:
                return self.win.status(str(e), True)
            self.client.http.headers["Authorization"] = "Bearer " + tok
            self.win.status("Secret rotated")
        run_async(lambda: self.client.admin_rotate(), done)

    def new_token(self):
        name, ok = QInputDialog.getText(self, "New token", "Device name:")
        if not ok:
            return

        def done(r, e):
            if e:
                return self.win.status(str(e), True)
            QInputDialog.getText(self, "Token (shown once)", "Copy this token:", text=r["token"])
            self.load()
        run_async(lambda: self.client.admin_token_create(name or "device"), done)

    def revoke(self):
        it = self.tokens.currentItem()
        if it:
            tid = it.text().split()[0]
            run_async(lambda: self.client.admin_token_revoke(tid), lambda r, e: self.load())


# ======================================================================== account / settings
class AccountDialog(QDialog):
    """Who you are and which server you are connected to: sign in, sign out, device token."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(f"{APP_NAME} - Account")
        self.setMinimumWidth(460)
        cfg = ClientConfig.load()
        lay = QVBoxLayout(self)
        self.ident = QLabel("")
        lay.addWidget(self.ident)
        g = QGroupBox("Server connection")
        f = QFormLayout(g)
        self.url = QLineEdit(cfg.server_url)
        self.device = QLineEdit(cfg.device)
        self.user = QLineEdit("admin")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.token = QLineEdit(cfg.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Server URL", self.url)
        f.addRow("Device name", self.device)
        f.addRow("Username", self.user)
        f.addRow("Password", self.pw)
        f.addRow("…or API token", self.token)
        row = QHBoxLayout()
        b1 = QPushButton("Log in (create device token)")
        b1.clicked.connect(self.login)
        b2 = QPushButton("Save token / URL")
        b2.clicked.connect(self.save)
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(row)
        self.state = QLabel("")
        f.addRow(self.state)
        lay.addWidget(g)
        row2 = QHBoxLayout()
        out = QPushButton("Sign out of this device")
        out.setObjectName("danger")
        out.clicked.connect(self.sign_out)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row2.addWidget(out)
        row2.addStretch(1)
        row2.addWidget(close)
        lay.addLayout(row2)

    def showEvent(self, e):  # noqa: N802 (Qt API)
        super().showEvent(e)
        self.refresh_identity()

    def refresh_identity(self):
        cfg = ClientConfig.load()
        self.url.setText(cfg.server_url)
        self.token.setText(cfg.token)
        if not cfg.token:
            self.ident.setText("Not signed in. Log in below or paste an API token.")
            return
        self.ident.setText("Checking…")

        def done(me, err):
            if err:
                self.ident.setText(f"Cannot verify the stored token: {err}")
            else:
                role = "administrator" if me.get("role") == "admin" else "device"
                self.ident.setText(f"Signed in as {me.get('name')} ({role}) on {cfg.server_url}")
        call_api(lambda c: c.me(), done)

    def save(self):
        cfg = ClientConfig.load()
        cfg.server_url = self.url.text().strip().rstrip("/")
        cfg.device = self.device.text().strip() or cfg.device
        cfg.token = self.token.text().strip()
        cfg.save()
        self.state.setText("Saved.")
        self.win.files.load()
        self.refresh_identity()

    def login(self):
        url, user, pw, dev = self.url.text().strip().rstrip("/"), self.user.text(), self.pw.text(), self.device.text().strip()

        def done(tok, err):
            if err:
                return self.state.setText(f"Login failed: {err}")
            self.token.setText(tok)
            self.pw.clear()
            self.save()
            self.state.setText("Logged in. Device token stored.")
        run_async(lambda: Client(url).login(user, pw, device=dev or "desktop")["token"], done)

    def sign_out(self):
        cfg = ClientConfig.load()
        cfg.token = ""
        cfg.save()
        self.token.clear()
        self.win.files.load()
        self.refresh_identity()
        self.win.status("Signed out. The device token was removed from this computer.")


class SettingsDialog(QDialog):
    """Per-computer preferences: background behaviour, confirmations, notifications."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(f"{APP_NAME} - Settings")
        self.setMinimumWidth(460)
        cfg = ClientConfig.load()
        lay = QVBoxLayout(self)
        g2 = QGroupBox("Background")
        l2 = QVBoxLayout(g2)
        self.boot = QCheckBox("Launch Domshon on boot / login (runs in the system tray)")
        try:
            self.boot.setChecked(autostart.is_enabled("tray"))
        except Exception:  # noqa: BLE001
            pass
        self.boot.toggled.connect(self.toggle_boot)
        l2.addWidget(self.boot)
        lay.addWidget(g2)
        g3 = QGroupBox("Preferences")
        l3 = QVBoxLayout(g3)
        self.confirm = QCheckBox("Confirm before deleting files")
        self.confirm.setChecked(bool(cfg.pref("confirm_delete", True)))
        self.notify = QCheckBox("Show tray notifications for sync activity")
        self.notify.setChecked(bool(cfg.pref("notify", True)))
        for w in (self.confirm, self.notify):
            w.toggled.connect(self.save_prefs)
            l3.addWidget(w)
        lay.addWidget(g3)
        row = QHBoxLayout()
        adm = QPushButton("Server settings (admin)…")
        adm.clicked.connect(self.open_admin)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(adm)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        lay.addWidget(QLabel(f"{APP_NAME} {__version__}"))

    def save_prefs(self):
        cfg = ClientConfig.load()
        cfg.prefs["confirm_delete"] = self.confirm.isChecked()
        cfg.prefs["notify"] = self.notify.isChecked()
        cfg.save()

    def open_admin(self):
        self.accept()
        self.win.tabs.setCurrentWidget(self.win.admin)

    def toggle_boot(self, on):
        try:
            msg = autostart.enable("tray") if on else autostart.disable("tray")
            self.win.status(msg)
        except Exception as e:  # noqa: BLE001
            self.win.status(f"Autostart failed: {e}", True)


# ======================================================================== window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1040, 700)
        self.tray: QSystemTrayIcon | None = None
        self.runner: Runner | None = None
        self.told_tray = False
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        head = QHBoxLayout()
        head.addWidget(card_title("◈ DOMSHON"))
        head.addStretch(1)
        self.sync_lbl = QLabel("idle")
        self.sync_lbl.setObjectName("muted")
        head.addWidget(self.sync_lbl)
        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.setToolTip("Preferences and background behaviour")
        self.btn_settings.clicked.connect(lambda: self.settings.exec())
        self.btn_account = QPushButton("◉ Account")
        self.btn_account.setToolTip("Server connection, sign in / out")
        self.btn_account.clicked.connect(lambda: self.account.exec())
        head.addWidget(self.btn_settings)
        head.addWidget(self.btn_account)
        lay.addLayout(head)
        self.tabs = QTabWidget()
        self.files, self.sync, self.admin = FilesTab(self), SyncTab(self), AdminTab(self)
        self.settings, self.account = SettingsDialog(self), AccountDialog(self)
        for w, n in ((self.files, "FILES"), (self.sync, "SYNC"), (self.admin, "ADMIN")):
            self.tabs.addTab(w, n)
        lay.addWidget(self.tabs, 1)
        self.statusBar().showMessage("Ready")
        if ClientConfig.load().token:
            self.files.load()
        else:
            QTimer.singleShot(0, self.account.exec)   # first run: ask for the server login

    def status(self, msg, err=False):
        self.statusBar().showMessage(("✕ " if err else "") + msg, 8000)

    def notify(self, title, msg):
        if self.tray and self.tray.isVisible() and ClientConfig.load().pref("notify", True):
            self.tray.showMessage(title, msg, QSystemTrayIcon.MessageIcon.Information, 5000)

    # runner hooks (GUI thread)
    def on_status(self, msg):
        self.sync_lbl.setText(msg[:90])
        self.sync.append(msg)

    def on_result(self, name, res, err):
        self.sync.reload()
        if err:
            self.notify(f"Sync problem - {name}", err)
        elif res and (res.changed or res.conflicts or res.errors):
            self.notify(f"Synced {name}", res.summary())
            if self.tabs.currentWidget() is self.files:
                self.files.refresh()

    def sync_now(self):
        if self.runner:
            self.runner.sync_now()
            self.status("Sync requested")

    def toggle_pause(self):
        if self.runner:
            self.runner.paused = not self.runner.paused
            self.status("Sync paused" if self.runner.paused else "Sync resumed")
            self.sync_lbl.setText("paused" if self.runner.paused else "idle")

    def closeEvent(self, e):
        if self.tray and self.tray.isVisible():
            e.ignore()
            self.hide()
            if not self.told_tray:
                self.told_tray = True
                self.notify(APP_NAME, "Still running in the system tray. Right-click the icon to quit.")
        else:
            e.accept()
            QApplication.quit()


def run_gui(minimized: bool = False):
    global BUS
    if sys.platform.startswith("win"):
        try:  # own taskbar identity so Windows shows the Domshon icon instead of python.exe's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(desktop.AUMID)
        except Exception:  # noqa: BLE001
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("domshon")        # Linux: links the taskbar/dock entry to domshon.desktop
    lock = QLockFile(QDir.temp().filePath("domshon-gui.lock"))
    if not lock.tryLock(100):
        print("Domshon is already running.")
        return
    BUS = Bus()
    app.setStyleSheet(theme.qss())
    icon = make_icon()
    app.setWindowIcon(icon)
    win = MainWindow()
    win.setWindowIcon(icon)
    note = desktop.ensure_registered()      # application-menu / Start-Menu launcher, once per version
    if note:
        win.status(note)

    have_tray = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not have_tray)
    if have_tray:
        tray = QSystemTrayIcon(icon, app)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        for text, fn in (("Open Domshon", lambda: (win.show(), win.raise_(), win.activateWindow())),
                         ("Sync now", win.sync_now), ("Pause / resume sync", win.toggle_pause)):
            a = QAction(text, menu)
            a.triggered.connect(fn)
            menu.addAction(a)
        menu.addSeparator()
        q = QAction("Quit", menu)
        q.triggered.connect(app.quit)
        menu.addAction(q)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda r: (win.hide() if win.isVisible() else (win.show(), win.raise_()))
                               if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        win.tray = tray
        app._tray, app._menu = tray, menu  # keep references alive

    runner = Runner(on_status=lambda m: BUS.call.emit(lambda: win.on_status(m)),
                    on_result=lambda n, r, e: BUS.call.emit(lambda: win.on_result(n, r, e)))
    win.runner = runner
    runner.start()
    app.aboutToQuit.connect(runner.stop)

    if not (minimized and have_tray):
        win.show()
    code = app.exec()
    lock.unlock()
    sys.exit(code)
