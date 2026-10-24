"""Muting a node for maintenance.

While a node is muted, nothing new alerts for it, or for the services it
hosts (matched by the address in their URL). Alerts already open can still
resolve. When the mute ends, the rules start over, so a node that is still
down then alerts as usual.

A mute lasts a set time, or "until back online": until the node has gone
quiet and reported again, plus BACK_GRACE for its containers and services
to start. Mutes are kept in the settings table as {hostname: mute}.
"""

import json
import sqlite3
import urllib.parse
from typing import Dict, List, Optional, Set

GONE_AFTER = 30          # seconds without a report that count as the node going down
BACK_GRACE = 600         # once it is back, seconds to stay muted while things start
BACK_LIMIT = 24 * 3600   # an "until back online" mute ends after this regardless
LONGEST = 7 * 86400      # longest timed mute

Mute = dict   # {"since", "until", "until_back", "gone", "back_at"}


def timed(now: float, seconds: float) -> Mute:
    return {"since": now, "until": now + seconds, "until_back": False, "gone": False, "back_at": None}


def until_back(now: float, device: Optional[dict]) -> Mute:
    # Already down when muted: the next report is the one that brings it back.
    down = device is None or now - device.get("last_seen", 0) >= GONE_AFTER
    return {"since": now, "until": now + BACK_LIMIT, "until_back": True, "gone": down, "back_at": None}


def note_report(mute: Mute, previous_report: Optional[float], now: float) -> bool:
    """A report arrived. After a gap (a reboot, say) the node is back:
    returns True when that changed the mute."""
    if not mute["until_back"] or mute["back_at"] is not None:
        return False
    if mute["gone"] or (previous_report is not None and now - previous_report >= GONE_AFTER):
        mute.update(gone=True, back_at=now)
        return True
    return False


def ends_at(mute: Mute) -> float:
    if mute["until_back"] and mute["back_at"] is not None:
        return min(mute["until"], mute["back_at"] + BACK_GRACE)
    return mute["until"]


def advance(mutes: Dict[str, Mute], devices: Dict[str, dict], now: float) -> bool:
    """Notice nodes going quiet and end mutes that are over (or whose node
    was removed). Returns whether anything changed."""
    changed = False
    for host, mute in list(mutes.items()):
        device = devices.get(host)
        if device is None or now >= ends_at(mute):
            del mutes[host]
            changed = True
        elif mute["until_back"] and not mute["gone"] and now - device.get("last_seen", 0) >= GONE_AFTER:
            mute["gone"] = True
            changed = True
    return changed


def services_on(hosts: Set[str], devices: Dict[str, dict], services: List[dict]) -> Set[str]:
    """Names of the services whose URL points at one of these nodes, by IP
    address or hostname."""
    addresses = set()
    for host in hosts:
        addresses.add(host.lower())
        if devices.get(host, {}).get("ip"):
            addresses.add(devices[host]["ip"])
    return {s["name"] for s in services
            if (urllib.parse.urlparse(s.get("url", "")).hostname or "").lower() in addresses}


def public(mutes: Dict[str, Mute]) -> Dict[str, dict]:
    """What the browser shows: when each mute ends, or that it waits for the node."""
    return {host: {"since": m["since"], "until": ends_at(m), "until_back": m["until_back"],
                   "back": m["back_at"] is not None}
            for host, m in mutes.items()}


def read(db_path: str) -> Dict[str, Mute]:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = 'mutes'").fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


def write(db_path: str, mutes: Dict[str, Mute]):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('mutes', ?)", (json.dumps(mutes),))
    conn.close()
