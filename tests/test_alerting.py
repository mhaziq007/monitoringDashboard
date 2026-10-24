"""Alert rules: when alerts fire, resolve and stay quiet, at production timings."""

import alerting
from alerting import AlertEngine, OFFLINE_AFTER

T0 = 1_000_000.0


def node(cpu=10, temp=50, disks=None, last_seen=T0, **extra):
    return {"cpu_usage": cpu, "temp": temp, "last_seen": last_seen,
            "disks": disks if disks is not None else [], **extra}


def disk(mount="/mnt/media", percent=50.0):
    return {"mount": mount, "percent": percent, "free": 1024 ** 4}


def titles(events, kind="fired"):
    return [a["title"] if k == "fired" else a["resolved_title"] for k, a in events if k == kind]


def test_offline_fires_after_the_quiet_period_and_resolves_on_a_report():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"nas": node(last_seen=T0)}
    assert engine.check_nodes(T0 + OFFLINE_AFTER - 1, devices, {}) == []
    assert titles(engine.check_nodes(T0 + OFFLINE_AFTER, devices, {})) == ["nas is offline"]
    # Still offline: no repeat.
    assert engine.check_nodes(T0 + OFFLINE_AFTER + 600, devices, {}) == []
    devices["nas"]["last_seen"] = T0 + 900
    events = engine.check_nodes(T0 + 901, devices, {})
    assert titles(events, "resolved") == ["nas is back online"]
    assert "Offline for 15 min" in events[0][1]["resolved_message"]


def test_startup_grace_neither_fires_nor_resolves_offline():
    # Reports from before a restart look old; nodes get OFFLINE_AFTER to report in.
    engine = AlertEngine(started_at=T0)
    devices = {"nas": node(last_seen=T0 - 7200), "vm": node(last_seen=T0 - 86400)}
    engine.restore([{"key": "offline:vm", "kind": "offline", "host": "vm", "severity": "crit",
                     "title": "vm is offline", "message": "", "started": T0 - 80000, "id": 1}])
    assert engine.check_nodes(T0 + 30, devices, {}) == []          # neither fired nor "back online"
    devices["nas"]["last_seen"] = T0 + 40                          # nas reports in
    assert engine.check_nodes(T0 + OFFLINE_AFTER + 1, devices, {}) == []   # vm already open: not re-sent
    assert set(engine.active) == {"offline:vm"}


def test_cpu_must_stay_high_for_five_minutes():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"srv": node(cpu=95)}
    for t in range(0, 300, 30):
        devices["srv"]["last_seen"] = T0 + t
        assert engine.check_nodes(T0 + t, devices, {}) == []
    devices["srv"]["last_seen"] = T0 + 300
    assert titles(engine.check_nodes(T0 + 300, devices, {})) == ["srv CPU at 95%"]


def test_a_short_spike_never_fires_and_hysteresis_holds_it_open():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"srv": node(cpu=95)}
    engine.check_nodes(T0, devices, {})
    devices["srv"].update(cpu=40, last_seen=T0 + 60)
    engine.check_nodes(T0 + 60, devices, {})                      # spike over: pending dropped
    devices["srv"].update(cpu=95, last_seen=T0 + 120)
    assert engine.check_nodes(T0 + 120, devices, {}) == []
    assert engine.check_nodes(T0 + 400, {"srv": node(cpu=95, last_seen=T0 + 400)}, {})
    # 80% is below the trip (85) but above the clear point (75): stays open.
    assert engine.check_nodes(T0 + 430, {"srv": node(cpu=80, last_seen=T0 + 430)}, {}) == []
    assert titles(engine.check_nodes(T0 + 460, {"srv": node(cpu=70, last_seen=T0 + 460)}, {}),
                  "resolved") == ["srv CPU back to 70%"]


def test_stale_figures_from_a_silent_node_raise_nothing():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"srv": node(cpu=99, temp=95, disks=[disk(percent=99)], last_seen=T0 - 120)}
    events = engine.check_nodes(T0 + 400, devices, {})
    assert all(a["kind"] == "offline" for _, a in events)


def test_disks_use_their_own_threshold_or_none():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"nas": node(disks=[disk("/mnt/media", 93), disk("/mnt/backup", 92), disk("/", 70)])}
    settings = {"nas": {"/mnt/media": "off", "/mnt/backup": 95}}
    assert engine.check_nodes(T0, devices, settings) == []
    settings["nas"]["/mnt/backup"] = 90
    assert titles(engine.check_nodes(T0 + 30, devices, settings)) == ["nas /mnt/backup at 92%"]


def test_switching_a_disk_off_closes_its_alert():
    engine = AlertEngine(started_at=T0 - 3600)
    devices = {"nas": node(disks=[disk("/mnt/media", 93)])}
    assert titles(engine.check_nodes(T0, devices, {})) == ["nas /mnt/media at 93%"]
    events = engine.check_nodes(T0 + 30, devices, {"nas": {"/mnt/media": "off"}})
    assert titles(events, "resolved") == ["nas /mnt/media: alert switched off"]


def test_a_service_needs_two_failed_probes_and_resolves_when_it_answers():
    engine = AlertEngine(started_at=T0)
    jellyfin = {"name": "Jellyfin", "online": False}
    assert engine.check_services(T0, [jellyfin]) == []            # one failure is not enough
    assert titles(engine.check_services(T0 + 30, [jellyfin])) == ["Jellyfin is down"]
    assert engine.check_services(T0 + 60, [jellyfin]) == []
    jellyfin["online"] = True
    assert titles(engine.check_services(T0 + 90, [jellyfin]), "resolved") == ["Jellyfin is back up"]


def test_removed_nodes_and_services_close_quietly():
    engine = AlertEngine(started_at=T0 - 3600)
    engine.check_nodes(T0, {"old": node(last_seen=T0 - 600)}, {})
    engine.check_services(T0, [{"name": "Gone", "online": False}])
    engine.check_services(T0 + 30, [{"name": "Gone", "online": False}])
    assert set(engine.active) == {"offline:old", "service:Gone"}
    kinds = [k for k, _ in engine.check_nodes(T0 + 60, {}, {}) + engine.check_services(T0 + 60, [])]
    assert kinds == ["dropped", "dropped"] and engine.active == {}


def test_digest_lists_only_nodes_with_updates():
    devices = {
        "srv": {"pending_updates": 3, "docker_containers": [
            {"name": "sonarr", "update": "available"}, {"name": "jellyfin", "update": "current"}]},
        "nas": {"pending_updates": 0, "docker_containers": []},
        "pc": {"pending_updates": 1},
    }
    assert alerting.build_digest(devices) == "pc: 1 OS package\nsrv: 3 OS packages, 1 image (sonarr)"
    assert alerting.build_digest({"nas": devices["nas"]}) is None


def test_ntfy_publish_sends_json_to_the_server_root(monkeypatch):
    sent = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"{}"

    def fake_urlopen(req, timeout):
        sent.update(url=req.full_url, headers=dict(req.header_items()), body=req.data)
        return Response()

    monkeypatch.setattr(alerting.urllib.request, "urlopen", fake_urlopen)
    alerting.ntfy_publish("http://192.168.0.105:7070/homelab", "tk_abc", "nas is offline", "No report.", "crit",
                          click="http://192.168.0.107:8000/alerts.html")
    import json
    body = json.loads(sent["body"])
    assert sent["url"] == "http://192.168.0.105:7070"
    assert sent["headers"]["Authorization"] == "Bearer tk_abc"
    assert body == {"topic": "homelab", "title": "nas is offline", "message": "No report.", "priority": 4,
                    "tags": ["rotating_light"], "click": "http://192.168.0.107:8000/alerts.html"}


def test_ntfy_url_without_a_topic_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        alerting.ntfy_publish("http://192.168.0.105:7070", "", "t", "m", "crit")


TOKEN = "123456789:AAH-test_token"


class FakeBotApi:
    """Stands in for urlopen: records each call and answers like the Bot API."""

    def __init__(self, result=True, status=200, description="", retry_after=None):
        self.calls, self.result, self.description = [], result, description
        # One status for every call, or a list taken in turn.
        self.statuses = status if isinstance(status, list) else None
        self.status, self.retry_after = status, retry_after

    def __call__(self, req, timeout):
        import io
        import json
        self.calls.append((req.full_url, json.loads(req.data)))
        status = self.statuses.pop(0) if self.statuses is not None else self.status
        if status != 200:
            answer = {"ok": False, "description": self.description}
            if self.retry_after is not None:
                answer["parameters"] = {"retry_after": self.retry_after}
            raise alerting.urllib.error.HTTPError(req.full_url, status, "error", {},
                                                  io.BytesIO(json.dumps(answer).encode()))
        test = self

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps({"ok": True, "result": test.result}).encode()
        return Response()


def test_telegram_send_escapes_html_and_mutes_good_news(monkeypatch):
    api = FakeBotApi()
    monkeypatch.setattr(alerting.urllib.request, "urlopen", api)
    alerting.telegram_send(TOKEN, "42", "srv <disk> at 95%", "Free: 10 GB & falling", "crit",
                           click="http://192.168.0.107:8000/alerts.html")
    alerting.telegram_send(TOKEN, "42", "srv is back online", "", "resolved")
    (url, alert), (_, resolved) = api.calls
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert alert["chat_id"] == "42" and alert["parse_mode"] == "HTML"
    assert alert["text"] == ("🚨 <b>srv &lt;disk&gt; at 95%</b>\nFree: 10 GB &amp; falling\n"
                             '<a href="http://192.168.0.107:8000/alerts.html">Open the dashboard</a>')
    assert alert["disable_notification"] is False
    assert resolved["text"] == "✅ <b>srv is back online</b>"
    assert resolved["disable_notification"] is True


def test_telegram_rejects_bad_tokens_and_chats_before_any_request(monkeypatch):
    import pytest
    api = FakeBotApi()
    monkeypatch.setattr(alerting.urllib.request, "urlopen", api)
    with pytest.raises(ValueError):
        alerting.telegram_send("not-a-token", "42", "t", "m", "crit")
    with pytest.raises(ValueError):
        alerting.telegram_send(TOKEN, "my chat", "t", "m", "crit")
    assert api.calls == []


def test_telegram_errors_explain_the_fix_and_never_show_the_token(monkeypatch):
    import pytest
    for status, description, expected in [
        (401, "Unauthorized", "rejected the bot token"),
        (400, "Bad Request: chat not found", "Find my chat"),
        (403, "Forbidden: bot was blocked by the user", "blocked"),
    ]:
        monkeypatch.setattr(alerting.urllib.request, "urlopen", FakeBotApi(status=status, description=description))
        with pytest.raises(RuntimeError) as err:
            alerting.telegram_send(TOKEN, "42", "t", "m", "crit")
        assert expected in str(err.value)
        assert TOKEN not in str(err.value) and err.value.__cause__ is None


def test_a_short_rate_limit_is_waited_out_once(monkeypatch):
    import pytest
    waits = []
    monkeypatch.setattr(alerting.time, "sleep", waits.append)
    api = FakeBotApi(status=[429, 200], description="Too Many Requests: retry after 3", retry_after=3)
    monkeypatch.setattr(alerting.urllib.request, "urlopen", api)
    alerting.telegram_send(TOKEN, "42", "t", "m", "crit")
    assert waits == [3] and len(api.calls) == 2
    # Still limited after the wait, or told to wait too long: give up.
    for statuses, retry_after in [([429, 429], 3), ([429], 600)]:
        monkeypatch.setattr(alerting.urllib.request, "urlopen",
                            FakeBotApi(status=statuses, description="Too Many Requests", retry_after=retry_after))
        with pytest.raises(RuntimeError, match="rate limiting"):
            alerting.telegram_send(TOKEN, "42", "t", "m", "crit")


def test_telegram_chats_lists_each_chat_once_newest_first(monkeypatch):
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Haziq"}}},
        {"update_id": 2, "message": {"chat": {"id": -1001, "type": "group", "title": "Homelab"}}},
        {"update_id": 3, "edited_message": {"chat": {"id": 42, "type": "private", "first_name": "Haziq"}}},
        {"update_id": 4, "my_chat_member": {"chat": {"id": -1002, "type": "channel", "title": "Alerts"}}},
    ]
    monkeypatch.setattr(alerting.urllib.request, "urlopen", FakeBotApi(result=updates))
    chats = alerting.telegram_chats(TOKEN)
    assert [(c["id"], c["name"], c["type"]) for c in chats] == [
        ("-1002", "Alerts", "channel"), ("42", "Haziq", "private"), ("-1001", "Homelab", "group")]


def test_settings_from_before_telegram_keep_their_ntfy_status(tmp_path):
    import json
    import sqlite3
    path = str(tmp_path / "history.db")
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO settings VALUES ('notifications', ?)", (json.dumps({
            "ntfy": {"url": "http://nas:7070/homelab", "token": "", "enabled": True},
            "status": {"ok": False, "at": 5, "error": "refused"}}),))
    conn.close()
    settings = alerting.read_settings(path)
    assert settings["status"] == {"ntfy": {"ok": False, "at": 5, "error": "refused"}}
    assert settings["telegram"] == {"token": "", "chat_id": "", "enabled": False}
    assert alerting.channel_ready(settings, "ntfy") and not alerting.channel_ready(settings, "telegram")
