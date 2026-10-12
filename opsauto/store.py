"""SQLite persistence: events (idempotency ledger), customers, dead-letter queue, audit log."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  idem_key TEXT PRIMARY KEY, kind TEXT, ref TEXT, email TEXT, payload TEXT,
  status TEXT, total REAL, currency TEXT, country TEXT, payment TEXT,
  risk INTEGER, flags TEXT DEFAULT '', notified INTEGER DEFAULT 0, result TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS customers(
  email TEXT PRIMARY KEY, orders INTEGER DEFAULT 0, total_spent REAL DEFAULT 0,
  flagged_orders INTEGER DEFAULT 0, first_seen TEXT);
CREATE TABLE IF NOT EXISTS dead_letters(
  id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT, stage TEXT, payload TEXT, error TEXT,
  attempts INTEGER DEFAULT 1, created_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, idem_key TEXT, detail TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._tx() as c:
            c.executescript(SCHEMA)

    def _tx(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return _Tx(c)

    # --- idempotency -------------------------------------------------------------------
    def claim(self, key, kind, ref, payload):
        """Insert the key atomically. Returns None if we own it, else the existing row."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO events(idem_key,kind,ref,payload,status,created_at) "
                "VALUES(?,?,?,?, 'processing', ?)", (key, kind, ref, json.dumps(payload), now()))
            if cur.rowcount:
                return None
            return dict(c.execute("SELECT * FROM events WHERE idem_key=?", (key,)).fetchone())

    def complete(self, key, ctx, flags, risky=False):
        with self._tx() as c:
            c.execute(
                "UPDATE events SET status='done',email=?,total=?,currency=?,country=?,payment=?,"
                "risk=?,flags=? WHERE idem_key=?",
                (ctx["email"], ctx.get("total"), ctx.get("currency"), ctx.get("country"),
                 ctx.get("payment_method"), ctx.get("risk_score"), ",".join(flags), key))
            if ctx["kind"] == "order":
                c.execute("INSERT OR IGNORE INTO customers(email,first_seen) VALUES(?,?)",
                          (ctx["email"], now()))
                c.execute("UPDATE customers SET orders=orders+1,total_spent=total_spent+?,"
                          "flagged_orders=flagged_orders+? WHERE email=?",
                          (ctx["total"], int(risky), ctx["email"]))
            return dict(c.execute("SELECT * FROM events WHERE idem_key=?", (key,)).fetchone())

    def set_status(self, key, status, notified=None, result=None):
        with self._tx() as c:
            c.execute("UPDATE events SET status=?, notified=COALESCE(?,notified), "
                      "result=COALESCE(?,result) WHERE idem_key=?",
                      (status, notified, json.dumps(result) if result else None, key))

    def customer(self, email):
        with self._tx() as c:
            r = c.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
            return dict(r) if r else None

    # --- dead letters / audit ------------------------------------------------------------
    def dlq_add(self, key, stage, payload, error, attempts=1):
        with self._tx() as c:
            c.execute("INSERT INTO dead_letters(idem_key,stage,payload,error,attempts,created_at) "
                      "VALUES(?,?,?,?,?,?)", (key, stage, json.dumps(payload), str(error), attempts, now()))

    def dlq_open(self, id=None):
        with self._tx() as c:
            q = "SELECT * FROM dead_letters WHERE resolved_at IS NULL" + (" AND id=?" if id else "")
            return [dict(r) for r in c.execute(q + " ORDER BY id", (id,) if id else ())]

    def dlq_resolve(self, id, ok, error=""):
        with self._tx() as c:
            if ok:
                c.execute("UPDATE dead_letters SET resolved_at=? WHERE id=?", (now(), id))
            else:
                c.execute("UPDATE dead_letters SET attempts=attempts+1,error=? WHERE id=?", (error, id))

    def audit(self, actor, action, key="", detail=None):
        with self._tx() as c:
            c.execute("INSERT INTO audit(ts,actor,action,idem_key,detail) VALUES(?,?,?,?,?)",
                      (now(), actor, action, key, json.dumps(detail) if detail is not None else ""))

    def audit_tail(self, n=20):
        with self._tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n,))]

    # --- reporting -----------------------------------------------------------------------
    def events(self, limit=None):
        with self._tx() as c:
            q = "SELECT * FROM events ORDER BY created_at DESC, rowid DESC" + (" LIMIT ?" if limit else "")
            return [dict(r) for r in c.execute(q, (limit,) if limit else ())]

    def stats(self):
        ev = self.events()
        flags: dict[str, int] = {}
        for e in ev:
            for f in filter(None, (e["flags"] or "").split(",")):
                flags[f] = flags.get(f, 0) + 1
        by = lambda k: {v: sum(1 for e in ev if e[k] == v) for v in {e[k] for e in ev}}  # noqa: E731
        risks = [e["risk"] for e in ev if e["risk"] is not None]
        return {"total": len(ev), "by_status": by("status"), "by_kind": by("kind"), "flags": flags,
                "notified": sum(e["notified"] or 0 for e in ev),
                "avg_risk": round(sum(risks) / len(risks), 1) if risks else 0,
                "dlq_open": len(self.dlq_open()),
                "recent": [{k: e[k] for k in ("created_at", "kind", "ref", "email", "total", "risk", "flags", "status")}
                           for e in ev[:15]]}


class _Tx:
    def __init__(self, c): self.c = c
    def __enter__(self): return self.c
    def __exit__(self, et, ev, tb):
        with closing(self.c):
            self.c.commit() if et is None else self.c.rollback()
