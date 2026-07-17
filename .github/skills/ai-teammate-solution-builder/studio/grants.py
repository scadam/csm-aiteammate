"""Transactional confirmation grants for one Spec Studio session."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ACTIVE_EXECUTION_STATUSES = {"claimed", "checkpoint"}
VISIBLE_STATUSES = {"issued", "claimed", "checkpoint", "complete", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def exclusive_lock(path: str | Path) -> Iterator[None]:
    """Serialize draft and grant transitions across local plugin processes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GrantLedger:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = self.root / "grants.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def issue(self, grant: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT grant_id FROM grants WHERE session_id = ? AND status IN ('claimed', 'checkpoint')",
                (grant["sessionId"],),
            ).fetchone()
            if active:
                raise ValueError("A confirmed build is already running or waiting at a checkpoint")
            connection.execute(
                "UPDATE grants SET status = 'revoked', updated_at = ? "
                "WHERE session_id = ? AND status = 'issued'",
                (utc_now(), grant["sessionId"]),
            )
            connection.execute(
                """
                INSERT INTO grants (
                    grant_id, session_id, revision, digest, action, output_path, force,
                    tenant_id, snapshot_path, issued_at, expires_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?)
                """,
                (
                    grant["grantId"],
                    grant["sessionId"],
                    grant["revision"],
                    grant["digest"],
                    grant["action"],
                    grant["outputPath"],
                    int(grant["force"]),
                    grant["tenantId"],
                    grant["snapshotPath"],
                    grant["issuedAt"],
                    grant["expiresAt"],
                    utc_now(),
                ),
            )
            connection.commit()

    def get(self, grant_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return self._record(row) if row else None

    def claim(self, grant_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Confirmation grant does not exist")
            grant = self._record(row)
            if grant["status"] != "issued":
                if grant["status"] == "revoked":
                    raise ValueError("Confirmation grant was invalidated by a later draft change")
                raise ValueError(f"Confirmation grant cannot be claimed from status {grant['status']}")
            expires = datetime.fromisoformat(grant["expiresAt"])
            if expires <= datetime.now(timezone.utc):
                connection.execute(
                    "UPDATE grants SET status = 'expired', updated_at = ? WHERE grant_id = ?",
                    (utc_now(), grant_id),
                )
                connection.commit()
                raise ValueError("Confirmation grant has expired")
            changed = connection.execute(
                "UPDATE grants SET status = 'claimed', updated_at = ? "
                "WHERE grant_id = ? AND status = 'issued'",
                (utc_now(), grant_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise ValueError("Confirmation grant was claimed concurrently")
            connection.commit()
        grant["status"] = "claimed"
        return grant

    def update_status(self, grant_id: str, status: str) -> dict[str, Any]:
        if status not in {"checkpoint", "complete", "failed", "revoked"}:
            raise ValueError(f"Unsupported confirmation status: {status}")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Confirmation grant does not exist")
            current = str(row["status"])
            allowed = {
                "claimed": {"checkpoint", "complete", "failed"},
                "checkpoint": {"checkpoint", "complete", "failed"},
                "issued": {"revoked"},
            }
            if status not in allowed.get(current, set()):
                raise ValueError(f"Cannot move confirmation grant from {current} to {status}")
            connection.execute(
                "UPDATE grants SET status = ?, updated_at = ? WHERE grant_id = ?",
                (status, utc_now(), grant_id),
            )
            connection.commit()
        result = self.get(grant_id)
        if result is None:
            raise ValueError("Confirmation grant disappeared after update")
        return result

    def assert_mutable(self, session_id: str) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status FROM grants WHERE session_id = ? "
                "AND status IN ('claimed', 'checkpoint') LIMIT 1",
                (session_id,),
            ).fetchone()
        if row:
            raise ValueError(
                "The confirmed build is running or waiting at an A365 checkpoint; resume it before editing"
            )

    def revoke_issued(self, grant_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE grants SET status = 'revoked', updated_at = ? "
                "WHERE grant_id = ? AND status = 'issued'",
                (utc_now(), grant_id),
            )
            connection.commit()

    def revoke_session_issued(self, session_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE grants SET status = 'revoked', updated_at = ? "
                "WHERE session_id = ? AND status = 'issued'",
                (utc_now(), session_id),
            )
            connection.commit()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    action TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    force INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS grants_session_status "
                "ON grants(session_id, status)"
            )
            connection.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "grantId": row["grant_id"],
            "sessionId": row["session_id"],
            "revision": int(row["revision"]),
            "digest": row["digest"],
            "action": row["action"],
            "outputPath": row["output_path"],
            "force": bool(row["force"]),
            "tenantId": row["tenant_id"],
            "snapshotPath": row["snapshot_path"],
            "issuedAt": row["issued_at"],
            "expiresAt": row["expires_at"],
            "status": row["status"],
            "updatedAt": row["updated_at"],
        }


def read_receipt(path: str | Path) -> tuple[Path, dict[str, str]]:
    target = Path(path).resolve()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Confirmation receipt is missing or invalid") from exc
    if set(value) != {"schemaVersion", "grantId", "sessionId"} or value.get("schemaVersion") != 2:
        raise ValueError("Confirmation receipt has an invalid shape")
    if not all(isinstance(value.get(key), str) and value[key] for key in ("grantId", "sessionId")):
        raise ValueError("Confirmation receipt identifiers are invalid")
    return target, value
