"""Persistență SQLite: date de business, checkpoint-uri, efecte idempotente, audit.

Totul stă într-un singur fișier .db, ca un crash între „am salvat
checkpoint-ul” și „am salvat efectul” să nu poată lăsa două baze de date
în stări diferite. SQL-ul e scris ca să meargă și pe PostgreSQL, cu
excepția `INSERT OR IGNORE`, care acolo devine `ON CONFLICT DO NOTHING`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    idno TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email_domain TEXT NOT NULL,
    credit_limit REAL NOT NULL,
    balance REAL NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    orders_paid_on_time INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    stock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    customer_idno TEXT NOT NULL REFERENCES customers(idno),
    po_number TEXT NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    sku TEXT NOT NULL REFERENCES products(sku),
    qty INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT PRIMARY KEY,
    step INTEGER NOT NULL,
    next_node TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    interrupt TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    key TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    outcome TEXT NOT NULL,
    route TEXT,
    duration_ms REAL NOT NULL,
    at REAL NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_run ON audit(run_id);
"""

SEED_CUSTOMERS = [
    ("1003600012345", "Agro-Nord SRL", "agronord.md", 60000, 12000, 0, 8),
    ("1012600098765", "Clinica Vita Med SRL", "vitamed.md", 150000, 0, 0, 15),
    ("1009600054321", "Birou Expert Contabil SRL", "expertcont.md", 20000, 18500, 0, 3),
    ("1015600011111", "Constructii Rapide SRL", "construct-rapid.md", 40000, 0, 1, 0),
]

SEED_PRODUCTS = [
    ("LAP-TP14", "Laptop ThinkPad E14 Gen 6, 16 GB, 512 GB", 15999.0, 40),
    ("LAP-MBA13", "Laptop MacBook Air 13 M3, 16 GB", 23999.0, 6),
    ("MON-DL27", "Monitor Dell P2725H 27\"", 4299.0, 25),
    ("PRN-HP4103", "Imprimantă HP LaserJet Pro 4103fdw", 6999.0, 3),
    ("KIT-LOGI", "Kit tastatură + mouse Logitech MK540", 899.0, 120),
    ("UPS-APC1500", "UPS APC Back-UPS 1500VA", 3899.0, 0),
]


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; tranzacțiile le deschidem explicit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def seed(conn: sqlite3.Connection) -> None:
    conn.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?,?,?,?,?)", SEED_CUSTOMERS)
    conn.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?)", SEED_PRODUCTS)


class SqliteCheckpoints:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, run_id, *, step, next_node, state, status, error="", interrupt=None):
        self.conn.execute(
            """INSERT INTO checkpoints (run_id, step, next_node, state, status, error, interrupt, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET step=excluded.step, next_node=excluded.next_node,
                 state=excluded.state, status=excluded.status, error=excluded.error,
                 interrupt=excluded.interrupt, updated_at=excluded.updated_at""",
            (run_id, step, next_node, json.dumps(state, ensure_ascii=False), status, error,
             json.dumps(interrupt, ensure_ascii=False) if interrupt else None, time.time()))

    def load(self, run_id):
        row = self.conn.execute("SELECT * FROM checkpoints WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {"step": row["step"], "next_node": row["next_node"], "state": json.loads(row["state"]),
                "status": row["status"], "error": row["error"],
                "interrupt": json.loads(row["interrupt"]) if row["interrupt"] else None}

    def by_status(self, status: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT run_id FROM checkpoints WHERE status = ? ORDER BY updated_at",
                                 (status,)).fetchall()
        return [{"run_id": r["run_id"], **self.load(r["run_id"])} for r in rows]


class Effects:
    """Execută o acțiune externă cel mult o dată per cheie de idempotență.

    Dacă procesul cade după acțiune dar înainte de checkpoint, la reluare
    `once` găsește rezultatul salvat și nu repetă acțiunea. Cheia se trimite
    și serviciului extern (header `Idempotency-Key`), pentru fereastra
    rămasă: crash între apelul extern și scrierea rândului de aici.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.replayed = 0

    def once(self, key: str, action: Callable[[str], Any]) -> Any:
        row = self.conn.execute("SELECT result FROM effects WHERE key = ?", (key,)).fetchone()
        if row is not None:
            self.replayed += 1
            return json.loads(row["result"])
        result = action(key)
        self.conn.execute("INSERT INTO effects (key, result, created_at) VALUES (?,?,?)",
                          (key, json.dumps(result, ensure_ascii=False), time.time()))
        return result


class AuditLog:
    """Jurnal append-only cu lanț SHA-256: modificarea unui rând strică toate hash-urile de după el."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def _last_hash(self) -> str:
        row = self.conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        return row["hash"] if row else "0" * 64

    @staticmethod
    def _digest(prev: str, run_id: str, node: str, outcome: str, route: str | None, at: float) -> str:
        payload = json.dumps([prev, run_id, node, outcome, route, round(at, 6)], ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def record(self, run_id: str, node: str, outcome: str, state: dict, duration_ms: float) -> None:
        at, prev = time.time(), self._last_hash()
        route = state.get("route")
        self.conn.execute(
            "INSERT INTO audit (run_id, node, outcome, route, duration_ms, at, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, node, outcome, route, duration_ms, at, prev, self._digest(prev, run_id, node, outcome, route, at)))

    def verify(self) -> bool:
        prev = "0" * 64
        for row in self.conn.execute("SELECT * FROM audit ORDER BY id"):
            expected = self._digest(prev, row["run_id"], row["node"], row["outcome"], row["route"], row["at"])
            if row["prev_hash"] != prev or row["hash"] != expected:
                return False
            prev = row["hash"]
        return True

    def for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
