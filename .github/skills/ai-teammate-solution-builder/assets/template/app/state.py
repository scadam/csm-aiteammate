"""SQLite development store for workflow runs and review decisions."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore(Protocol):
    def create_run(self, workflow_id: str, manager_id: str, subject_id: str,
                   trigger_mode: str, input_data: dict[str, Any], request_key: str = "") -> dict[str, Any]: ...
    def save_run(self, run_id: str, status: str, results: list[dict[str, Any]]) -> dict[str, Any]: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def list_runs(self, manager_id: str | None = None) -> list[dict[str, Any]]: ...
    def create_review(self, run_id: str, manager_id: str, decisions: list[str],
                      context: dict[str, Any]) -> dict[str, Any]: ...
    def get_review(self, review_id: str) -> dict[str, Any] | None: ...
    def list_reviews(self, manager_id: str | None = None,
                     pending_only: bool = False) -> list[dict[str, Any]]: ...
    def decide_review(self, review_id: str, decision: str,
                      final_data: dict[str, Any]) -> dict[str, Any]: ...
    def claim_effect(self, key: str, capability_id: str) -> tuple[bool, dict[str, Any] | None]: ...
    def complete_effect(self, key: str, result: Any) -> None: ...
    def fail_effect(self, key: str, error: str) -> None: ...
    def record_event(self, capability_id: str, manager_id: str,
                     run_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    @property
    def provenance(self) -> str: ...


class SQLiteStateStore:
    """Durable for local development; replace with a shared store before scaling out."""

    def __init__(self, path: str | Path | None = None):
        configured = path or os.getenv("STATE_DB_PATH")
        self.path = str(configured or (Path(__file__).resolve().parents[1] / ".state" / "runtime.db"))
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    manager_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    trigger_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    manager_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decisions_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    decision TEXT,
                    final_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effects (
                    idempotency_key TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_requests (
                    request_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    manager_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create_run(
        self,
        workflow_id: str,
        manager_id: str,
        subject_id: str,
        trigger_mode: str,
        input_data: dict[str, Any],
        request_key: str = "",
    ) -> dict[str, Any]:
        now = _now()
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        with self._lock, self._connection:
            if request_key:
                existing = self._connection.execute(
                    "SELECT run_id FROM workflow_requests WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if existing:
                    return self.get_run(existing["run_id"]) or {}
            self._connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, workflow_id, manager_id, subject_id, trigger_mode, "running", _dump(input_data), "[]", now, now),
            )
            if request_key:
                self._connection.execute(
                    "INSERT INTO workflow_requests VALUES (?, ?, ?)",
                    (request_key, run_id, now),
                )
        return self.get_run(run_id) or {}

    def save_run(self, run_id: str, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE runs SET status = ?, results_json = ?, updated_at = ? WHERE id = ?",
                (status, _dump(results), _now(), run_id),
            )
        return self.get_run(run_id) or {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _run(row) if row else None

    def list_runs(self, manager_id: str | None = None) -> list[dict[str, Any]]:
        if manager_id:
            rows = self._connection.execute(
                "SELECT * FROM runs WHERE manager_id = ? ORDER BY created_at DESC", (manager_id,)
            ).fetchall()
        else:
            rows = self._connection.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [_run(row) for row in rows]

    def create_review(
        self, run_id: str, manager_id: str, decisions: list[str], context: dict[str, Any]
    ) -> dict[str, Any]:
        now = _now()
        review_id = f"review_{uuid.uuid4().hex[:16]}"
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, run_id, manager_id, "pending", _dump(decisions), _dump(context), None, "{}", now, now),
            )
        return self.get_review(review_id) or {}

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return _review(row) if row else None

    def list_reviews(self, manager_id: str | None = None, pending_only: bool = False) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[str] = []
        if manager_id:
            clauses.append("manager_id = ?")
            values.append(manager_id)
        if pending_only:
            clauses.append("status = 'pending'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM reviews{where} ORDER BY created_at DESC", values  # noqa: S608 - fixed clauses only
        ).fetchall()
        return [_review(row) for row in rows]

    def decide_review(self, review_id: str, decision: str, final_data: dict[str, Any]) -> dict[str, Any]:
        review = self.get_review(review_id)
        if review is None:
            raise KeyError(review_id)
        if review["status"] != "pending":
            raise ValueError("Review has already been resolved")
        if decision not in review["decisions"]:
            raise ValueError("Decision is not allowed by this workflow")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE reviews SET status = 'resolved', decision = ?, final_json = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (decision, _dump(final_data), _now(), review_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Review has already been resolved")
        return self.get_review(review_id) or {}

    def claim_effect(self, key: str, capability_id: str) -> tuple[bool, dict[str, Any] | None]:
        """Atomically claim an effect, or return its previously completed result."""
        now = _now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status, result_json FROM effects WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row:
                if row["status"] == "failed":
                    cursor = self._connection.execute(
                        "UPDATE effects SET status = 'pending', result_json = '{}', updated_at = ? "
                        "WHERE idempotency_key = ? AND status = 'failed'",
                        (_now(), key),
                    )
                    return cursor.rowcount == 1, None
                result = json.loads(row["result_json"]) if row["status"] == "complete" else None
                return False, result
            self._connection.execute(
                "INSERT INTO effects VALUES (?, ?, 'pending', '{}', ?, ?)",
                (key, capability_id, now, now),
            )
        return True, None

    def complete_effect(self, key: str, result: Any) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE effects SET status = 'complete', result_json = ?, updated_at = ? "
                "WHERE idempotency_key = ?",
                (_dump(result), _now(), key),
            )

    def fail_effect(self, key: str, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE effects SET status = 'failed', result_json = ?, updated_at = ? "
                "WHERE idempotency_key = ?",
                (_dump({"error": error}), _now(), key),
            )

    def record_event(
        self, capability_id: str, manager_id: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = {
            "id": f"event_{uuid.uuid4().hex[:16]}",
            "capabilityId": capability_id,
            "managerId": manager_id,
            "runId": run_id,
            "payload": payload,
            "createdAt": _now(),
        }
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event["id"],
                    capability_id,
                    manager_id,
                    run_id,
                    _dump(payload),
                    event["createdAt"],
                ),
            )
        return event

    @property
    def provenance(self) -> str:
        return "development:sqlite-state"


def create_state_store(path: str | Path | None = None) -> StateStore:
    if path is not None:
        return SQLiteStateStore(path)
    from . import config

    if config.DEVELOPMENT_MODE:
        return SQLiteStateStore()
    if not config.STATE_TABLE_ENDPOINT:
        raise RuntimeError(
            "STATE_TABLE_ENDPOINT is mandatory outside explicit development mode"
        )
    from .azure_state import AzureTableStateStore

    return AzureTableStateStore(config.STATE_TABLE_ENDPOINT, config.STATE_TABLE_NAME)


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workflowId": row["workflow_id"],
        "managerId": row["manager_id"],
        "subjectId": row["subject_id"],
        "triggerMode": row["trigger_mode"],
        "status": row["status"],
        "input": json.loads(row["input_json"]),
        "results": json.loads(row["results_json"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _review(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "managerId": row["manager_id"],
        "status": row["status"],
        "decisions": json.loads(row["decisions_json"]),
        "context": json.loads(row["context_json"]),
        "decision": row["decision"],
        "final": json.loads(row["final_json"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
