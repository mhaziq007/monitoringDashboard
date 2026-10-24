"""Backups of the dashboard's database: the snapshot, its key, and the agent that keeps the copies."""

import gzip
import hashlib
import io
import os
import sqlite3
import time
import urllib.error
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import backup as agent_backup
import backups
import checkins
import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "history.db"))
    main.init_db()
    main.alert_engine.active.clear()
    sent = []

    async def deliver(title, message, tone):
        sent.append((title, message, tone))
    monkeypatch.setattr(main, "deliver", deliver)
    c = TestClient(main.app)
    token = c.post("/api/auth/register", json={"username": "tester", "password": "secret123"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    c.sent = sent
    return c


def test_a_snapshot_is_a_checked_gzip_of_the_whole_database(client, tmp_path):
    path, digest, size = backups.snapshot(main.DB_PATH, str(tmp_path / "tmp"))
    data = open(path, "rb").read()
    assert hashlib.sha256(data).hexdigest() == digest and len(data) == size
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(data))
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT username FROM users").fetchall() == [("tester",)]
    conn.close()


def test_snapshots_need_the_backup_key_or_a_session(client):
    key = client.get("/api/backup").json()["key"]
    anonymous = TestClient(main.app)
    assert anonymous.get("/api/backup/snapshot").status_code == 401
    assert anonymous.get("/api/backup/snapshot", headers={"X-Backup-Key": "wrong"}).status_code == 401
    # The shared agent key is not enough.
    assert anonymous.get("/api/backup/snapshot",
                         headers={"X-Backup-Key": main.get_agent_auth_key()}).status_code == 401
    by_key = anonymous.get("/api/backup/snapshot", headers={"X-Backup-Key": key})
    assert by_key.status_code == 200
    assert hashlib.sha256(by_key.content).hexdigest() == by_key.headers["X-Backup-SHA256"]
    assert client.get("/api/backup/snapshot").status_code == 200   # the browser's download
    rotated = client.post("/api/backup/key").json()["key"]
    assert rotated != key
    assert anonymous.get("/api/backup/snapshot", headers={"X-Backup-Key": key}).status_code == 401


def test_reports_feed_a_built_in_check_in_that_alerts_on_failure(client):
    key = client.get("/api/backup").json()["key"]
    report = lambda body, k=key: TestClient(main.app).post("/api/backup/report", json=body, headers={"X-Backup-Key": k})
    assert report({"ok": True, "detail": "x", "host": "apu2"}, "wrong").status_code == 401
    assert report({"ok": "yes", "detail": "x", "host": "apu2"}).status_code == 400
    assert report({"ok": True, "detail": "homelab-dashboard-2026-10-24.db.gz, 48.0 MB, 1 kept",
                   "host": "haziq-apu2"}).status_code == 200
    status = client.get("/api/backup").json()
    assert status["last"]["ok"] and status["last"]["host"] == "haziq-apu2"
    [check] = client.get("/api/checkins").json()
    assert check["name"] == "Dashboard backup to haziq-apu2" and check["state"] == "ok"
    report({"ok": False, "detail": "No space left on device", "host": "haziq-apu2"})
    assert client.sent[-1] == ("Dashboard backup to haziq-apu2 failed",
                               "It reported a failure: No space left on device", "crit")
    # Deleted by hand: the next report makes it again.
    client.delete(f"/api/checkins/{check['id']}")
    report({"ok": True, "detail": "ok", "host": "haziq-apu2"})
    assert [c["name"] for c in client.get("/api/checkins").json()] == ["Dashboard backup to haziq-apu2"]


class Response:
    def __init__(self, r):
        self.headers, self.body = r.headers, io.BytesIO(r.content)

    def read(self, n=-1):
        return self.body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def through(client, tamper=None):
    """urlopen for the agent, answered by the server under test."""
    def urlopen(req, timeout):
        path = urllib.parse.urlparse(req.full_url).path
        headers = dict(req.header_items())
        r = (client.get(path, headers=headers) if req.data is None
             else client.post(path, content=req.data, headers=headers))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, "error", {}, io.BytesIO(r.content))
        if tamper and path.endswith("/snapshot"):
            r._content = tamper(r.content)
        return Response(r)
    return urlopen


NIGHT = time.mktime((2026, 10, 24, 3, 30, 0, 0, 0, -1))   # 03:30 local time


def test_the_agent_keeps_a_checked_copy_and_reports_it(client, tmp_path, monkeypatch):
    key = client.get("/api/backup").json()["key"]
    anonymous = TestClient(main.app)
    monkeypatch.setattr(agent_backup.urllib.request, "urlopen", through(anonymous))
    keeper = agent_backup.Backup("http://192.168.0.107:8000/api/report", str(tmp_path / "copies"), key, 3, 14)
    assert not keeper.due(NIGHT - 3600)        # 02:30: not yet
    assert keeper.due(NIGHT)
    keeper.run(NIGHT)
    saved = tmp_path / "copies" / "homelab-dashboard-2026-10-24.db.gz"
    assert gzip.decompress(saved.read_bytes())[:16] == b"SQLite format 3\x00"
    assert not keeper.due(NIGHT + 7200)        # today's copy is there
    last = client.get("/api/backup").json()["last"]
    assert last["ok"] and last["detail"].startswith("homelab-dashboard-2026-10-24.db.gz, ")


def test_a_damaged_or_refused_download_leaves_nothing_behind(client, tmp_path, monkeypatch):
    key = client.get("/api/backup").json()["key"]
    anonymous = TestClient(main.app)
    keeper = agent_backup.Backup("http://192.168.0.107:8000/api/report", str(tmp_path / "copies"), key, 3, 14)
    monkeypatch.setattr(agent_backup.urllib.request, "urlopen",
                        through(anonymous, tamper=lambda body: body[:-10] + b"0123456789"))
    with pytest.raises(RuntimeError, match="checksum"):
        keeper.fetch(NIGHT)
    keeper.key = "wrong"
    monkeypatch.setattr(agent_backup.urllib.request, "urlopen", through(anonymous))
    with pytest.raises(RuntimeError, match="rejected the backup key"):
        keeper.fetch(NIGHT)
    assert os.listdir(tmp_path / "copies") == []


def test_only_the_newest_copies_are_kept(tmp_path):
    keeper = agent_backup.Backup("http://x/api/report", str(tmp_path), "k", 3, 14)
    for day in range(1, 17):
        (tmp_path / f"homelab-dashboard-2026-10-{day:02d}.db.gz").write_bytes(b"x")
    (tmp_path / "homelab-dashboard-2026-10-17.db.gz.part").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("mine")
    assert keeper.prune() == 14
    assert sorted(os.listdir(tmp_path))[0] == "homelab-dashboard-2026-10-03.db.gz"
    assert "notes.txt" in os.listdir(tmp_path) and len(os.listdir(tmp_path)) == 15


def test_backups_are_off_unless_configured():
    assert not agent_backup.Backup("http://x/api/report", "", "k").enabled
    assert not agent_backup.Backup("http://x/api/report", "/var/backups/x", "").enabled
    assert agent_backup.Backup("http://x:8000/api/report", "/d", "k").base == "http://x:8000"
