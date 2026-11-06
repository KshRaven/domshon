"""Core tests: quota/space guards, extension parsing, path safety and upload limits over the real API."""
import pytest

from domshon.common.scan import ignored
from domshon.config import _parse_exts
from domshon.server.quota import MB, check_space, dir_size


def test_parse_exts_normalises():
    assert _parse_exts("exe, .PHP;sh  exe") == [".exe", ".php", ".sh"]
    assert _parse_exts("") == []
    with pytest.raises(ValueError):
        _parse_exts("bad/ext")


def test_check_space():
    check_space(used=0, quota=10 * MB, free=0, reserve=0, size=10 * MB)
    with pytest.raises(ValueError):
        check_space(used=5 * MB, quota=10 * MB, free=0, reserve=0, size=6 * MB)
    with pytest.raises(ValueError):
        check_space(used=0, quota=0, free=5 * MB, reserve=4 * MB, size=2 * MB)
    check_space(used=0, quota=0, free=5 * MB, reserve=4 * MB, size=1 * MB)


def test_dir_size_and_ignored(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.bin").write_bytes(b"x" * 100)
    (tmp_path / "g.bin").write_bytes(b"y" * 50)
    assert dir_size(tmp_path) == 150
    assert ignored("x.dspart") and ignored(".DS_Store") and not ignored("notes.txt")


def test_service_config_dir_falls_back_when_write_is_denied(tmp_path, monkeypatch):
    from domshon import config

    candidate = tmp_path / "Domshon" / "config"
    candidate.mkdir(parents=True)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    def deny_write(**kwargs):
        raise PermissionError("service config is not writable")

    monkeypatch.setattr(config.tempfile, "TemporaryFile", deny_write)
    assert config._service_config_dir() is None


def test_port_in_use_message_and_probe():
    import socket

    from domshon.winservice import _port_conflict_message, _port_in_use

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert _port_in_use("127.0.0.1", port)

    message = _port_conflict_message("127.0.0.1", port)
    assert f"Port {port} is currently in use" in message
    assert "domshon service install --port 8788" in message


def test_service_install_accepts_port_option():
    from domshon.cli import build_parser

    args = build_parser().parse_args(["service", "install", "--port", "8788"])
    assert args.port == 8788


def test_service_settings_persists_port_override_without_shell_env(tmp_path, monkeypatch):
    import json
    import os

    from domshon import winservice

    monkeypatch.setattr(winservice, "program_data", lambda: tmp_path)
    env_file = tmp_path / "domshon.env"
    env_file.write_text("DS_HOST=127.0.0.1\nDS_PORT=8787\n", encoding="utf-8")
    monkeypatch.setenv("DS_PORT", "9000")

    assert winservice._service_settings(str(env_file), 8788) == ("127.0.0.1", 8788)
    saved = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
    assert saved["port"] == 8788
    assert os.environ["DS_PORT"] == "9000"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from domshon.config import Settings
    from domshon.server.app import create_app

    monkeypatch.setenv("DS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("DS_STORAGE_ROOT", str(tmp_path / "files"))
    monkeypatch.setenv("DS_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DS_ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("DS_QUOTA_MB", "1")
    monkeypatch.setenv("DS_BLOCKED_EXTENSIONS", ".exe")
    with TestClient(create_app(Settings())) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"}).json()["token"]
        c.headers["Authorization"] = "Bearer " + tok
        yield c


def test_upload_ok_and_listing(client):
    assert client.put("/api/files", params={"path": "docs/a.txt"}, content=b"hello").status_code == 200
    names = [i["name"] for i in client.get("/api/files", params={"path": "docs"}).json()["items"]]
    assert names == ["a.txt"]


def test_quota_rejects_big_upload(client):
    r = client.put("/api/files", params={"path": "big.bin"}, content=b"x" * (2 * MB))
    assert r.status_code == 507


def test_blocked_extension(client):
    assert client.put("/api/files", params={"path": "run.exe"}, content=b"MZ").status_code == 400


def test_path_escape_rejected(client):
    assert client.get("/api/files", params={"path": "../.."}).status_code == 400


def test_unauthenticated_rejected(client):
    client.headers.pop("Authorization")
    assert client.get("/api/files").status_code == 401
