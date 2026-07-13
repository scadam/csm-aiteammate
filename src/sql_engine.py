"""
Relational back end for NL-to-SQL.

When a real Snowflake account is configured (``config.USE_SNOWFLAKE``), queries
are executed against Snowflake (``src.db.snowflake_client``). Otherwise an
in-memory SQLite database — seeded from the ``data/*.json`` fixtures — is used as
a drop-in simulation so the agent runs with no external dependencies. Both paths
accept only read-only SELECT/WITH statements.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any

from . import config, data_store

# Tables exposed to the NL-to-SQL engine (the relational "Snowflake" surface).
SQL_TABLES: tuple[str, ...] = (
    "accounts",
    "signals",
    "signal_action_map",
    "routing_rules",
    "enhancements",
    "content_library",
    "voc",
    "csm_voice",
    "px_engagement",
    "review_queue",
    "managers",
)

_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None
_scoped_conns: dict[str, sqlite3.Connection] = {}

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE|ATTACH|PRAGMA)\b",
    re.IGNORECASE,
)

# Tables that carry a per-CSM owner column, so a user-delegated query only ever
# returns that CSM's rows (the simulation of a Snowflake row-access policy).
_OWNED_TABLES: dict[str, str] = {
    "accounts": "csm_manager_id",
    "review_queue": "csm_manager_id",
    "csm_voice": "csm_manager_id",
}


def _cell(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _build(scope_manager_id: str | None = None) -> sqlite3.Connection:
    """Build an in-memory SQLite DB from the fixtures.

    When ``scope_manager_id`` is given, tables that carry a per-CSM owner column
    are seeded with **only that CSM's rows** — a faithful simulation of a
    Snowflake row-access policy bound to the user-delegated identity. Signals are
    additionally trimmed to the CSM's accounts.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    owned_accounts: set[str] | None = None
    if scope_manager_id:
        owned_accounts = {
            a["account_id"] for a in data_store.table("accounts")
            if a.get("csm_manager_id") == scope_manager_id
        }

    for name in SQL_TABLES:
        rows = data_store.table(name)
        if scope_manager_id:
            owner_col = _OWNED_TABLES.get(name)
            if owner_col:
                rows = [r for r in rows if r.get(owner_col) == scope_manager_id]
            elif name == "signals" and owned_accounts is not None:
                rows = [r for r in rows if r.get("account_id") in owned_accounts]
        columns: list[str] = []
        seen: set[str] = set()
        for row in data_store.table(name):  # full column set, even if rows are trimmed
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        if not columns:
            continue
        col_defs = ", ".join(f'"{c}"' for c in columns)
        cur.execute(f'CREATE TABLE "{name}" ({col_defs})')
        placeholders = ", ".join(["?"] * len(columns))
        for row in rows:
            cur.execute(
                f'INSERT INTO "{name}" ({col_defs}) VALUES ({placeholders})',
                [_cell(row.get(c)) for c in columns],
            )
    conn.commit()
    return conn


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _LOCK:
        if _conn is None:
            _conn = _build()
        return _conn


def _get_scoped_conn(scope_manager_id: str) -> sqlite3.Connection:
    with _LOCK:
        conn = _scoped_conns.get(scope_manager_id)
        if conn is None:
            conn = _build(scope_manager_id)
            _scoped_conns[scope_manager_id] = conn
        return conn


def refresh() -> None:
    """Rebuild the in-memory database(s) from the (possibly mutated) fixtures."""
    global _conn
    with _LOCK:
        if _conn is not None:
            _conn.close()
        _conn = None
        for c in _scoped_conns.values():
            c.close()
        _scoped_conns.clear()


def execute_sql(sql: str) -> dict:
    """
    Execute a read-only SELECT/WITH statement against the active back end.

    Routes to real Snowflake when configured, otherwise the SQLite simulation.
    Raises ValueError if the statement is not read-only.
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not re.match(r"^(SELECT|WITH)\s", cleaned, re.IGNORECASE):
        raise ValueError("Only SELECT/WITH statements are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("Forbidden SQL keyword detected.")

    if config.USE_SNOWFLAKE:
        from .db import snowflake_client

        return snowflake_client.execute_sql(cleaned)

    with _LOCK:
        conn = _get_conn()
        cur = conn.cursor()
        t0 = time.monotonic()
        cur.execute(cleaned)
        rows = cur.fetchall()
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        columns = [d[0] for d in cur.description] if cur.description else []
        result_rows = [dict(r) for r in rows]
        cur.close()

    return {
        "sql": cleaned,
        "columns": columns,
        "rows": result_rows,
        "row_count": len(result_rows),
        "execution_time_ms": elapsed_ms,
    }
