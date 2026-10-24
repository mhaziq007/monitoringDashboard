"""Check-ins: backups and scheduled jobs that report in.

Each check-in has a secret ping URL. A job calls it when it finishes (or
its /fail form when it fails), and the dashboard alerts when a ping is late
or reports a failure, so a job that silently stopped running is noticed.
Stored in the `checkins` table; the alert rule is AlertEngine.check_checkins.
"""

import secrets
import sqlite3
from typing import List, Optional

FIELDS = ("id", "name", "token", "every", "grace", "created", "last_ping", "last_status", "last_note")
NOTE_LIMIT = 500   # characters of a failure ping's body kept, e.g. the end of a log


def create_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            token TEXT UNIQUE,
            every INTEGER,
            grace INTEGER,
            created REAL,
            last_ping REAL,
            last_status TEXT,
            last_note TEXT
        )
    """)


def state(check: dict, now: float) -> str:
    """"new" (no ping yet, not due), "ok", "late" or "failed"."""
    if check["last_status"] == "fail":
        return "failed"
    since = check["last_ping"] if check["last_ping"] is not None else check["created"]
    if now > since + check["every"] + check["grace"]:
        return "late"
    return "ok" if check["last_ping"] is not None else "new"


def _rows(conn, where: str = "", args: tuple = ()) -> List[dict]:
    rows = conn.execute(f"SELECT {', '.join(FIELDS)} FROM checkins {where} ORDER BY name COLLATE NOCASE",
                        args).fetchall()
    return [dict(zip(FIELDS, row)) for row in rows]


def list_all(db_path: str, now: float) -> List[dict]:
    conn = sqlite3.connect(db_path)
    checks = _rows(conn)
    conn.close()
    for check in checks:
        check["state"] = state(check, now)
    return checks


def get(db_path: str, check_id: int) -> Optional[dict]:
    conn = sqlite3.connect(db_path)
    found = _rows(conn, "WHERE id = ?", (check_id,))
    conn.close()
    return found[0] if found else None


def create(db_path: str, name: str, every: int, grace: int, now: float) -> dict:
    conn = sqlite3.connect(db_path)
    with conn:
        cur = conn.execute("INSERT INTO checkins (name, token, every, grace, created) VALUES (?, ?, ?, ?, ?)",
                           (name, secrets.token_urlsafe(12), every, grace, now))
    check = _rows(conn, "WHERE id = ?", (cur.lastrowid,))[0]
    conn.close()
    return check


def update(db_path: str, check_id: int, fields: dict) -> Optional[dict]:
    """Change name, every and/or grace; None for an unknown check-in."""
    conn = sqlite3.connect(db_path)
    with conn:
        for field in ("name", "every", "grace"):
            if field in fields:
                conn.execute(f"UPDATE checkins SET {field} = ? WHERE id = ?", (fields[field], check_id))
    found = _rows(conn, "WHERE id = ?", (check_id,))
    conn.close()
    return found[0] if found else None


def delete(db_path: str, check_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    with conn:
        removed = conn.execute("DELETE FROM checkins WHERE id = ?", (check_id,)).rowcount
    conn.close()
    return bool(removed)


def ping(db_path: str, token: str, ok: bool, note: str, now: float) -> bool:
    """Record a ping; False for an unknown token."""
    conn = sqlite3.connect(db_path)
    with conn:
        changed = conn.execute(
            "UPDATE checkins SET last_ping = ?, last_status = ?, last_note = ? WHERE token = ?",
            (now, "ok" if ok else "fail", note[:NOTE_LIMIT] if not ok else "", token)).rowcount
    conn.close()
    return bool(changed)
