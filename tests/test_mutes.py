"""Muting a node for maintenance: what it holds back, and when it ends."""

import pytest
from fastapi.testclient import TestClient

import main
import mutes
from alerting import AlertEngine, OFFLINE_AFTER

T0 = 1_000_000.0
SERVICES = [{"name": "Immich", "url": "http://192.168.0.104:2283"},
            {"name": "Nextcloud", "url": "https://192.168.0.100"},
            {"name": "Homarr", "url": "http://haziq-apu2:7575"}]


def test_a_muted_node_raises_nothing_new_and_alerts_once_the_mute_ends():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"srv": {"last_seen": T0, "cpu_usage": 5}}
    engine.muted_hosts = {"srv"}
    assert engine.check_nodes(T0 + OFFLINE_AFTER + 60, devices, {}) == []   # down, but muted
    engine.muted_hosts = set()
    events = engine.check_nodes(T0 + OFFLINE_AFTER + 90, devices, {})
    assert [a["title"] for _, a in events] == ["srv is offline"]


def test_open_alerts_still_resolve_while_muted():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"srv": {"last_seen": T0, "cpu_usage": 5}}
    engine.check_nodes(T0 + OFFLINE_AFTER, devices, {})
    engine.muted_hosts = {"srv"}
    devices["srv"]["last_seen"] = T0 + 400
    events = engine.check_nodes(T0 + 401, devices, {})
    assert [(k, a["resolved_title"]) for k, a in events] == [("resolved", "srv is back online")]


def test_services_on_a_muted_node_are_muted_too():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"mhaziq-server": {"ip": "192.168.0.104"}, "haziq-apu2": {"ip": "192.168.0.106"}}
    engine.muted_services = mutes.services_on({"mhaziq-server", "haziq-apu2"}, devices, SERVICES)
    assert engine.muted_services == {"Immich", "Homarr"}
    down = [{**s, "online": False} for s in SERVICES]
    engine.check_services(T0, down)
    assert [a["title"] for _, a in engine.check_services(T0 + 30, down)] == ["Nextcloud is down"]


def test_a_timed_mute_ends_on_time():
    mute_map = {"srv": mutes.timed(T0, 3600)}
    devices = {"srv": {"last_seen": T0}}
    assert not mutes.advance(mute_map, devices, T0 + 3599) and "srv" in mute_map
    assert mutes.advance(mute_map, devices, T0 + 3600) and mute_map == {}


def test_until_back_waits_for_a_reboot_then_gives_services_time_to_start():
    devices = {"srv": {"last_seen": T0}}
    mute_map = {"srv": mutes.until_back(T0, devices["srv"])}
    # Still up: nothing changes, however long it takes.
    assert not mutes.note_report(mute_map["srv"], T0, T0 + 5)
    # Reboots: 40 s without a report, then it is back.
    assert mutes.note_report(mute_map["srv"], T0 + 5, T0 + 45)
    assert mutes.public(mute_map)["srv"] == {"since": T0, "until": T0 + 45 + mutes.BACK_GRACE,
                                             "until_back": True, "back": True}
    mutes.advance(mute_map, devices, T0 + 45 + mutes.BACK_GRACE - 1)
    assert "srv" in mute_map
    assert mutes.advance(mute_map, devices, T0 + 45 + mutes.BACK_GRACE) and mute_map == {}


def test_until_back_on_a_node_that_is_already_down_or_never_comes_back():
    devices = {"srv": {"last_seen": T0 - 600}}
    mute = mutes.until_back(T0, devices["srv"])
    assert mute["gone"] and mutes.note_report(mute, T0 - 600, T0 + 60)   # its first report is the return
    stuck = {"srv": mutes.until_back(T0, {"last_seen": T0})}
    assert mutes.advance(stuck, devices, T0 + 60) and stuck["srv"]["gone"]   # went quiet
    assert mutes.advance(stuck, devices, T0 + mutes.BACK_LIMIT) and stuck == {}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "history.db"))
    main.init_db()
    main.devices.clear()
    main.node_mutes.clear()
    c = TestClient(main.app)
    token = c.post("/api/auth/register", json={"username": "tester", "password": "secret123"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def test_mute_api_validates_persists_and_unmutes(client):
    key = main.get_agent_auth_key()
    client.post("/api/report", json={"hostname": "srv", "ip": "192.168.0.104"}, headers={"X-Agent-Key": key})
    assert client.put("/api/mutes/ghost", json={"minutes": 60}).status_code == 404
    assert client.put("/api/mutes/srv", json={"minutes": 0}).status_code == 400
    assert client.put("/api/mutes/srv", json={"minutes": True}).status_code == 400
    muted = client.put("/api/mutes/srv", json={"minutes": 60}).json()
    assert muted["srv"]["until"] - muted["srv"]["since"] == 3600
    assert main.alert_engine.muted_hosts == {"srv"}
    assert mutes.read(main.DB_PATH).keys() == {"srv"}          # survives a restart
    assert client.put("/api/mutes/srv", json={"until_back": True}).json()["srv"]["until_back"] is True
    assert client.delete("/api/mutes/srv").json() == {}
    assert main.alert_engine.muted_hosts == set() and client.get("/api/mutes").json() == {}
