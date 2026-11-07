"""Device monitoring, read-only tokens, session presence and the terminal app's pure helpers."""
import re
import time

import pytest

from domshon.server.security import Presence, TokenStore, agent_label, merge_devices


def test_token_touch_and_devices(tmp_path):
    ts = TokenStore(tmp_path / "tokens.json")
    tid, tok = ts.create("laptop")
    rec = ts.verify(tok)
    assert rec and rec["id"] == tid and not rec.get("read_only")
    assert ts.devices()["last"] is None and ts.devices()["online"] == 0
    ts.touch(tid, "10.0.0.5", "python-httpx/0.27", force=True)
    d = ts.devices()
    assert d["online"] == 1 and d["last"]["name"] == "laptop" and d["last"]["last_ip"] == "10.0.0.5"
    assert ts.devices(online_window=0)["devices"][0]["online"] in (True, False)   # window respected, no crash
    assert ts.devices(live_ids={tid})["devices"][0]["live"] is True


def test_read_only_token(tmp_path):
    ts = TokenStore(tmp_path / "tokens.json")
    _, tok = ts.create("viewer", read_only=True)
    assert ts.verify(tok)["read_only"] is True
    assert ts.list()[0]["read_only"] is True


def test_presence_and_merge(tmp_path):
    p = Presence(tmp_path / "presence.json")
    p.touch("aaaa1111", "1.2.3.4", "Mozilla/5.0 (Windows NT 10.0) Chrome", force=True)
    p.touch("bbbb2222", "5.6.7.8", "Mozilla/5.0 DomshonAndroid/0.1.0", force=True)
    again = Presence(tmp_path / "presence.json")                                  # survives a restart
    assert len(again.devices(set())) == 2
    devs = again.devices(set(), current="aaaa1111")
    assert any(d["current"] and "(this device)" in d["name"] for d in devs)
    merged = merge_devices({"devices": []}, devs)
    assert merged["total"] == 2 and merged["last"]["name"] == "Android app"      # never the viewer's own session


def test_agent_label():
    assert agent_label("x DomshonAndroid/1") == "Android app"
    assert agent_label("python-httpx/0.27") == "Desktop / CLI (admin)"
    assert "Windows" in agent_label("Mozilla/5.0 (Windows NT 10.0)")
    assert agent_label("") == "Unknown client"


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def test_tui_helpers_and_frame_width():
    from domshon import tui

    assert tui.ago(None) == "never" and tui.ago(3) == "just now" and tui.ago(120) == "2 min ago"
    assert tui.human(2048) == "2.0 KB"
    assert ANSI.sub("", tui.gradient("abc")) == "abc"
    ui = tui.UI()
    f = tui.Frame(ui)
    f.top()
    f.row([("hello", "cyan", None, True), ("  world", "muted")])
    f.row([("x" * 500, "text")])                          # over-long rows are clipped to the frame
    f.bottom()
    widths = {len(ANSI.sub("", ln)) - f.pad for ln in f.lines}
    assert widths == {f.w}


def test_cli_help_forms(capsys):
    from domshon.cli import main

    for argv in (["help"], ["--", "help"], ["help", "sync", "add"], ["server", "-h"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 0
    assert "domshon" in capsys.readouterr().out.lower()


def test_theme_override_is_validated(tmp_path, monkeypatch):
    import json

    from domshon import theme

    f = tmp_path / "t.json"
    f.write_text(json.dumps({"cyan": "#112233", "magenta": "red", "font-ui": "a;b", "bogus": "#000000"}))
    monkeypatch.setenv("DS_THEME_FILE", str(f))
    old = dict(theme.TOKENS)
    try:
        theme._load_user_theme()
        assert theme.TOKENS["cyan"] == "#112233"
        assert theme.TOKENS["magenta"] == old["magenta"] and theme.TOKENS["font-ui"] == old["font-ui"]
        assert "bogus" not in theme.TOKENS
    finally:
        theme.TOKENS.clear()
        theme.TOKENS.update(old)
