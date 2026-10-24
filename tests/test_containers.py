"""Container alerts: what the agent reads from docker ps, and when the server alerts."""

import subprocess

import pytest

import agent
from alerting import AlertEngine, CONTAINER_HELD

T0 = 1_000_000.0


def report(*containers, last_seen=T0):
    return {"nas": {"cpu_usage": 5, "last_seen": last_seen, "docker_containers": list(containers)}}


def ctr(name="gluetun", state="Running", health=None, uptime=3600, status="Up 1 hour"):
    return {"name": name, "state": state, "health": health, "uptime": uptime, "status": status}


def run(engine, start, end, make, step=30):
    """Report every `step` seconds from start to end; returns every event."""
    events = []
    for t in range(start, end + 1, step):
        events += engine.check_nodes(T0 + t, report(*make(t), last_seen=T0 + t), {})
    return events


def names(events, kind="fired"):
    return [a["title"] if k == "fired" else a["resolved_title"] for k, a in events if k == kind]


@pytest.mark.parametrize("status, expected", [
    ("Up 3 hours (unhealthy)", ("unhealthy", 10800)),
    ("Up 12 seconds (health: starting)", ("starting", 12)),
    ("Up About a minute (healthy)", ("healthy", 60)),
    ("Up Less than a second", (None, 0)),
    ("Up 2 weeks", (None, 1209600)),
    ("Exited (137) 5 minutes ago", (None, None)),
    ("Restarting (1) 8 seconds ago", (None, None)),
])
def test_parse_docker_status(status, expected):
    assert agent.parse_docker_status(status) == expected


def test_containers_carry_health_and_uptime(monkeypatch):
    out = "gluetun\trunning\tqmcgaw/gluetun\tUp 2 hours (unhealthy)\nold\texited\tbusybox\tExited (0) 3 weeks ago\n"
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    gluetun, old = agent.get_docker_containers()
    assert gluetun == {"name": "gluetun", "state": "Running", "image": "qmcgaw/gluetun",
                       "status": "Up 2 hours (unhealthy)", "health": "unhealthy", "uptime": 7200}
    assert old["state"] == "Exited" and old["uptime"] is None


def test_a_container_that_stops_alerts_after_two_minutes_and_resolves_when_back():
    engine = AlertEngine(started_at=T0 - 3600)
    stopped = ctr(state="Exited", uptime=None, status="Exited (1) 1 minute ago")
    assert run(engine, 0, 0, lambda t: [ctr()]) == []
    early = run(engine, 30, 30 + CONTAINER_HELD - 30, lambda t: [stopped])
    assert early == []
    fired = run(engine, 30 + CONTAINER_HELD, 30 + CONTAINER_HELD, lambda t: [stopped])
    assert names(fired) == ["nas: gluetun stopped"]
    assert fired[0][1]["message"] == "Docker says: Exited (1) 1 minute ago."
    # Started again: fixed once it has stayed up for a minute.
    assert run(engine, 200, 200, lambda t: [ctr(uptime=10)]) == []
    assert names(run(engine, 260, 260, lambda t: [ctr(uptime=70)]), "resolved") == ["nas: gluetun is running again"]


def test_a_crash_loop_alerts_even_though_it_is_sometimes_running():
    engine = AlertEngine(started_at=T0 - 3600)
    run(engine, 0, 0, lambda t: [ctr()])
    loop = lambda t: [ctr(state="Restarting", uptime=None, status="Restarting (1) 2 seconds ago")
                      if t % 60 else ctr(uptime=5, status="Up 5 seconds")]
    events = run(engine, 30, 30 + CONTAINER_HELD, loop)
    assert names(events) == ["nas: gluetun keeps restarting"]


def test_unhealthy_is_a_warning_and_clears_when_healthy():
    engine = AlertEngine(started_at=T0 - 3600)
    events = run(engine, 0, CONTAINER_HELD, lambda t: [ctr(health="unhealthy")])
    assert names(events) == ["nas: gluetun is unhealthy"] and events[0][1]["severity"] == "warn"
    resolved = run(engine, 150, 150, lambda t: [ctr(health="healthy")])
    assert names(resolved, "resolved") == ["nas: gluetun is healthy again"]


def test_quick_recreates_old_stopped_containers_and_blank_lists_are_ignored():
    engine = AlertEngine(started_at=T0 - 3600)
    old = ctr("certbot-once", state="Exited", uptime=None, status="Exited (0) 3 weeks ago")
    # Recreated by compose: gone for a moment, then up again with a new uptime.
    recreate = lambda t: [old] + ([] if t == 60 else [ctr(uptime=max(1, t - 60))])
    assert run(engine, 0, 600, recreate) == []
    # docker did not answer: an empty list changes nothing.
    stopped = ctr(state="Exited", uptime=None)
    events = run(engine, 630, 630 + CONTAINER_HELD, lambda t: [old, stopped])
    assert names(events) == ["nas: gluetun stopped"]
    assert run(engine, 900, 900, lambda t: []) == []
    assert "container:nas:gluetun" in engine.active
    # Removed for good: closed quietly.
    assert [k for k, _ in run(engine, 930, 930, lambda t: [old])] == ["dropped"]
