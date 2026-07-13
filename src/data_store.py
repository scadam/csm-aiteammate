"""
Thin repository layer over the static JSON fixtures in ``data/``.

Every back-end system (Snowflake, Gainsight CS/PX, Work IQ / Microsoft 365) is
simulated with static JSON. Tools and the agent must go through this layer and
never read files directly, so the JSON can later be swapped for real systems
without touching tool or agent logic.

Fixtures are treated as read-mostly. Writes (the learning loop) are kept
in-memory for the life of the process and are not persisted to disk.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import config

# Known simulated tables (file name without .json under data/).
TABLES: tuple[str, ...] = (
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
    "owners",
)

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}


def _path(name: str):
    return config.DATA_DIR / f"{name}.json"


def load(name: str) -> Any:
    """Load and cache the JSON fixture ``name`` (without extension)."""
    with _LOCK:
        if name not in _CACHE:
            with open(_path(name), "r", encoding="utf-8") as fh:
                _CACHE[name] = json.load(fh)
        return _CACHE[name]


def table(name: str) -> list[dict]:
    """Return a fixture as a list of row dicts."""
    data = load(name)
    if isinstance(data, list):
        return data
    raise TypeError(f"Fixture '{name}' is not a table (expected a JSON array).")


def find(name: str, **filters: Any) -> list[dict]:
    """Return rows from ``name`` matching all equality ``filters`` (case-insensitive for str)."""
    rows = table(name)
    out: list[dict] = []
    for row in rows:
        ok = True
        for key, value in filters.items():
            cell = row.get(key)
            if isinstance(cell, str) and isinstance(value, str):
                if cell.lower() != value.lower():
                    ok = False
                    break
            elif cell != value:
                ok = False
                break
        if ok:
            out.append(row)
    return out


def get(name: str, id_field: str, id_value: Any) -> dict | None:
    """Return the first row in ``name`` where ``id_field == id_value``, or None."""
    matches = find(name, **{id_field: id_value})
    return matches[0] if matches else None


def resolve_account(name_or_id: str) -> dict | None:
    """Resolve an account by its id (``ACC-…``) OR its name (case-insensitive).

    Tools take ``account_id``, but a person (and the reasoning loop) naturally
    refer to an account by name ("Nordia Bank"). Accepting either removes the
    brittle id-only contract that otherwise hard-fails on a name. Tries, in order:
    exact id, exact name, then a contains-match on the name.
    """
    if not name_or_id:
        return None
    hit = get("accounts", "account_id", name_or_id)
    if hit:
        return hit
    hit = get("accounts", "account_name", name_or_id)
    if hit:
        return hit
    needle = str(name_or_id).strip().lower()
    if len(needle) >= 3:
        for row in table("accounts"):
            nm = str(row.get("account_name", "")).lower()
            if nm and (needle in nm or nm in needle):
                return row
    return None


def append(name: str, row: dict) -> dict:
    """Append a row to an in-memory table (not persisted)."""
    with _LOCK:
        table(name).append(row)
        return row


def update(name: str, id_field: str, id_value: Any, changes: dict) -> dict | None:
    """Update the first matching row in-memory (not persisted). Returns the row or None."""
    with _LOCK:
        row = get(name, id_field, id_value)
        if row is None:
            return None
        row.update(changes)
        return row
