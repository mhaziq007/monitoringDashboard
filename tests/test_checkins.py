"""Check-ins: when a job counts as late or failed, and the ping and management API."""

import pytest
from fastapi.testclient import TestClient

import checkins
import main
from alerting import AlertEngine

T0 = 1_000_000.0
DAY, HOUR = 86400, 3600


def check(**fields):
    base = {"id": 1, "name": "Backup to OCI", "every": DAY, "grace": HOUR, "created": T0,
            "last_ping": None, "last_status": None, "last_note": ""}
    return {**base, **fields}


def test_state_follows_the_schedule():
    assert checkins.state(check(), T0 + DAY) == "new"
    assert checkins.state(check(), T0 + DAY + HOUR + 1) == "late"          # never reported in
    assert checkins.state(check(last_ping=T0 + DAY, last_status="ok"), T0 + 2 * DAY + HOUR) == "ok"
    assert checkins.state(check(last_ping=T0 + DAY, last_status="ok"), T0 + 2 * DAY + HOUR + 1) == "late"
    assert checkins.state(check(last_ping=T0 + 60, last_status="fail"), T0 + 120) == "failed"


def with_state(c, now):
    return {**c, "state": checkins.state(c, now)}


def test_late_and_failed_alert_and_a_good_ping_resolves():
    engine = AlertEngine(started_at=T0)
    now = T0 + 2 * DAY
    late = engine.check_checkins(now, [with_state(check(last_ping=T0, last_status="ok"), now)])
    assert [(a["title"], a["message"]) for _, a in late] == [
        ("Backup to OCI is late", "No check-in for 2 d; expected every 1 d.")]
    fixed = engine.check_checkins(now + 60, [with_state(check(last_ping=now + 30, last_status="ok"), now + 60)])
    assert [(k, a["resolved_title"]) for k, a in fixed] == [("resolved", "Backup to OCI checked in")]
    failed = engine.check_checkins(now + 90, [with_state(
        check(last_ping=now + 90, last_status="fail", last_note="rsync: connection refused"), now + 90)])
    assert failed[0][1]["message"] == "It reported a failure: rsync: connection refused"
    # Deleted while failing: closed quietly.
    assert [k for k, _ in engine.check_checkins(now + 120, [])] == ["dropped"]


def test_node_rules_leave_check_in_alerts_alone():
    engine = AlertEngine(started_at=T0)
    engine.check_checkins(T0 + 2 * DAY, [with_state(check(), T0 + 2 * DAY)])
    assert engine.check_nodes(T0 + 2 * DAY, {}, {}) == []
    assert "checkin:1" in engine.active


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


def test_check_ins_validate_and_pings_need_only_the_token(client):
    assert client.post("/api/checkins", json={"name": "", "every": DAY, "grace": HOUR}).status_code == 400
    assert client.post("/api/checkins", json={"name": "Backup", "every": 10, "grace": HOUR}).status_code == 400
    assert client.post("/api/checkins", json={"name": "Backup", "every": DAY}).status_code == 400
    made = client.post("/api/checkins", json={"name": "Backup to OCI", "every": DAY, "grace": HOUR}).json()
    assert made["state"] == "new" and len(made["token"]) >= 16

    anonymous = TestClient(main.app)
    assert anonymous.get("/api/checkins").status_code == 401
    assert anonymous.get("/ping/not-a-token").status_code == 404
    assert anonymous.get(f"/ping/{made['token']}/maybe").status_code == 404
    assert anonymous.get(f"/ping/{made['token']}").text == "OK\n"
    assert client.get("/api/checkins").json()[0]["state"] == "ok"

    # A failure alerts at once, with the body it sent.
    assert anonymous.post(f"/ping/{made['token']}/fail", content=b"disk full\n").status_code == 200
    assert client.sent[-1] == ("Backup to OCI failed", "It reported a failure: disk full", "crit")
    assert client.get("/api/alerts").json()["active"][0]["kind"] == "checkin"
    anonymous.head(f"/ping/{made['token']}")
    assert client.sent[-1][0] == "Backup to OCI checked in"

    assert client.put(f"/api/checkins/{made['id']}", json={"every": 3600}).json()["every"] == 3600
    assert client.put("/api/checkins/999", json={"every": 3600}).status_code == 404
    assert client.delete(f"/api/checkins/{made['id']}").json() == {"status": "ok"}
    assert anonymous.get(f"/ping/{made['token']}").status_code == 404
