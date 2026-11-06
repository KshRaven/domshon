"""Single source of truth for the Domshon look.

The web UI gets these tokens as CSS variables (served at /static/theme.css) and the
desktop GUI builds its Qt stylesheet from the same values, so every front-end looks
like the same application.
"""
from __future__ import annotations

TOKENS = {
    "bg0": "#06050f",
    "bg1": "#0d0a22",
    "bg2": "#161036",
    "cyan": "#00f0ff",
    "magenta": "#ff2bd6",
    "violet": "#8a5cff",
    "yellow": "#fcee0a",
    "ok": "#39ff88",
    "danger": "#ff4d6d",
    "text": "#e8f1ff",
    "muted": "#8b95b8",
    "glass": "rgba(255,255,255,0.06)",
    "glass-strong": "rgba(255,255,255,0.10)",
    "glass-border": "rgba(0,240,255,0.28)",
    "font-ui": '"Segoe UI", "Inter", "Cantarell", system-ui, sans-serif',
    "font-mono": '"JetBrains Mono", "Cascadia Code", Consolas, "DejaVu Sans Mono", monospace',
    "radius": "14px",
}


def css_variables() -> str:
    body = ";".join(f"--ds-{k}:{v}" for k, v in TOKENS.items())
    return f":root{{{body}}}\n"


def qss() -> str:
    t = TOKENS
    return f"""
* {{ font-family: {t['font-ui']}; color: {t['text']}; font-size: 13px; }}
QMainWindow, QDialog, QWidget#root {{
  background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {t['bg0']}, stop:0.55 {t['bg1']}, stop:1 {t['bg2']});
}}
QWidget {{ background: transparent; }}
QTabWidget::pane {{ border: 1px solid rgba(0,240,255,0.28); border-radius: 14px;
  background: rgba(255,255,255,0.05); top: -1px; }}
QTabBar::tab {{ background: rgba(255,255,255,0.04); color: {t['muted']}; padding: 9px 22px;
  margin-right: 4px; border: 1px solid rgba(255,255,255,0.08); border-bottom: none;
  border-top-left-radius: 10px; border-top-right-radius: 10px; }}
QTabBar::tab:selected {{ color: {t['cyan']}; border-color: {t['cyan']}; background: rgba(0,240,255,0.10); }}
QFrame#card, QGroupBox {{ background: rgba(255,255,255,0.06); border: 1px solid rgba(0,240,255,0.28);
  border-radius: 14px; margin-top: 10px; padding: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; color: {t['cyan']}; padding: 0 6px; }}
QLabel#title {{ font-family: {t['font-mono']}; font-size: 20px; font-weight: 700; color: {t['cyan']}; letter-spacing: 3px; }}
QLabel#muted {{ color: {t['muted']}; }}
QLabel#ok {{ color: {t['ok']}; }} QLabel#danger {{ color: {t['danger']}; }}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{ background: rgba(6,5,15,0.65);
  border: 1px solid rgba(255,255,255,0.14); border-radius: 9px; padding: 7px 10px;
  selection-background-color: {t['violet']}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {t['cyan']}; }}
QPushButton {{ background: rgba(0,240,255,0.10); border: 1px solid {t['cyan']}; color: {t['cyan']};
  border-radius: 9px; padding: 7px 16px; font-weight: 600; }}
QPushButton:hover {{ background: rgba(0,240,255,0.25); }}
QPushButton:pressed {{ background: rgba(255,43,214,0.25); border-color: {t['magenta']}; color: {t['magenta']}; }}
QPushButton#danger {{ border-color: {t['danger']}; color: {t['danger']}; background: rgba(255,77,109,0.10); }}
QPushButton:disabled {{ color: {t['muted']}; border-color: rgba(255,255,255,0.12); background: transparent; }}
QTableWidget, QListWidget, QTreeWidget {{ background: rgba(6,5,15,0.45); border: 1px solid rgba(255,255,255,0.10);
  border-radius: 10px; gridline-color: rgba(255,255,255,0.06); alternate-background-color: rgba(255,255,255,0.03); }}
QTableWidget::item:selected, QListWidget::item:selected {{ background: rgba(138,92,255,0.35); color: {t['text']}; }}
QHeaderView::section {{ background: rgba(255,255,255,0.06); color: {t['cyan']}; border: none; padding: 7px;
  font-family: {t['font-mono']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: rgba(0,240,255,0.35); border-radius: 5px; min-height: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QProgressBar {{ border: 1px solid rgba(0,240,255,0.35); border-radius: 8px; text-align: center; background: rgba(6,5,15,0.6); }}
QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t['cyan']}, stop:1 {t['magenta']}); border-radius: 7px; }}
QStatusBar {{ color: {t['muted']}; }}
QMenu {{ background: {t['bg1']}; border: 1px solid {t['cyan']}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(0,240,255,0.18); }}
QToolTip {{ background: {t['bg1']}; color: {t['cyan']}; border: 1px solid {t['cyan']}; }}
"""
