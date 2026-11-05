"""The simulated company's database: one SQLite file shared by the MCP servers and the harness.

- `Company.create(path, seed)` builds the schema and the synthetic data (see generate.py);
- `snapshot()` / `restore()` save and reload the whole state inside the same file (a
  `snap_*` copy of every table), so a task starts from a known state and the servers' open
  connections see the reset without reconnecting;
- `now()` is the simulation clock (a fixed date, so refund windows are deterministic);
- `audit()` records every tool call; `set_task()` tags what follows with the task id.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
TABLES = [
    "customers",
    "orders",
    "order_items",
    "returns",
    "plans",
    "subscriptions",
    "charges",
    "refunds",
    "appointments",
    "slots",
    "tickets",
    "ticket_notes",
    "ticket_replies",
    "verifications",
    "kb_articles",
    "approvals",
    "audit_log",
]
DEFAULT_NOW = "2026-11-04T10:00:00"


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


class Company:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.con = connect(self.path)

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def create(cls, path: Path | str, seed: int = 20261104, now: str = DEFAULT_NOW) -> Company:
        from opsagent.sim.generate import populate

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
        company = cls(p)
        company.con.executescript(SCHEMA)
        company.set_context("now", now)
        company.set_context("task_id", "")
        populate(company, seed)
        company.snapshot("base")
        return company

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------ context
    def set_context(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO sim_context(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def context(self, key: str, default: str = "") -> str:
        row = self.con.execute("SELECT value FROM sim_context WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def now(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.context("now", DEFAULT_NOW))

    def set_task(self, task_id: str) -> None:
        self.set_context("task_id", task_id)

    def task_id(self) -> str:
        return self.context("task_id")

    # ------------------------------------------------------------------ snapshots
    def snapshot(self, name: str) -> None:
        """Copy every table into snap_<name>_<table> (replacing an older snapshot)."""
        with self.transaction():
            for t in TABLES:
                self.con.execute(f"DROP TABLE IF EXISTS snap_{name}_{t}")
                self.con.execute(f"CREATE TABLE snap_{name}_{t} AS SELECT * FROM {t}")
            self.con.execute(f"DROP TABLE IF EXISTS snap_{name}_sim_context")
            self.con.execute(f"CREATE TABLE snap_{name}_sim_context AS SELECT * FROM sim_context")

    def restore(self, name: str) -> None:
        with self.transaction():
            self.con.execute("PRAGMA defer_foreign_keys = ON")
            for t in reversed(TABLES):
                self.con.execute(f"DELETE FROM {t}")
            for t in TABLES:
                self.con.execute(f"INSERT INTO {t} SELECT * FROM snap_{name}_{t}")
            self.con.execute("DELETE FROM sim_context")
            self.con.execute(f"INSERT INTO sim_context SELECT * FROM snap_{name}_sim_context")
            # autoincrement counters restart from the data, not from the snapshot's high-water mark
            self.con.execute("DELETE FROM sqlite_sequence")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")

    # ------------------------------------------------------------------ helpers
    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.con.execute(sql, tuple(params)).fetchall()]

    def row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        r = self.con.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.con.execute(sql, tuple(params))

    def audit(
        self, server: str, tool: str, kind: str, params: dict[str, Any], result_summary: str
    ) -> None:
        self.con.execute(
            "INSERT INTO audit_log(task_id, server, tool, kind, params_json, result_summary, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.task_id(),
                server,
                tool,
                kind,
                json.dumps(params, default=str, sort_keys=True),
                result_summary[:500],
                self.now().isoformat(timespec="seconds"),
            ),
        )

    def audit_for(self, task_id: str) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM audit_log WHERE task_id = ? ORDER BY id", (task_id,))

    def diff(self, name: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Rows added / removed / changed since snapshot `name`, per business table (the audit
        log and context excluded). Keyed by primary key when the table has a text id."""
        out: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for t in TABLES:
            if t in ("audit_log",):
                continue
            before = {self._key(t, r): r for r in self.rows(f"SELECT * FROM snap_{name}_{t}")}
            after = {self._key(t, r): r for r in self.rows(f"SELECT * FROM {t}")}
            added = [after[k] for k in after.keys() - before.keys()]
            removed = [before[k] for k in before.keys() - after.keys()]
            changed = [
                {"before": before[k], "after": after[k]}
                for k in before.keys() & after.keys()
                if before[k] != after[k]
            ]
            if added or removed or changed:
                out[t] = {"added": added, "removed": removed, "changed": changed}
        return out

    @staticmethod
    def _key(table: str, row: dict[str, Any]) -> Any:
        if table == "slots":
            return row["slot_at"]
        return row.get("id")
