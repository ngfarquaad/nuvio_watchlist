"""The account settings and the add-on profiles, kept in one SQLite file under DATA_DIR."""

import json
import os
import sqlite3
import threading
import time
import uuid


class Store:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(os.path.join(data_dir, "nuvio_watchlist.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS profiles ("
                "id TEXT PRIMARY KEY, number INTEGER NOT NULL UNIQUE, label TEXT NOT NULL, "
                "settings TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "activated INTEGER NOT NULL DEFAULT 0)")

    def close(self):
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ key/value
    def _get_kv(self, key, default=None):
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def _set_kv(self, key, value):
        self._db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, json.dumps(value)))

    def general(self):
        with self._lock:
            return self._get_kv("general", {})

    def set_general(self, values):
        with self._lock, self._db:
            self._set_kv("general", values)

    # ------------------------------------------------------------ profiles
    @staticmethod
    def _row(row):
        return {"id": row["id"], "number": row["number"], "label": row["label"],
                "settings": json.loads(row["settings"]), "activated": bool(row["activated"]), "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def list(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM profiles ORDER BY number").fetchall()
        return [self._row(r) for r in rows]

    def get(self, pid):
        with self._lock:
            row = self._db.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
        return self._row(row) if row else None

    def create(self, settings, label=None):
        """Add a profile with the lowest unused number (1, 2, 3, ...) and a new UUID."""
        with self._lock, self._db:
            used = {r["number"] for r in self._db.execute("SELECT number FROM profiles")}
            number = next(n for n in range(1, len(used) + 2) if n not in used)
            pid = str(uuid.uuid4())
            now = time.time()
            # a new profile stays idle (nothing is fetched from Nuvio) until it is saved, refreshed or installed
            self._db.execute("INSERT INTO profiles (id, number, label, settings, created_at, updated_at, activated) "
                             "VALUES (?,?,?,?,?,?,0)",
                             (pid, number, label or f"Profile {number}", json.dumps(settings), now, now))
        return self.get(pid)

    def update(self, pid, label, settings):
        with self._lock, self._db:
            self._db.execute("UPDATE profiles SET label = ?, settings = ?, updated_at = ?, activated = 1 WHERE id = ?",
                             (label, json.dumps(settings), time.time(), pid))

    def activate(self, pid):
        with self._lock, self._db:
            self._db.execute("UPDATE profiles SET activated = 1 WHERE id = ?", (pid,))

    def delete(self, pid):
        with self._lock, self._db:
            self._db.execute("DELETE FROM profiles WHERE id = ?", (pid,))

    def wipe(self):
        """Remove every setting and profile. The empty database file stays because it is open."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM profiles")
            self._db.execute("DELETE FROM kv")
        with self._lock:
            self._db.execute("VACUUM")
