"""Backups of the dashboard's own database.

The agent on another node (set up with a backup directory and the backup
key) downloads a snapshot each night, keeps the last few and reports back.
Each report pings a built-in check-in, so a failed or missing backup alerts
like any other late job. The browser can download a snapshot too.

Kept in the settings table under "backup": the backup key, the check-in it
pings and the last report.
"""

import gzip
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from typing import Optional, Tuple

import checkins

CHECKIN_EVERY = 86400   # one backup a night...
CHECKIN_GRACE = 7200    # ...and the alert once one is two hours overdue
STALE_TEMP = 3600       # leftovers of an interrupted download are removed after this


def read(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = 'backup'").fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


def write(db_path: str, data: dict):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup', ?)", (json.dumps(data),))
    conn.close()


def key(db_path: str, rotate: bool = False) -> str:
    """The backup key, made on first use. It only reads snapshots and reports
    on them, so the shared agent key never has to unlock the database."""
    data = read(db_path)
    if rotate or not data.get("key"):
        data["key"] = secrets.token_urlsafe(24)
        write(db_path, data)
    return data["key"]


def key_matches(db_path: str, given: Optional[str]) -> bool:
    stored = read(db_path).get("key")
    return bool(stored and given) and secrets.compare_digest(stored, given)


def snapshot(db_path: str, directory: str) -> Tuple[str, str, int]:
    """A consistent, checked, gzipped copy of the database, made while the
    server keeps writing. Returns (path, sha256, bytes); the caller removes it."""
    os.makedirs(directory, exist_ok=True)
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if time.time() - os.path.getmtime(path) > STALE_TEMP:
            os.remove(path)
    raw = os.path.join(directory, f"snapshot-{time.time_ns()}.db")
    source, copy = sqlite3.connect(db_path), sqlite3.connect(raw)
    try:
        # In steps, so the server's own writes never wait long; one that lands
        # mid-copy makes SQLite start over, so the copy is still consistent.
        source.backup(copy, pages=2048, sleep=0.005)
        if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("the database failed its integrity check")
    finally:
        source.close()
        copy.close()
    packed = raw + ".gz"
    with open(raw, "rb") as f, gzip.open(packed, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g, 1 << 20)
    os.remove(raw)
    digest = hashlib.sha256()
    with open(packed, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return packed, digest.hexdigest(), os.path.getsize(packed)


def record(db_path: str, ok: bool, detail: str, host: str, now: float):
    """Keep a node's report and ping the built-in check-in with it; the
    check-in is made on the first report and again if it was deleted."""
    data = read(db_path)
    check = checkins.get(db_path, data["checkin_id"]) if data.get("checkin_id") else None
    if check is None:
        check = checkins.create(db_path, f"Dashboard backup to {host}", CHECKIN_EVERY, CHECKIN_GRACE, now)
        data["checkin_id"] = check["id"]
    data["last"] = {"ok": ok, "at": now, "detail": detail[:checkins.NOTE_LIMIT], "host": host}
    write(db_path, data)
    checkins.ping(db_path, check["token"], ok, detail, now)


def status(db_path: str) -> dict:
    """What the Alerts page shows: the key and the last report, if any."""
    data = read(db_path)
    return {"key": key(db_path), "last": data.get("last"), "checkin_id": data.get("checkin_id")}
