"""Server-side alerts.

Rules run against the latest reports and probe results, fired alerts are
kept in an alert log in SQLite, and every change is sent to ntfy. The
browser still highlights cards and pops toasts on its own, instantly; these
alerts wait until a condition has held for a while, because they reach your
phone.
"""

import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

def ram_percent(device: dict) -> Optional[float]:
    """Memory in use, as the RAM meter shows it (the agent counts reclaimable
    ZFS cache as free)."""
    mem = device.get("memory") or {}
    total, used = mem.get("total"), mem.get("used")
    if not isinstance(total, (int, float)) or not total or not isinstance(used, (int, float)):
        return None
    return used / total * 100


# Same trip and clear points as THRESHOLDS in frontend/js/config.js, which
# colours the meters. Each must hold for HELD seconds before it fires, so a
# short spike never reaches your phone.
METRIC_RULES = [
    # kind, label, unit, reading, trip, clear, held (s), severity
    ("cpu", "CPU", "%", lambda d: d.get("cpu_usage"), 85, 75, 300, "warn"),
    ("mem", "RAM", "%", ram_percent, 90, 80, 300, "warn"),
    ("temp", "CPU temperature", "°C", lambda d: d.get("temp"), 80, 72, 300, "crit"),
    ("gputemp", "GPU temperature", "°C", lambda d: (d.get("gpu") or {}).get("temp"), 80, 72, 300, "crit"),
]
DISK_DEFAULT_TRIP = 90   # THRESHOLDS.disk in frontend/js/config.js; per-disk overrides come from settings
DISK_CLEAR_GAP = 5       # a disk alert clears this many points below its threshold
OFFLINE_AFTER = 180      # seconds without a report before a node counts as offline
FRESH_WITHIN = 60        # a node's figures are only checked if it reported this recently
SERVICE_FAILURES = 2     # consecutive failed probes (30 s apart) before a service counts as down
CONTAINER_HELD = 120     # seconds a container must stay stopped, restarting or unhealthy before it alerts
CONTAINER_STABLE = 60    # seconds a restarted container must stay up before it counts as fixed

# ntfy priorities run 1 (min) to 5 (max).
PRIORITY = {"crit": 4, "warn": 3, "resolved": 2, "digest": 2}
TAGS = {"crit": ["rotating_light"], "warn": ["warning"], "resolved": ["white_check_mark"], "digest": ["package"]}

Event = Tuple[str, dict]   # ("fired" | "resolved" | "dropped", alert)


def duration(seconds: float) -> str:
    """"45 s", "12 min", "3 h 20 min", "2 d 4 h"."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(s // 3600, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class AlertEngine:
    """Decides when alerts fire and resolve. Holds no I/O, so it is easy to test."""

    def __init__(self, started_at: float):
        # Reports from before a restart look old; nodes get OFFLINE_AFTER from
        # startup to report in before any of them counts as offline.
        self.started_at = started_at
        # Set by the server from the mutes in mutes.py: nothing new fires for these.
        self.muted_hosts: set = set()
        self.muted_services: set = set()
        self.active: Dict[str, dict] = {}     # key -> alert that fired and has not resolved
        self.pending: Dict[str, float] = {}   # key -> when its condition started holding
        self.failures: Dict[str, int] = {}    # service name -> consecutive failed probes
        self.ran: set = set()                 # container keys seen running since startup

    def restore(self, open_alerts: List[dict]):
        """Alerts still open in the log at startup: no second notification for them."""
        for alert in open_alerts:
            self.active[alert["key"]] = alert

    def is_muted(self, key: str) -> bool:
        """Keys are "service:<name>" or "<kind>:<host>[:...]"."""
        kind, _, rest = key.partition(":")
        if kind == "service":
            return rest in self.muted_services
        return rest.split(":", 1)[0] in self.muted_hosts

    def snapshot(self) -> List[dict]:
        order = {"crit": 0, "warn": 1}
        return sorted(self.active.values(), key=lambda a: (order.get(a["severity"], 2), a["started"]))

    def _hold(self, events: List[Event], now: float, key: str, tripped: bool, cleared: bool,
              held: float, fire: Callable[[], dict], resolve: Callable[[dict], Tuple[str, str]]):
        """Fire `key` once `tripped` has held for `held` seconds; resolve it when `cleared`."""
        if tripped and self.is_muted(key):
            tripped = False   # muted: nothing new fires, and its hold time starts over afterwards
        if key in self.active:
            if cleared:
                alert = self.active.pop(key)
                alert["resolved"] = now
                alert["resolved_title"], alert["resolved_message"] = resolve(alert)
                events.append(("resolved", alert))
            return
        if not tripped:
            self.pending.pop(key, None)
            return
        since = self.pending.setdefault(key, now)
        if now - since >= held:
            del self.pending[key]
            alert = fire()
            alert.update(key=key, started=now)
            self.active[key] = alert
            events.append(("fired", alert))

    def _drop(self, events: List[Event], now: float, keep: Callable[[str], bool]):
        """Close alerts whose subject went away (a node or service removed)."""
        for key in [k for k in self.active if not keep(k)]:
            alert = self.active.pop(key)
            alert["resolved"] = now
            alert["resolved_title"], alert["resolved_message"] = f"{alert['title']}: no longer monitored", ""
            events.append(("dropped", alert))
        for key in [k for k in self.pending if not keep(k)]:
            del self.pending[key]

    def check_nodes(self, now: float, devices: Dict[str, dict], disk_alerts: dict) -> List[Event]:
        events: List[Event] = []
        for host, device in list(devices.items()):
            last_seen = device.get("last_seen", 0)
            silent = now - max(last_seen, self.started_at)
            offline = silent >= OFFLINE_AFTER
            # Back only on a real report: the startup grace above would
            # otherwise "resolve" an offline alert restored from before a restart.
            reporting = now - last_seen < OFFLINE_AFTER
            self._hold(
                events, now, f"offline:{host}", offline, reporting, 0,
                fire=lambda host=host, silent=silent, last_seen=last_seen: {
                    "kind": "offline", "host": host, "severity": "crit",
                    "title": f"{host} is offline",
                    "message": f"No report for {duration(silent)}.",
                    "since": last_seen,
                },
                resolve=lambda alert, host=host: (
                    f"{host} is back online",
                    f"Offline for {duration(now - alert.get('since', alert['started']))}."),
            )
            # Figures from a node that stopped reporting are stale: leave its
            # metric alerts as they are until it reports again.
            if now - last_seen > FRESH_WITHIN:
                continue

            for kind, label, unit, reading, trip, clear, held, severity in METRIC_RULES:
                value = reading(device)
                if not isinstance(value, (int, float)):
                    continue
                self._hold(
                    events, now, f"{kind}:{host}", value > trip, value < clear, held,
                    fire=lambda label=label, unit=unit, value=value, trip=trip, held=held, severity=severity: {
                        "kind": kind, "host": host, "severity": severity,
                        "title": f"{host} {label} at {value:.0f}{unit}",
                        "message": f"Above {trip}{unit} for {duration(held)}.",
                    },
                    resolve=lambda alert, label=label, unit=unit, value=value: (
                        f"{host} {label} back to {value:.0f}{unit}",
                        f"High for {duration(now - alert['started'] + held)}."),
                )

            seen = set()
            for disk in device.get("disks") or []:
                mount, percent = disk.get("mount"), disk.get("percent")
                if not mount or not isinstance(percent, (int, float)):
                    continue
                key = f"disk:{host}:{mount}"
                seen.add(key)
                setting = (disk_alerts.get(host) or {}).get(mount)
                if setting == "off":
                    tripped, cleared, trip = False, True, None
                else:
                    trip = setting if isinstance(setting, (int, float)) else DISK_DEFAULT_TRIP
                    tripped, cleared = percent > trip, percent < trip - DISK_CLEAR_GAP
                self._hold(
                    events, now, key, tripped, cleared, 0,
                    fire=lambda mount=mount, percent=percent, trip=trip, disk=disk: {
                        "kind": "disk", "host": host, "severity": "crit",
                        "title": f"{host} {mount} at {percent:.0f}%",
                        "message": f"Over its {trip}% alert threshold; {size(disk.get('free') or 0)} free.",
                    },
                    resolve=lambda alert, mount=mount, percent=percent, trip=trip: (
                        (f"{host} {mount}: alert switched off", f"At {percent:.0f}%; this disk no longer alerts.")
                        if trip is None else (f"{host} {mount} back to {percent:.0f}%", "")),
                )
            # A disk that is no longer reported takes its alert with it.
            self._drop(events, now, lambda k, host=host, seen=seen: not k.startswith(f"disk:{host}:") or k in seen)

            self._check_containers(events, now, host, device.get("docker_containers") or [])

        hosts = set(devices)
        self._drop(events, now, lambda k: k.startswith(("service:", "checkin:")) or k.split(":")[1] in hosts)
        return events

    def _check_containers(self, events: List[Event], now: float, host: str, containers: List[dict]):
        """A container that was seen running and then stops, keeps restarting
        or fails its health check. Ones already stopped when first seen (old
        or one-off containers) are left alone, as is a container being
        recreated, which is back within CONTAINER_HELD."""
        if not containers:
            return   # usually docker did not answer this time: change nothing
        present = set()
        for c in containers:
            name = c.get("name")
            if not name:
                continue
            key = f"container:{host}:{name}"
            present.add(key)
            state, health, up = (c.get("state") or "").lower(), c.get("health"), c.get("uptime")
            running = state == "running"
            if running:
                self.ran.add(key)
            # Running again, but only briefly: still in trouble if it already was.
            settling = running and isinstance(up, (int, float)) and up < CONTAINER_STABLE
            if state in ("exited", "dead"):
                problem = "stopped"
            elif state == "restarting" or (settling and key in self.pending):
                problem = "restarting"
            elif running and health == "unhealthy":
                problem = "unhealthy"
            else:
                problem = None
            self._hold(
                events, now, key, problem is not None and (key in self.ran or key in self.active),
                running and health != "unhealthy" and not settling, CONTAINER_HELD,
                fire=lambda name=name, problem=problem, c=c: {
                    "kind": "container", "host": host, "problem": problem,
                    "severity": "warn" if problem == "unhealthy" else "crit",
                    "title": {"stopped": f"{host}: {name} stopped",
                              "restarting": f"{host}: {name} keeps restarting",
                              "unhealthy": f"{host}: {name} is unhealthy"}[problem],
                    "message": {"stopped": f"Docker says: {c.get('status') or c.get('state')}.",
                                "restarting": f"It has not stayed up for {duration(CONTAINER_HELD)}.",
                                "unhealthy": f"Running, but failing its health check for "
                                             f"{duration(CONTAINER_HELD)}."}[problem],
                },
                resolve=lambda alert, name=name: (
                    f"{host}: {name} is healthy again" if alert.get("problem") == "unhealthy"
                    else f"{host}: {name} is running again",
                    f"Down for {duration(now - alert['started'] + CONTAINER_HELD)}."),
            )
        # A removed container takes its alert with it.
        self._drop(events, now, lambda k: not k.startswith(f"container:{host}:") or k in present)

    def check_services(self, now: float, services: List[dict]) -> List[Event]:
        """Call once per probe round, after every service has been probed."""
        events: List[Event] = []
        names = set()
        for service in services:
            name = service["name"]
            names.add(name)
            online = bool(service.get("online"))
            self.failures[name] = 0 if online else self.failures.get(name, 0) + 1
            down = self.failures[name] >= SERVICE_FAILURES
            self._hold(
                events, now, f"service:{name}", down, online, 0,
                fire=lambda name=name: {
                    "kind": "service", "host": None, "severity": "crit",
                    "title": f"{name} is down",
                    "message": f"No response to {self.failures[name]} probes in a row from the dashboard server.",
                },
                resolve=lambda alert, name=name: (
                    f"{name} is back up", f"Down for {duration(now - alert['started'])}."),
            )
        self._drop(events, now, lambda k: not k.startswith("service:") or k.split(":", 1)[1] in names)
        return events


    def check_checkins(self, now: float, checks: List[dict]) -> List[Event]:
        """Check-ins (checkins.py) that are late or reported a failure. Each
        check carries its current "state"; a good ping resolves it."""
        events: List[Event] = []
        ids = set()
        for check in checks:
            key = f"checkin:{check['id']}"
            ids.add(key)
            name, state = check["name"], check["state"]
            last = check["last_ping"]
            self._hold(
                events, now, key, state in ("late", "failed"), state == "ok", 0,
                fire=lambda name=name, state=state, last=last, check=check: {
                    "kind": "checkin", "host": None, "severity": "crit",
                    "title": f"{name} failed" if state == "failed" else f"{name} is late",
                    "message": (f"It reported a failure{': ' + check['last_note'] if check['last_note'] else '.'}"
                                if state == "failed" else
                                f"No check-in for {duration(now - last)}; expected every {duration(check['every'])}."
                                if last is not None else
                                f"No check-in yet, {duration(now - check['created'])} after it was added."),
                },
                resolve=lambda alert, name=name: (f"{name} checked in", "It reported success."),
            )
        self._drop(events, now, lambda k: not k.startswith("checkin:") or k in ids)
        return events


def build_digest(devices: Dict[str, dict]) -> Optional[str]:
    """One line per node with OS package or container image updates, or None."""
    lines = []
    for host, device in sorted(devices.items()):
        parts = []
        packages = device.get("pending_updates") or 0
        if packages:
            parts.append(f"{packages} OS package{'s' if packages != 1 else ''}")
        images = [c["name"] for c in device.get("docker_containers") or [] if c.get("update") == "available"]
        if images:
            parts.append(f"{len(images)} image{'s' if len(images) != 1 else ''} ({', '.join(images)})")
        if parts:
            lines.append(f"{host}: {', '.join(parts)}")
    return "\n".join(lines) or None


# --- Storage -----------------------------------------------------------------

def create_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT,
            kind TEXT,
            host TEXT,
            severity TEXT,
            title TEXT,
            message TEXT,
            started REAL,
            resolved REAL,
            resolved_title TEXT,
            resolved_message TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_log_started ON alert_log(started)")


ALERT_FIELDS = ("id", "key", "kind", "host", "severity", "title", "message",
                "started", "resolved", "resolved_title", "resolved_message")


def open_alerts(db_path: str) -> List[dict]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"SELECT {', '.join(ALERT_FIELDS)} FROM alert_log WHERE resolved IS NULL").fetchall()
    conn.close()
    return [dict(zip(ALERT_FIELDS, row)) for row in rows]


def recent_alerts(db_path: str, limit: int = 100) -> List[dict]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        f"SELECT {', '.join(ALERT_FIELDS)} FROM alert_log ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(zip(ALERT_FIELDS, row)) for row in rows]


def log_events(db_path: str, events: List[Event], now: float):
    """Record fired alerts and close resolved ones; prunes entries older than 90 days."""
    conn = sqlite3.connect(db_path)
    with conn:
        for kind, alert in events:
            if kind == "fired":
                cur = conn.execute(
                    "INSERT INTO alert_log (key, kind, host, severity, title, message, started) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (alert["key"], alert["kind"], alert.get("host"), alert["severity"],
                     alert["title"], alert["message"], alert["started"]))
                alert["id"] = cur.lastrowid
            elif alert.get("id") is not None:
                conn.execute(
                    "UPDATE alert_log SET resolved = ?, resolved_title = ?, resolved_message = ? WHERE id = ?",
                    (alert["resolved"], alert["resolved_title"], alert["resolved_message"], alert["id"]))
        conn.execute("DELETE FROM alert_log WHERE started < ?", (now - 90 * 86400,))
    conn.close()


def log_digest(db_path: str, message: str, now: float):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO alert_log (key, kind, host, severity, title, message, started, resolved) "
            "VALUES ('digest', 'digest', NULL, 'info', 'Updates available', ?, ?, ?)", (message, now, now))
    conn.close()


# --- Notification settings and delivery: ntfy and Telegram --------------------

CHANNELS = ("ntfy", "telegram")
TELEGRAM_API = "https://api.telegram.org"   # tests point this at a stand-in

# A bot token goes into the Bot API's URL path, so nothing else is accepted.
TELEGRAM_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
# A numeric chat ID (negative for groups and channels) or a public @channel.
TELEGRAM_CHAT = re.compile(r"^(-?\d+|@[A-Za-z0-9_]{5,})$")

DEFAULT_SETTINGS = {
    "ntfy": {"url": "", "token": "", "enabled": False},
    "telegram": {"token": "", "chat_id": "", "enabled": False},
    "click_url": "",       # opened when a notification is tapped, e.g. http://192.168.0.107:8000/alerts.html
    "digest_hour": 9,      # local hour for the daily update summary; None turns it off
    "last_digest": "",     # date the summary last went out, so a restart does not repeat it
    "status": {},          # channel -> outcome of its last delivery: {"ok", "at", "error"}
}


def read_settings(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = 'notifications'").fetchone()
    conn.close()
    stored = json.loads(row[0]) if row else {}
    settings = {**DEFAULT_SETTINGS, **stored}
    for channel in CHANNELS:
        settings[channel] = {**DEFAULT_SETTINGS[channel], **(stored.get(channel) or {})}
    status = stored.get("status")
    if isinstance(status, dict) and "ok" in status:
        status = {"ntfy": status}   # before Telegram, "status" was ntfy's alone
    settings["status"] = status or {}
    return settings


def write_settings(db_path: str, settings: dict):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('notifications', ?)",
                     (json.dumps(settings),))
    conn.close()


def public_settings(settings: dict) -> dict:
    """Settings as the browser may see them: tokens never leave the server."""
    ntfy, telegram = settings["ntfy"], settings["telegram"]
    return {
        "ntfy": {"url": ntfy["url"], "enabled": ntfy["enabled"], "has_token": bool(ntfy["token"])},
        "telegram": {"chat_id": telegram["chat_id"], "enabled": telegram["enabled"],
                     "has_token": bool(telegram["token"])},
        "click_url": settings["click_url"],
        "digest_hour": settings["digest_hour"],
        "status": settings["status"],
    }


def ntfy_publish(url: str, token: str, title: str, message: str, tone: str, click: str = ""):
    """Send one notification. `url` is the topic URL, e.g. http://192.168.0.105:7070/homelab.

    Published as JSON to the server root rather than with headers, so titles
    are not limited to the characters an HTTP header can carry.
    """
    base, _, topic = url.strip().rstrip("/").rpartition("/")
    if "://" not in base or not topic:
        raise ValueError("the ntfy URL must end with the topic, e.g. http://192.168.0.105:7070/homelab")
    body = {"topic": topic, "title": title, "message": message or title,
            "priority": PRIORITY[tone], "tags": TAGS[tone]}
    if click:
        body["click"] = click
    headers = {"Content-Type": "application/json", "User-Agent": "HomeLab-Dashboard"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        reason = {401: "ntfy needs an access token for this topic",
                  403: "ntfy refused the access token for this topic",
                  429: "ntfy is rate limiting this server"}.get(e.code, f"ntfy answered {e.code}")
        raise RuntimeError(reason) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"ntfy is unreachable ({e.reason})") from e


EMOJI = {"crit": "🚨", "warn": "⚠️", "resolved": "✅", "digest": "📦"}


def _telegram_reason(code: int, description: str) -> str:
    text = description.lower()
    if code in (401, 404):
        return "Telegram rejected the bot token"
    if "chat not found" in text:
        return "Telegram does not know that chat: send your bot a message, then use Find my chat"
    if "blocked by the user" in text:
        return "the bot is blocked in that chat; unblock it in Telegram"
    if code == 409:
        return "the bot has a webhook set, so its messages cannot be read here; enter the chat ID by hand"
    if code == 429:
        return "Telegram is rate limiting this bot"
    return f"Telegram answered {code}: {description}" if description else f"Telegram answered {code}"


def _telegram_call(token: str, method: str, payload: dict, retry: bool = True):
    """Call the Bot API and return its result. Errors never include the
    token, which is part of the URL."""
    if not TELEGRAM_TOKEN.match(token or ""):
        raise ValueError("that does not look like a bot token from @BotFather (digits, a colon, then letters)")
    req = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/{method}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "HomeLab-Dashboard"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            answer = json.loads(e.read())
        except ValueError:
            answer = {}
        wait = (answer.get("parameters") or {}).get("retry_after")
        if e.code == 429 and retry and isinstance(wait, int) and wait <= 30:
            # A burst, such as every service going down at once: wait it out once.
            time.sleep(wait)
            return _telegram_call(token, method, payload, retry=False)
        raise RuntimeError(_telegram_reason(e.code, answer.get("description", ""))) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram is unreachable ({e.reason})") from None
    if not body.get("ok"):
        raise RuntimeError(_telegram_reason(0, body.get("description", "")))
    return body["result"]


def telegram_send(token: str, chat_id: str, title: str, message: str, tone: str, click: str = ""):
    """Send one message: bold title, details, and a link to the dashboard."""
    if not TELEGRAM_CHAT.match(chat_id or ""):
        raise ValueError("the chat ID should be a number such as 123456789 (use Find my chat)")
    lines = [f"{EMOJI[tone]} <b>{html.escape(title)}</b>"]
    if message and message != title:
        lines.append(html.escape(message))
    if click:
        lines.append(f'<a href="{html.escape(click, quote=True)}">Open the dashboard</a>')
    _telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        # Resolutions and the daily summary arrive without a sound, like
        # ntfy's low priority for them.
        "disable_notification": tone in ("resolved", "digest"),
        "link_preview_options": {"is_disabled": True},
    })


def telegram_chats(token: str) -> List[dict]:
    """Chats that have recently messaged the bot, most recent first, so the
    chat ID can be picked rather than looked up. Reading does not mark the
    messages as seen."""
    chats: Dict[int, dict] = {}
    for update in _telegram_call(token, "getUpdates", {"limit": 100, "timeout": 0}):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            name = (chat.get("title")
                    or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
                    or chat.get("username") or str(chat["id"]))
            chats.pop(chat["id"], None)   # re-inserted below, so the latest sorts last
            chats[chat["id"]] = {"id": str(chat["id"]), "name": name, "type": chat.get("type", "")}
    return list(reversed(chats.values()))


def channel_ready(settings: dict, channel: str) -> bool:
    c = settings[channel]
    if not c["enabled"]:
        return False
    return bool(c["url"]) if channel == "ntfy" else bool(c["token"] and c["chat_id"])


def send(settings: dict, channel: str, title: str, message: str, tone: str):
    c = settings[channel]
    if channel == "ntfy":
        ntfy_publish(c["url"], c["token"], title, message, tone, settings["click_url"])
    else:
        telegram_send(c["token"], c["chat_id"], title, message, tone, settings["click_url"])


def notification_for(kind: str, alert: dict) -> Tuple[str, str, str]:
    """(title, message, tone) for a fired or resolved alert."""
    if kind == "fired":
        return alert["title"], alert["message"], alert["severity"]
    return alert["resolved_title"], alert["resolved_message"], "resolved"


def now_status(ok: bool, error: str = "") -> dict:
    return {"ok": ok, "at": time.time(), "error": error}
