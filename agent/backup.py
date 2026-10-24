"""Nightly copy of the dashboard's database onto this node.

Off unless HOMELAB_BACKUP_DIR and HOMELAB_BACKUP_KEY (the backup key from the
dashboard's Alerts page) are set in the agent environment. Once a day, after
HOMELAB_BACKUP_HOUR (local time, default 3), the agent downloads a snapshot,
checks it, keeps the newest HOMELAB_BACKUP_KEEP (default 14) and tells the
dashboard how it went. A failed run is tried again an hour later.
"""

import gzip
import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

BACKUP_DIR = os.environ.get("HOMELAB_BACKUP_DIR", "").strip()
BACKUP_KEY = os.environ.get("HOMELAB_BACKUP_KEY", "").strip()
BACKUP_HOUR = int(os.environ.get("HOMELAB_BACKUP_HOUR", "3"))
BACKUP_KEEP = max(1, int(os.environ.get("HOMELAB_BACKUP_KEEP", "14")))
RETRY_AFTER = 3600
PREFIX, SUFFIX = "homelab-dashboard-", ".db.gz"


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


class Backup:
    def __init__(self, report_url, directory=BACKUP_DIR, key=BACKUP_KEY, hour=BACKUP_HOUR, keep=BACKUP_KEEP):
        # http://host:8000/api/report -> http://host:8000
        self.base = report_url.rsplit("/api/report", 1)[0].rstrip("/")
        self.directory, self.key, self.hour, self.keep = directory, key, hour, keep
        self.last_attempt = 0.0
        self.running = False

    @property
    def enabled(self):
        return bool(self.directory and self.key)

    def path_for(self, now):
        return os.path.join(self.directory, f"{PREFIX}{time.strftime('%Y-%m-%d', time.localtime(now))}{SUFFIX}")

    def due(self, now):
        """After the hour, with no copy from today yet, and not just tried."""
        return (self.enabled and not self.running and time.localtime(now).tm_hour >= self.hour
                and not os.path.exists(self.path_for(now)) and now - self.last_attempt >= RETRY_AFTER)

    def maybe_start(self, now=None):
        now = time.time() if now is None else now
        if self.due(now):
            self.running, self.last_attempt = True, now
            threading.Thread(target=self.run, args=(now,), daemon=True).start()

    def run(self, now):
        try:
            path, size = self.fetch(now)
            kept = self.prune()
            detail = f"{os.path.basename(path)}, {_size(size)}, {kept} kept in {self.directory}"
            print(f"Dashboard backup saved: {detail}")
            self.report(True, detail)
        except Exception as e:
            print(f"Dashboard backup failed: {e}")
            self.report(False, str(e))
        finally:
            self.running = False

    def fetch(self, now):
        """Download into a .part file and move it into place only once it
        checks out, so a broken download never replaces anything."""
        os.makedirs(self.directory, exist_ok=True)
        final = self.path_for(now)
        part = final + ".part"
        try:
            self.download(part)
        except BaseException:
            if os.path.exists(part):
                os.remove(part)
            raise
        os.replace(part, final)
        return final, os.path.getsize(final)

    def download(self, part):
        """Fetch a snapshot, then check its checksum and that it unpacks to an SQLite database."""
        req = urllib.request.Request(f"{self.base}/api/backup/snapshot", headers={"X-Backup-Key": self.key})
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(part, "wb") as f:
                expected = resp.headers.get("X-Backup-SHA256")
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    digest.update(chunk)
                    f.write(chunk)
        except urllib.error.HTTPError as e:
            older = "the dashboard server is running older code; run scripts/update.sh on it"
            raise RuntimeError({401: "the dashboard rejected the backup key; copy it again from the Alerts page",
                                404: older, 405: older}.get(e.code, f"the dashboard answered {e.code}")) from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not reach the dashboard ({e.reason})") from None
        if expected and digest.hexdigest() != expected:
            raise RuntimeError("the download was damaged (checksum mismatch)")
        with gzip.open(part, "rb") as g:
            if g.read(16) != b"SQLite format 3\x00":
                raise RuntimeError("the download is not an SQLite database")
            while g.read(1 << 20):
                pass

    def prune(self):
        """Keep the newest `keep` copies (the names sort by date); drop stray partial files."""
        names = sorted(n for n in os.listdir(self.directory) if n.startswith(PREFIX) and n.endswith(SUFFIX))
        for name in names[:-self.keep]:
            os.remove(os.path.join(self.directory, name))
        for name in os.listdir(self.directory):
            if name.startswith(PREFIX) and name.endswith(".part"):
                os.remove(os.path.join(self.directory, name))
        return min(len(names), self.keep)

    def report(self, ok, detail):
        body = json.dumps({"ok": ok, "detail": detail, "host": socket.gethostname()}).encode()
        req = urllib.request.Request(f"{self.base}/api/backup/report", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Backup-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            # The dashboard's check-in goes late without it, which alerts.
            print(f"Could not report the backup to the dashboard: {e}")
