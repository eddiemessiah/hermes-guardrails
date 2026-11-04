"""SQLite ledger: payments, reservations, kill switch and a hash-chained audit log.

Every state change and every policy decision is appended to ``audit`` where
hash_n = sha256(hash_{n-1} || canonical_json(entry_n)). Editing, deleting or
reordering any row breaks ``verify()``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .policy import LedgerView

GENESIS = "0" * 64
RESERVATION_TTL_S = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
  key TEXT PRIMARY KEY, tool TEXT NOT NULL, payee TEXT NOT NULL, amount INTEGER NOT NULL,
  currency TEXT NOT NULL, status TEXT NOT NULL, day TEXT NOT NULL, approved_by TEXT,
  created REAL NOT NULL, updated REAL NOT NULL, result TEXT);
CREATE INDEX IF NOT EXISTS payments_day ON payments(day, status);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL,
  prev TEXT NOT NULL, hash TEXT NOT NULL);
"""

LIVE = ("reserved", "charging", "settled")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev: str, seq: int, ts: str, event: str, data: str) -> str:
    return hashlib.sha256((prev + _canon([seq, ts, event, data])).encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path | str, clock=time.time):
        self.path = str(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- plumbing -----------------------------------------------------------
    @contextmanager
    def tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def day(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")

    def _append(self, db, event: str, data: dict) -> str:
        row = db.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
        ts = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="seconds")
        payload = _canon(data)
        h = entry_hash(prev, seq, ts, event, payload)
        db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)", (seq, ts, event, payload, prev, h))
        return h

    def audit(self, event: str, data: dict) -> str:
        with self.tx() as db:
            return self._append(db, event, data)

    def _expire(self, db):
        cutoff = self._clock() - RESERVATION_TTL_S
        for (key,) in db.execute("SELECT key FROM payments WHERE status='reserved' AND updated < ?", (cutoff,)).fetchall():
            db.execute("UPDATE payments SET status='expired', updated=? WHERE key=?", (self._clock(), key))
            self._append(db, "reservation_expired", {"key": key})

    # -- kill switch --------------------------------------------------------
    def frozen(self, db=None) -> bool:
        with self._lock:
            row = (db or self._db).execute("SELECT v FROM kv WHERE k='frozen'").fetchone()
        return bool(row and row[0] == "1")

    def set_frozen(self, on: bool, actor: str) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO kv VALUES('frozen',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("1" if on else "0",))
            self._append(db, "kill_switch_on" if on else "kill_switch_off", {"actor": actor})

    # -- reads --------------------------------------------------------------
    def view(self, db, payee: str, key: str) -> LedgerView:
        self._expire(db)
        d = self.day()
        q = "SELECT COALESCE(SUM(amount),0) FROM payments WHERE day=? AND status IN ('reserved','charging','settled') AND key<>?"
        spent = db.execute(q, (d, key)).fetchone()[0]
        spent_p = db.execute(q + " AND payee=?", (d, key, payee)).fetchone()[0]
        known = db.execute("SELECT 1 FROM payments WHERE payee=? AND status='settled' LIMIT 1", (payee,)).fetchone() is not None
        row = db.execute("SELECT status, approved_by FROM payments WHERE key=?", (key,)).fetchone()
        status = row[0] if row and row[0] in LIVE else None
        if status == "reserved" and row[1] == "pending":
            status = "pending"  # approval was asked but never answered yes: ask again
        return LedgerView(self.frozen(db), spent, spent_p, known, status)

    def payment(self, key: str):
        with self._lock:
            return self._u_payment(key)

    def _u_payment(self, key: str):
        row = self._db.execute("SELECT key,tool,payee,amount,currency,status,approved_by,result FROM payments WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        cols = ("key", "tool", "payee", "amount", "currency", "status", "approved_by", "result")
        out = dict(zip(cols, row))
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    def totals(self) -> dict:
        with self._lock:
            return self._u_totals()

    def _u_totals(self) -> dict:
        d = self.day()
        rows = self._db.execute(
            "SELECT payee, SUM(amount) FROM payments WHERE day=? AND status IN ('reserved','charging','settled') GROUP BY payee", (d,)).fetchall()
        return {"day": d, "by_payee": {p: a for p, a in rows}, "total": sum(a for _, a in rows)}

    def entries(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return self._u_entries(limit)

    def _u_entries(self, limit: int = 20) -> list[dict]:
        rows = self._db.execute("SELECT seq, ts, event, data, hash FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{"seq": s, "ts": t, "event": e, "data": json.loads(d), "hash": h[:12]} for s, t, e, d, h in reversed(rows)]

    def verify(self) -> dict:
        with self._lock:
            return self._u_verify()

    def _u_verify(self) -> dict:
        prev, n = GENESIS, 0
        for seq, ts, event, data, p, h in self._db.execute("SELECT seq, ts, event, data, prev, hash FROM audit ORDER BY seq"):
            n += 1
            if seq != n or p != prev or entry_hash(prev, seq, ts, event, data) != h:
                return {"ok": False, "entries": n - 1, "broken_at": seq}
            prev = h
        return {"ok": True, "entries": n, "head": prev[:16]}

    # -- writes -------------------------------------------------------------
    def reserve(self, db, key, tool, payee, amount, currency, approved_by=None):
        now = self._clock()
        db.execute(
            "INSERT INTO payments(key,tool,payee,amount,currency,status,day,approved_by,created,updated) VALUES (?,?,?,?,?,'reserved',?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status='reserved', amount=excluded.amount, day=excluded.day, approved_by=excluded.approved_by, updated=excluded.updated",
            (key, tool, payee, amount, currency, self.day(), approved_by, now, now))

    def claim(self, key: str):
        """Atomically move a reservation to 'charging'. Returns the prior row, or None if not claimable."""
        with self.tx() as db:
            row = db.execute("SELECT status, approved_by FROM payments WHERE key=?", (key,)).fetchone()
            if not row or row[0] != "reserved":
                return row
            by = "human" if row[1] == "pending" else row[1]
            db.execute("UPDATE payments SET status='charging', approved_by=?, updated=? WHERE key=?", (by, self._clock(), key))
            if row[1] == "pending":
                self._append(db, "approved", {"key": key, "by": "human"})
            return ("claimed", by)

    def settle(self, key: str, result: dict) -> None:
        with self.tx() as db:
            db.execute("UPDATE payments SET status='settled', result=?, updated=? WHERE key=?", (_canon(result), self._clock(), key))
            self._append(db, "settled", {"key": key, "ref": result.get("id")})

    def fail(self, key: str, error: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE payments SET status='failed', result=?, updated=? WHERE key=? AND status IN ('reserved','charging')",
                       (_canon({"error": error[:300]}), self._clock(), key))
            self._append(db, "failed", {"key": key, "error": error[:300]})
