import asyncio
import json
import os
import platform
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Dict, List
from fastapi import FastAPI, Request, Header, Query, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import alerting

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def read_git_commit() -> str:
    """The checkout's current commit, read from .git without running git
    (the service user may not be the checkout's owner). "" when unknown."""
    git_dir = os.path.join(REPO_ROOT, ".git")
    try:
        with open(os.path.join(git_dir, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head   # detached HEAD
        ref = head[5:]
        ref_path = os.path.join(git_dir, ref)
        if os.path.exists(ref_path):
            with open(ref_path) as f:
                return f.read().strip()
        with open(os.path.join(git_dir, "packed-refs")) as f:
            for line in f:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0]
    except OSError:
        pass
    return ""

# The code this process started with. A `git pull` without a restart moves
# the checkout (and so the pages it serves) ahead of it.
RUNNING_COMMIT = read_git_commit()

app = FastAPI(title="Home Lab Dashboard API")

# Enable CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for device metrics
# Key: hostname, Value: device metrics dict
devices: Dict[str, dict] = {}
device_persist_lock = asyncio.Lock()

# In-memory store for the last 6 hours of samples: lab power plus fleet load
power_history: List[dict] = []

# Fleet load recorded beside the power total. These arrived after power_log
# existed, so older databases gain the columns on startup; their earlier rows
# read as NULL, which the charts draw as a gap.
FLEET_COLUMNS = ("cpu", "mem", "net_down", "net_up")
MAX_POWER_HISTORY = 360  # 6 hours of 1-minute intervals

# Services monitoring list
services_list: List[dict] = []

def init_db():
    """Create the SQLite history and auth tables and indexes if not exists."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Power log table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS power_log (
                timestamp REAL,
                power REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_power_log_timestamp ON power_log(timestamp)")
        # Every service probe (one row per service every 30 s), for uptime.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS service_log (
                name TEXT,
                timestamp REAL,
                online INTEGER,
                latency REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_service_log_time ON service_log(timestamp)")

        # Each online node's own load, one row a minute, for its history charts.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS node_log (
                hostname TEXT,
                timestamp REAL,
                cpu REAL,
                mem REAL,
                net_down REAL,
                net_up REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_node_log_host_time ON node_log(hostname, timestamp)")

        # Server-side alert history (alerting.py).
        alerting.create_tables(cursor)

        existing = {row[1] for row in cursor.execute("PRAGMA table_info(power_log)")}
        for column in FLEET_COLUMNS:
            if column not in existing:
                cursor.execute(f"ALTER TABLE power_log ADD COLUMN {column} REAL")

        # Last report of every node. Without it a restart forgot any node that
        # was offline at the time, and blanked the rest until they reported.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                hostname TEXT PRIMARY KEY,
                data TEXT
            )
        """)
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT,
                salt TEXT,
                created_at REAL
            )
        """)
        
        # Sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT,
                expires_at REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        
        # Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        # Generate agent key if not exists
        cursor.execute("SELECT value FROM settings WHERE key = 'agent_auth_key'")
        row = cursor.fetchone()
        if not row:
            import secrets
            # Generate a 32-character secure random agent key
            new_key = secrets.token_hex(16)
            cursor.execute("INSERT INTO settings (key, value) VALUES ('agent_auth_key', ?)", (new_key,))
            
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error initializing database: {e}")

def get_agent_auth_key() -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'agent_auth_key'")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""

def verify_session_token(token: str) -> str:
    """Verifies a session token. Returns username if valid, otherwise None."""
    if not token:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        current_time = time.time()
        cursor.execute("SELECT username FROM sessions WHERE token = ? AND expires_at > ?", (token, current_time))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def get_current_user(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication credentials"
        )
    token = authorization.split(" ")[1]
    username = verify_session_token(token)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired or is invalid"
        )
    return username

def load_power_history_from_db():
    """Load the last 6 hours of power history from SQLite on startup."""
    global power_history
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        six_hours_ago = time.time() - 21600
        cursor.execute(f"SELECT timestamp, power, {', '.join(FLEET_COLUMNS)} FROM power_log "
                       "WHERE timestamp >= ? ORDER BY timestamp ASC", (six_hours_ago,))
        rows = cursor.fetchall()
        power_history = [{"time": r[0], "power": r[1], **dict(zip(FLEET_COLUMNS, r[2:]))} for r in rows]
        conn.close()
    except Exception as e:
        print(f"Error loading power history: {e}")

def load_devices_from_db():
    """Restore every known node; each shows offline until it reports again."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT hostname, data FROM devices").fetchall()
        conn.close()
        for hostname, data in rows:
            device = json.loads(data)
            device["latency"] = None
            devices[hostname] = device
    except Exception as e:
        print(f"Error loading devices: {e}")

def device_rows() -> List[tuple]:
    """Serialise the registry on the event loop, before a thread writes it."""
    return [(name, json.dumps(info)) for name, info in list(devices.items())]

def save_devices_to_db(rows: List[tuple]):
    """Replace the stored registry, so forgotten nodes drop out of it too."""
    try:
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute("DELETE FROM devices")
            conn.executemany("INSERT INTO devices (hostname, data) VALUES (?, ?)", rows)
        conn.close()
    except Exception as e:
        print(f"Error saving devices: {e}")

async def persist_devices_snapshot():
    """Persist one consistent snapshot; serialize full-table writes."""
    async with device_persist_lock:
        await asyncio.to_thread(save_devices_to_db, device_rows())

def load_services_config():
    """Load services to monitor from services.json."""
    global services_list
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                services_list = json.load(f)
                # Initialize status keys
                for s in services_list:
                    s["online"] = False
                    s["latency"] = 0.0
        except Exception as e:
            print(f"Error reading services.json: {e}")

def get_sanitized_services() -> List[dict]:
    """Return services list with URLs stripped out for security."""
    return [
        {
            "name": s["name"],
            "icon": s["icon"],
            "category": s.get("category", "General"),
            "online": s.get("online", False),
            "latency": s.get("latency", 0.0)
        }
        for s in services_list
    ]

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect already proves the service is up. Following it only adds a
    second request, often for a full app shell, to the measured latency."""
    def redirect_request(self, *args, **kwargs):
        return None

_service_opener = urllib.request.build_opener(_NoRedirect)

# Firing every check at once piled them onto the same host and queued them
# behind the default thread pool, which reported 5 ms services as ~1 s.
_service_check_slots = asyncio.Semaphore(4)

async def ping_service(service: dict):
    """Check if an HTTP service is online internally (APU backend side) and record response time."""
    url = service.get("url")
    if not url:
        return

    def run() -> float:
        # Timed inside the worker so waiting for a free thread is not counted.
        start = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": "HomeLab-Dashboard-Monitor"})
        try:
            with _service_opener.open(req, timeout=2.0) as response:
                response.read(1)
        except urllib.error.HTTPError:
            pass  # Any HTTP status, including 3xx and 401, means it answered.
        return (time.time() - start) * 1000.0

    try:
        async with _service_check_slots:
            latency = await asyncio.to_thread(run)
        service["online"] = True
        service["latency"] = round(latency, 1)
    except Exception:
        service["online"] = False
        service["latency"] = 0.0

# Active SSE listener queues
listeners: List[asyncio.Queue] = []

# Offline threshold in seconds
OFFLINE_THRESHOLD = 15

def get_device_status(device_info: dict, current_time: float) -> dict:
    """Helper to enrich device info with live online/offline status."""
    info = device_info.copy()
    last_seen = info.get("last_seen", 0)
    info["online"] = (current_time - last_seen) < OFFLINE_THRESHOLD
    # Seconds since the last report, by this server's clock. Browsers anchor it
    # to their own clock rather than comparing last_seen across two clocks.
    info["age"] = max(0.0, current_time - last_seen)
    return info

async def broadcast(event_type: str, data: dict):
    """Broadcast an event to all connected SSE clients."""
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    for queue in list(listeners):
        try:
            await queue.put(payload)
        except Exception:
            pass

async def ping_host(ip: str) -> float:
    """Ping a host and return round-trip latency in ms, or None if unreachable."""
    try:
        is_win = platform.system() == "Windows"
        # 1 packet, 1 second timeout (Linux -W, Windows -w 1000)
        cmd = ["ping", "-n", "1", "-w", "1000", ip] if is_win else ["ping", "-c", "1", "-W", "1", ip]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        
        if proc.returncode == 0:
            output = stdout.decode(errors='ignore')
            # Look for "time=Xms" or "time=X.Y ms"
            match = re.search(r'time[=<]([\d\.]+)\s*ms', output, re.IGNORECASE)
            if match:
                return float(match.group(1))
            match = re.search(r'time=([\d\.]+)', output, re.IGNORECASE)
            if match:
                return float(match.group(1))
    except Exception:
        pass
    return None

async def ping_background_loop():
    """Background task to periodically ping registered nodes and update latency."""
    while True:
        try:
            for hostname in list(devices.keys()):
                device = devices.get(hostname)
                if not device:
                    continue
                # Check if device is active before pinging
                is_active = (time.time() - device.get("last_seen", 0)) < OFFLINE_THRESHOLD
                latency = None
                if is_active and device.get("ip"):
                    latency = await ping_host(device["ip"])

                # A report may have replaced this device's dict while the ping
                # was awaited. Updating the stale copy lost the latency and
                # broadcast an older snapshot with last_seen moving backwards.
                current = devices.get(hostname)
                if current is None:
                    continue
                current["latency"] = latency

                # Broadcast latency update
                await broadcast("metrics", get_device_status(current, time.time()))
        except Exception as e:
            print(f"Error in ping loop: {e}")
        # Sleep for 10 seconds before next ping sweep
        await asyncio.sleep(10.0)

def fleet_load(active: List[dict]) -> dict:
    """Average CPU, share of memory in use and total network throughput.

    VMs count here: unlike power, their load is their own. frontend/js/devices.js
    computes the live headline values the same way.
    """
    cpus = [d["cpu_usage"] for d in active if isinstance(d.get("cpu_usage"), (int, float))]
    mems = [d["memory"] for d in active if isinstance(d.get("memory"), dict) and d["memory"].get("total")]
    nets = [d["network"] for d in active if isinstance(d.get("network"), dict)]
    return {
        "cpu": sum(cpus) / len(cpus) if cpus else None,
        # Used over total rather than a mean of percentages, so a small box at
        # 90% does not outweigh a 64 GB one at 10%.
        "mem": 100.0 * sum(m.get("used") or 0 for m in mems) / sum(m["total"] for m in mems) if mems else None,
        "net_down": sum(n.get("down_speed") or 0 for n in nets) if nets else None,
        "net_up": sum(n.get("up_speed") or 0 for n in nets) if nets else None,
    }

async def record_power_history_loop():
    """Background task to periodically record lab power and fleet load."""
    while True:
        # Sleep first: sampling at startup, before any agent had reported,
        # wrote a 0 W point into the history on every restart.
        await asyncio.sleep(60.0)
        try:
            current_time = time.time()
            total_power = 0.0
            active = [d for d in list(devices.values())
                      if (current_time - d.get("last_seen", 0)) < OFFLINE_THRESHOLD]
            for device in active:
                # A VM's draw is either already in its host's reading or, for
                # a cloud instance, not on this lab's bill at all.
                if not device.get("is_vm"):
                    cpu_p = device.get("cpu_power")
                    gpu_info = device.get("gpu")
                    gpu_p = gpu_info.get("power") if gpu_info else None
                    
                    device_power = 0.0
                    if isinstance(cpu_p, (int, float)):
                        device_power += cpu_p
                    if isinstance(gpu_p, (int, float)):
                        device_power += gpu_p
                    total_power += device_power
            
            load = fleet_load(active)
            # A fleet of one gives a node's own figures.
            node_rows = [(d.get("hostname"), current_time, *(fleet_load([d])[c] for c in FLEET_COLUMNS))
                         for d in active]
            power_history.append({
                "time": current_time,
                "power": total_power,
                **load
            })
            
            if len(power_history) > MAX_POWER_HISTORY:
                power_history.pop(0)
                
            # Persistent DB insert & prune (Non-blocking background thread)
            def db_write():
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute(
                        f"INSERT INTO power_log (timestamp, power, {', '.join(FLEET_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?)",
                        (current_time, total_power, *(load[c] for c in FLEET_COLUMNS)))
                    
                    cursor.executemany(
                        f"INSERT INTO node_log (hostname, timestamp, {', '.join(FLEET_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?)",
                        node_rows)

                    # Delete values older than 30 days (30 * 24 * 3600 seconds)
                    limit_time = current_time - (30 * 24 * 3600)
                    cursor.execute("DELETE FROM power_log WHERE timestamp < ?", (limit_time,))
                    cursor.execute("DELETE FROM node_log WHERE timestamp < ?", (limit_time,))
                    conn.commit()
                    conn.close()
                except Exception as db_err:
                    print(f"Error writing power to db: {db_err}")
            
            await asyncio.to_thread(db_write)
            await persist_devices_snapshot()
        except Exception as e:
            print(f"Error in power history loop: {e}")

def record_service_checks(rows: List[tuple], prune_before: float = None):
    """Store one round of probes, and drop rounds older than 30 days when asked."""
    try:
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.executemany("INSERT INTO service_log (name, timestamp, online, latency) VALUES (?, ?, ?, ?)", rows)
            if prune_before is not None:
                conn.execute("DELETE FROM service_log WHERE timestamp < ?", (prune_before,))
        conn.close()
    except Exception as e:
        print(f"Error writing service checks: {e}")

async def check_services_loop():
    """Background task to query services status every 30 seconds."""
    last_prune = 0.0
    while True:
        try:
            tasks = [ping_service(s) for s in services_list]
            if tasks:
                await asyncio.gather(*tasks)
                now = time.time()
                rows = [(s["name"], now, int(s.get("online", False)), s.get("latency") or None)
                        for s in services_list]
                # Pruning scans the table, so once an hour is plenty.
                prune = now - 30 * 86400 if now - last_prune > 3600 else None
                if prune is not None:
                    last_prune = now
                await asyncio.to_thread(record_service_checks, rows, prune)
                await handle_alert_events(alert_engine.check_services(now, services_list))
            
            sanitized = get_sanitized_services()
            await broadcast("services", sanitized)
        except Exception as e:
            print(f"Error in services loop: {e}")
        await asyncio.sleep(30.0)

# --- Server-side alerts (rules in alerting.py) -------------------------------

alert_engine = alerting.AlertEngine(started_at=time.time())
_notification_settings_lock = asyncio.Lock()

async def update_notification_settings(change) -> dict:
    """Read-modify-write the stored settings; two writers must not lose a change."""
    async with _notification_settings_lock:
        def apply():
            settings = alerting.read_settings(DB_PATH)
            change(settings)
            alerting.write_settings(DB_PATH, settings)
            return settings
        return await asyncio.to_thread(apply)

async def deliver(title: str, message: str, tone: str):
    """Send to every enabled channel; one failing never stops the others.
    Each channel's outcome is kept for the Alerts page."""
    settings = await asyncio.to_thread(alerting.read_settings, DB_PATH)
    outcomes = {}
    for channel in alerting.CHANNELS:
        if not alerting.channel_ready(settings, channel):
            continue
        try:
            await asyncio.to_thread(alerting.send, settings, channel, title, message, tone)
            outcomes[channel] = alerting.now_status(True)
        except Exception as e:
            print(f"{channel} notification failed: {e}")
            outcomes[channel] = alerting.now_status(False, str(e))
    if outcomes:
        await update_notification_settings(lambda s: s["status"].update(outcomes))

async def handle_alert_events(events: List[tuple]):
    if not events:
        return
    await asyncio.to_thread(alerting.log_events, DB_PATH, events, time.time())
    for kind, alert in events:
        if kind != "dropped":   # a removed node or service is not news
            await deliver(*alerting.notification_for(kind, alert))
    await broadcast("alerts", alert_engine.snapshot())

async def maybe_send_digest():
    """Once a day, after digest_hour (server local time): pending OS and image updates."""
    settings = await asyncio.to_thread(alerting.read_settings, DB_PATH)
    hour, today = settings["digest_hour"], time.strftime("%Y-%m-%d")
    if hour is None or time.localtime().tm_hour < hour or settings["last_digest"] == today:
        return
    # Marked first, so a failing ntfy does not mean a retry every 30 seconds.
    await update_notification_settings(lambda s: s.update(last_digest=today))
    message = alerting.build_digest(devices)
    if message:
        await asyncio.to_thread(alerting.log_digest, DB_PATH, message, time.time())
        await deliver("Updates available", message, "digest")

async def alerts_loop():
    """Node rules every 30 s; services are checked after each probe round instead."""
    while True:
        await asyncio.sleep(30.0)
        try:
            disk_settings = await asyncio.to_thread(read_disk_alerts)
            await handle_alert_events(alert_engine.check_nodes(time.time(), devices, disk_settings))
            await maybe_send_digest()
        except Exception as e:
            print(f"Error in alerts loop: {e}")

@app.on_event("startup")
async def startup_event():
    init_db()
    # Alerts still open from before a restart: tracked again, not re-sent.
    alert_engine.restore(alerting.open_alerts(DB_PATH))
    load_power_history_from_db()
    load_devices_from_db()
    load_services_config()
    # Start the non-blocking background ping sweep
    asyncio.create_task(ping_background_loop())
    # Start the power history loop
    asyncio.create_task(record_power_history_loop())
    # Start the services loop
    asyncio.create_task(check_services_loop())
    asyncio.create_task(alerts_loop())

@app.on_event("shutdown")
async def shutdown_event():
    # The loop only saves once a minute; keep the latest reports on a restart.
    await persist_devices_snapshot()

# Authentication & User Management Endpoints

@app.get("/api/version")
async def get_version():
    """Commits running and installed; "stale" means the server needs a restart."""
    installed = read_git_commit()
    return {
        "running": RUNNING_COMMIT[:7],
        "installed": installed[:7],
        "stale": bool(RUNNING_COMMIT and installed and RUNNING_COMMIT != installed),
    }

@app.get("/api/auth/status")
async def auth_status():
    """Check if any user exists in the system."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]
        conn.close()
        return {"users_exist": count > 0}
    except Exception as e:
        return {"users_exist": False, "error": str(e)}

@app.post("/api/auth/register")
async def register(payload: dict):
    username = payload.get("username")
    password = payload.get("password")
    
    if not username or not password:
        raise HTTPException(status_code=400, detail="Missing username or password")
        
    username = username.strip()
    if len(username) < 3 or len(password) < 6:
        raise HTTPException(status_code=400, detail="Username must be >= 3 chars, password >= 6 chars")
        
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        user_count = cursor.fetchone()[0]
        if user_count > 0:
            conn.close()
            raise HTTPException(status_code=403, detail="Registration is disabled. Admin user already exists.")
            
        import hashlib
        import secrets
        
        # Hash password using PBKDF2-SHA256
        salt = secrets.token_bytes(16)
        pw_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000).hex()
        salt_hex = salt.hex()
        
        cursor.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, pw_hash, salt_hex, time.time())
        )
        conn.commit()
        conn.close()
        
        return await login(payload)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.post("/api/auth/login")
async def login(payload: dict):
    username = payload.get("username")
    password = payload.get("password")
    
    if not username or not password:
        raise HTTPException(status_code=400, detail="Missing username or password")
        
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash, salt FROM users WHERE username = ?", (username.strip(),))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            raise HTTPException(status_code=401, detail="Invalid username or password")
            
        stored_hash, salt_hex = row
        salt = bytes.fromhex(salt_hex)
        import hashlib
        import secrets
        
        # Verify password
        pw_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000).hex()
        if pw_hash != stored_hash:
            conn.close()
            raise HTTPException(status_code=401, detail="Invalid username or password")
            
        # Create session token
        token = secrets.token_hex(32)
        # 14 days expiration
        expires_at = time.time() + (14 * 24 * 3600)
        cursor.execute("INSERT INTO sessions (token, username, expires_at) VALUES (?, ?, ?)", (token, username, expires_at))
        conn.commit()
        conn.close()
        
        agent_key = get_agent_auth_key()
        
        return {
            "token": token,
            "username": username,
            "expires_at": expires_at,
            "agent_auth_key": agent_key
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.post("/api/auth/logout")
async def logout(payload: dict):
    token = payload.get("token")
    if token:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return {"status": "ok"}

# Per-disk alert thresholds, kept in the settings table as
# {hostname: {mount: percent | "off"}}. A disk without an entry alerts at the
# default (THRESHOLDS.disk in frontend/js/config.js).
_disk_alerts_lock = asyncio.Lock()

def read_disk_alerts() -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM settings WHERE key = 'disk_alerts'").fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}

@app.get("/api/settings/disk-alerts")
async def get_disk_alerts(current_user: str = Depends(get_current_user)):
    return await asyncio.to_thread(read_disk_alerts)

@app.put("/api/settings/disk-alerts")
async def set_disk_alert(payload: dict, current_user: str = Depends(get_current_user)):
    """Set one disk's threshold: a percent from 50 to 99, "off", or null for the default."""
    hostname, mount, trip = payload.get("hostname"), payload.get("mount"), payload.get("trip")
    if not isinstance(hostname, str) or not hostname or not isinstance(mount, str) or not mount:
        raise HTTPException(status_code=400, detail="hostname and mount are required")
    valid_percent = isinstance(trip, (int, float)) and not isinstance(trip, bool) and 50 <= trip <= 99
    if trip is not None and trip != "off" and not valid_percent:
        raise HTTPException(status_code=400, detail='trip must be 50-99, "off" or null')

    def write():
        alerts = read_disk_alerts()
        host = alerts.setdefault(hostname, {})
        if trip is None:
            host.pop(mount, None)
        else:
            host[mount] = trip
        if not host:
            alerts.pop(hostname)
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('disk_alerts', ?)",
                         (json.dumps(alerts),))
        conn.close()
        return alerts

    # Read-modify-write: two changes at once must not drop one of them.
    async with _disk_alerts_lock:
        alerts = await asyncio.to_thread(write)
    await broadcast("disk_alerts", alerts)
    return alerts

@app.get("/api/alerts")
async def get_alerts(current_user: str = Depends(get_current_user)):
    history = await asyncio.to_thread(alerting.recent_alerts, DB_PATH)
    return {"active": alert_engine.snapshot(), "history": history}

@app.get("/api/settings/notifications")
async def get_notification_settings(current_user: str = Depends(get_current_user)):
    return alerting.public_settings(await asyncio.to_thread(alerting.read_settings, DB_PATH))

def _is_http_url(value) -> bool:
    return isinstance(value, str) and (value == "" or value.startswith(("http://", "https://")))

@app.put("/api/settings/notifications")
async def set_notification_settings(payload: dict, current_user: str = Depends(get_current_user)):
    """Update the fields present. A token of null (or absent) keeps the stored one; "" clears it."""
    ntfy = payload.get("ntfy") or {}
    telegram = payload.get("telegram") or {}
    if "url" in ntfy and not _is_http_url(ntfy["url"]):
        raise HTTPException(status_code=400, detail="The ntfy URL must start with http:// or https://")
    if ntfy.get("token") is not None and not isinstance(ntfy["token"], str):
        raise HTTPException(status_code=400, detail="The ntfy token must be text")
    for channel in (ntfy, telegram):
        if "enabled" in channel and not isinstance(channel["enabled"], bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
    tg_token = telegram.get("token")
    if tg_token is not None and not (isinstance(tg_token, str)
                                     and (tg_token.strip() == "" or alerting.TELEGRAM_TOKEN.match(tg_token.strip()))):
        raise HTTPException(status_code=400, detail="That does not look like a bot token from @BotFather")
    if "chat_id" in telegram and not (isinstance(telegram["chat_id"], str)
                                      and (telegram["chat_id"].strip() == ""
                                           or alerting.TELEGRAM_CHAT.match(telegram["chat_id"].strip()))):
        raise HTTPException(status_code=400, detail="The chat ID should be a number such as 123456789")
    if "click_url" in payload and not _is_http_url(payload["click_url"]):
        raise HTTPException(status_code=400, detail="The dashboard link must start with http:// or https://")
    hour = payload.get("digest_hour", "unset")
    if hour != "unset" and hour is not None and not (isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23):
        raise HTTPException(status_code=400, detail="digest_hour must be 0-23 or null")

    def change(settings):
        for field in ("url", "enabled"):
            if field in ntfy:
                settings["ntfy"][field] = ntfy[field].strip() if field == "url" else ntfy[field]
        if ntfy.get("token") is not None:
            settings["ntfy"]["token"] = ntfy["token"].strip()
        if "enabled" in telegram:
            settings["telegram"]["enabled"] = telegram["enabled"]
        if "chat_id" in telegram:
            settings["telegram"]["chat_id"] = telegram["chat_id"].strip()
        if tg_token is not None:
            settings["telegram"]["token"] = tg_token.strip()
        if "click_url" in payload:
            settings["click_url"] = payload["click_url"].strip()
        if hour != "unset":
            settings["digest_hour"] = hour

    return alerting.public_settings(await update_notification_settings(change))

def _typed_or_saved(payload: dict, field: str, saved: str) -> str:
    value = payload.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else saved

@app.post("/api/notifications/test")
async def send_test_notification(payload: dict, current_user: str = Depends(get_current_user)):
    """Send a test to one channel ("ntfy" unless "channel" says "telegram"). Values
    typed into the form are used, unsaved; anything left blank falls back to the
    saved settings, the token included."""
    settings = await asyncio.to_thread(alerting.read_settings, DB_PATH)
    channel = payload.get("channel", "ntfy")
    title, message = "Test from HomeLab Monitor", "Alerts from your dashboard will arrive here."
    try:
        if channel == "telegram":
            saved = settings["telegram"]
            token = _typed_or_saved(payload, "token", saved["token"])
            chat_id = _typed_or_saved(payload, "chat_id", saved["chat_id"])
            if not token or not chat_id:
                raise HTTPException(status_code=400, detail="Enter the bot token and pick a chat first")
            await asyncio.to_thread(alerting.telegram_send, token, chat_id, title, message, "resolved",
                                    settings["click_url"])
        elif channel == "ntfy":
            url = _typed_or_saved(payload, "url", settings["ntfy"]["url"])
            if not url:
                raise HTTPException(status_code=400, detail="Set the ntfy topic URL first")
            token = _typed_or_saved(payload, "token", settings["ntfy"]["token"])
            await asyncio.to_thread(alerting.ntfy_publish, url, token, title, message, "resolved",
                                    settings["click_url"])
        else:
            raise HTTPException(status_code=400, detail='channel must be "ntfy" or "telegram"')
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}

@app.post("/api/notifications/telegram/chats")
async def find_telegram_chats(payload: dict, current_user: str = Depends(get_current_user)):
    """Chats that recently messaged the bot (typed token, or the saved one)."""
    settings = await asyncio.to_thread(alerting.read_settings, DB_PATH)
    token = _typed_or_saved(payload, "token", settings["telegram"]["token"])
    if not token:
        raise HTTPException(status_code=400, detail="Enter the bot token first")
    try:
        return {"chats": await asyncio.to_thread(alerting.telegram_chats, token)}
    except Exception as e:
        return {"chats": [], "error": str(e)}

@app.get("/api/auth/agent-key")
async def get_agent_key_endpoint(current_user: str = Depends(get_current_user)):
    return {"agent_auth_key": get_agent_auth_key()}


# Secured Dashboard Metrics Endpoints

@app.post("/api/report")
async def report_metrics(data: dict, x_agent_key: str = Header(None)):
    stored_key = get_agent_auth_key()
    if not x_agent_key or x_agent_key != stored_key:
        raise HTTPException(status_code=401, detail="Unauthorized agent key")

    hostname = data.get("hostname")
    if not hostname:
        return {"status": "error", "message": "Missing hostname"}
    
    current_time = time.time()
    # Preserving latency from background ping loop if already present
    if hostname in devices and "latency" in devices[hostname]:
        data["latency"] = devices[hostname]["latency"]
    else:
        data["latency"] = None
        
    data["last_seen"] = current_time
    devices[hostname] = data
    
    enriched = get_device_status(data, current_time)
    await broadcast("metrics", enriched)
    return {"status": "ok"}

@app.get("/api/devices")
async def get_devices(current_user: str = Depends(get_current_user)):
    current_time = time.time()
    return {
        name: get_device_status(info, current_time)
        for name, info in devices.items()
    }

@app.delete("/api/devices/{hostname}")
async def forget_device(hostname: str, current_user: str = Depends(get_current_user)):
    """Drop a retired or renamed node. One still running an agent comes back."""
    if devices.pop(hostname, None) is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    await persist_devices_snapshot()

    def drop_history():
        conn = sqlite3.connect(DB_PATH)
        with conn:
            conn.execute("DELETE FROM node_log WHERE hostname = ?", (hostname,))
        conn.close()
    await asyncio.to_thread(drop_history)
    await broadcast("removed", {"hostname": hostname})
    return {"status": "ok"}

# Window and bucket size in seconds for each history range.
HISTORY_RANGES = {"6h": (21600, 60), "24h": (86400, 300), "7d": (604800, 1800), "30d": (2592000, 7200)}

@app.get("/api/devices/{hostname}/history")
async def get_device_history(hostname: str, range: str = "6h", current_user: str = Depends(get_current_user)):
    """One node's CPU, memory and network over a range, bucketed like /api/power-history."""
    span, interval = HISTORY_RANGES.get(range, HISTORY_RANGES["6h"])

    def fetch_data():
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(f"""
            SELECT CAST(timestamp / ? AS INTEGER) * ? AS grp, {', '.join(f'AVG({c})' for c in FLEET_COLUMNS)}
            FROM node_log
            WHERE hostname = ? AND timestamp >= ?
            GROUP BY grp
            ORDER BY grp ASC
        """, (interval, interval, hostname, time.time() - span)).fetchall()
        conn.close()
        return [
            {"time": r[0], **{c: (round(v, 1) if v is not None else None) for c, v in zip(FLEET_COLUMNS, r[1:])}}
            for r in rows
        ]

    return await asyncio.to_thread(fetch_data)

@app.get("/api/power-history")
async def get_power_history(range: str = "6h", current_user: str = Depends(get_current_user)):
    current_time = time.time()
    
    # 6h range defaults to the in-memory array for speed
    if range == "6h":
        return power_history
        
    def fetch_data():
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            span, interval = HISTORY_RANGES.get(range, HISTORY_RANGES["6h"])
            start_time = current_time - span

            cursor.execute(f"""
                SELECT CAST(timestamp / ? AS INTEGER) * ? as grp, AVG(power),
                       {', '.join(f'AVG({c})' for c in FLEET_COLUMNS)}
                FROM power_log
                WHERE timestamp >= ?
                GROUP BY grp
                ORDER BY grp ASC
            """, (interval, interval, start_time))
            rows = cursor.fetchall()
            conn.close()
            
            return [
                {
                    "time": r[0],
                    "power": round(r[1], 1) if r[1] is not None else 0.0,
                    **{c: (round(v, 1) if v is not None else None) for c, v in zip(FLEET_COLUMNS, r[2:])},
                }
                for r in rows
            ]
        except Exception as query_err:
            print(f"Error querying power history database: {query_err}")
            return []

    return await asyncio.to_thread(fetch_data)

# Window per uptime range; each is split into this many slots for the bar.
SERVICE_RANGES = {"24h": 86400, "7d": 604800, "30d": 2592000}
SERVICE_SLOTS = 48

@app.get("/api/services/history")
async def get_services_history(range: str = "24h", current_user: str = Depends(get_current_user)):
    """Uptime per service over a range: an overall percentage plus one entry per
    slot (share of probes answered and mean latency of the answered ones).
    A slot with no probes is null, e.g. while the dashboard server was down."""
    span = SERVICE_RANGES.get(range, SERVICE_RANGES["24h"])
    slot = span / SERVICE_SLOTS
    start = time.time() - span

    def fetch_data():
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT name, MIN(CAST((timestamp - ?) / ? AS INTEGER), ?) AS i,
                   SUM(online), COUNT(*), AVG(CASE WHEN online THEN latency END)
            FROM service_log
            WHERE timestamp >= ?
            GROUP BY name, i
        """, (start, slot, SERVICE_SLOTS - 1, start)).fetchall()
        conn.close()

        out = {}
        for name, i, up, total, latency in rows:
            entry = out.setdefault(name, {"slots": [None] * SERVICE_SLOTS, "up": 0, "total": 0})
            entry["slots"][i] = {
                "start": start + i * slot,
                "up": up / total,
                "latency": round(latency, 1) if latency is not None else None,
            }
            entry["up"] += up
            entry["total"] += total
        return {
            name: {"uptime": round(100.0 * e["up"] / e["total"], 2), "slots": e["slots"], "slot_seconds": slot}
            for name, e in out.items()
        }

    return await asyncio.to_thread(fetch_data)

@app.get("/api/services")
async def get_services_endpoint(current_user: str = Depends(get_current_user)):
    return get_sanitized_services()

@app.get("/api/stream")
async def message_stream(request: Request, token: str = Query(None)):
    username = verify_session_token(token)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized stream connection"
        )
        
    queue = asyncio.Queue()
    listeners.append(queue)
    
    current_time = time.time()
    initial_state = [
        get_device_status(info, current_time)
        for info in devices.values()
    ]
    initial_services = get_sanitized_services()
    
    async def event_generator():
        try:
            yield f"event: init\ndata: {json.dumps(initial_state)}\n\n"
            yield f"event: services_init\ndata: {json.dumps(initial_services)}\n\n"
            yield f"event: alerts\ndata: {json.dumps(alert_engine.snapshot())}\n\n"
            
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield data
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in listeners:
                listeners.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Mount frontend static files
backend_dir = os.path.dirname(os.path.abspath(__file__))
frontend_dir = os.path.abspath(os.path.join(backend_dir, "../frontend"))

if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="static")
else:
    print(f"Warning: Static files directory '{frontend_dir}' not found.")
