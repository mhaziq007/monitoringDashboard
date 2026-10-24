"""Backend API: reports, devices, history, disk alert settings, uptime."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "history.db"))
    main.init_db()
    main.devices.clear()
    main.power_history.clear()
    # Not used as a context manager, so the startup tasks (pings, service
    # probes) never start and nothing reaches the network.
    c = TestClient(main.app)
    token = c.post("/api/auth/register", json={"username": "tester", "password": "secret123"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def report(c, hostname, **fields):
    body = {"hostname": hostname, "ip": "192.168.0.10", "cpu_usage": 10,
            "memory": {"total": 1000, "used": 250, "free": 750}, **fields}
    return c.post("/api/report", json=body, headers={"X-Agent-Key": main.get_agent_auth_key()})


def db(sql, args=()):
    conn = sqlite3.connect(main.DB_PATH)
    with conn:
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def test_endpoints_need_a_session(client):
    assert TestClient(main.app).get("/api/devices").status_code == 401


def test_reports_need_the_agent_key(client):
    bad = client.post("/api/report", json={"hostname": "x"}, headers={"X-Agent-Key": "wrong"})
    assert bad.status_code == 401
    assert report(client, "nas").json() == {"status": "ok"}
    device = client.get("/api/devices").json()["nas"]
    assert device["online"] is True and device["cpu_usage"] == 10


def test_forgetting_a_node_drops_it_and_its_history(client):
    assert client.delete("/api/devices/nas").status_code == 404
    report(client, "nas")
    db("INSERT INTO node_log VALUES ('nas', ?, 1, 2, 3, 4)", (time.time(),))
    assert client.delete("/api/devices/nas").status_code == 200
    assert "nas" not in client.get("/api/devices").json()
    assert db("SELECT COUNT(*) FROM node_log WHERE hostname = 'nas'") == [(0,)]


def test_disk_alert_settings_validate_and_persist(client):
    url = "/api/settings/disk-alerts"
    for bad in ({"hostname": "nas", "mount": "/m", "trip": 40}, {"hostname": "nas", "mount": "/m", "trip": "never"},
                {"mount": "/m", "trip": 95}, {"hostname": "nas", "mount": "/m", "trip": True}):
        assert client.put(url, json=bad).status_code == 400, bad
    client.put(url, json={"hostname": "nas", "mount": "/mnt/media", "trip": "off"})
    client.put(url, json={"hostname": "nas", "mount": "/mnt/backup", "trip": 95})
    assert client.get(url).json() == {"nas": {"/mnt/media": "off", "/mnt/backup": 95}}
    client.put(url, json={"hostname": "nas", "mount": "/mnt/media", "trip": None})
    client.put(url, json={"hostname": "nas", "mount": "/mnt/backup", "trip": None})
    assert client.get(url).json() == {}


def test_device_history_is_bucketed_by_range(client):
    now = time.time()
    start = (int(now) // 300) * 300 - 3000           # well inside the 24 h window
    db("INSERT INTO node_log VALUES ('nas', ?, 10, 50, 100, 10)", (start + 10,))
    db("INSERT INTO node_log VALUES ('nas', ?, 30, 50, 300, 30)", (start + 70,))
    db("INSERT INTO node_log VALUES ('pc', ?, 99, 99, 9, 9)", (start + 10,))
    points = client.get("/api/devices/nas/history?range=24h").json()
    assert points == [{"time": start, "cpu": 20.0, "mem": 50.0, "net_down": 200.0, "net_up": 20.0}]


def test_older_databases_gain_the_fleet_columns(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE power_log (timestamp REAL, power REAL)")
    conn.execute("INSERT INTO power_log VALUES (?, 42)", (time.time() - 600,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(main, "DB_PATH", path)
    main.init_db()
    c = TestClient(main.app)
    token = c.post("/api/auth/register", json={"username": "tester", "password": "secret123"}).json()["token"]
    points = c.get("/api/power-history?range=24h", headers={"Authorization": f"Bearer {token}"}).json()
    assert points[0]["power"] == 42 and points[0]["cpu"] is None and points[0]["net_up"] is None


def test_service_uptime_has_48_slots_with_gaps(client):
    now = time.time()
    for i in range(10):                               # the last five minutes: 9 up, 1 down
        db("INSERT INTO service_log VALUES ('Jellyfin', ?, ?, 5)", (now - 300 + i * 30, 0 if i == 4 else 1))
    history = client.get("/api/services/history?range=24h").json()["Jellyfin"]
    assert len(history["slots"]) == 48
    assert history["uptime"] == 90.0
    assert history["slots"][0] is None                # nothing recorded a day ago
    assert history["slots"][-1]["up"] == pytest.approx(0.9)


def test_fleet_load_counts_vms_and_weights_memory():
    active = [
        {"cpu_usage": 10, "memory": {"total": 4, "used": 3}, "network": {"down_speed": 100, "up_speed": 10}},
        {"cpu_usage": 30, "memory": {"total": 60, "used": 6}, "network": {"down_speed": 50, "up_speed": 5}, "is_vm": True},
    ]
    load = main.fleet_load(active)
    assert load["cpu"] == 20
    assert load["mem"] == pytest.approx(100 * 9 / 64)   # used over total, not a mean of 75% and 10%
    assert (load["net_down"], load["net_up"]) == (150, 15)
    assert main.fleet_load([]) == {"cpu": None, "mem": None, "net_down": None, "net_up": None}
