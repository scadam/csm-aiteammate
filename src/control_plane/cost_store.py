"""
Durable cost ledger — persists per-job inference cost points to Azure Table
Storage so the cost/token history survives container recycles and refreshes.

Why this exists: the control plane's job state (:mod:`src.control_plane.store`)
is process-memory only, so a Container Apps replica recycle wipes the cost
history. This module keeps an append-only ledger of small **cost points** (one
per finished job) in an Azure Storage **table**, written via the host's
**managed identity** (``DefaultAzureCredential``) — never a shared key, per the
tenant policy that disables shared-key access on storage accounts.

It is **best-effort**: if the table endpoint isn't configured, the SDK isn't
installed, or the identity can't reach storage, every call degrades to a silent
no-op and the dashboard falls back to the in-memory series. It never raises and
never blocks the request path (writes are fire-and-forget on a daemon thread).
"""

from __future__ import annotations

import logging
import threading
import time

from .. import config

logger = logging.getLogger(__name__)

_TABLE = "costpoints"
_lock = threading.Lock()
_client = None              # cached TableClient
_init_done = False
_unavailable: str | None = None


def enabled() -> bool:
    return bool(config.COST_STORE_TABLE_ENDPOINT)


def status() -> dict:
    return {
        "enabled": enabled(),
        "endpoint": config.COST_STORE_TABLE_ENDPOINT or None,
        "table": _TABLE,
        "ready": _client is not None,
        "unavailable": _unavailable,
    }


def _get_client():
    """Lazily build a TableClient using the managed identity (cached)."""
    global _client, _init_done, _unavailable
    if _client is not None or _init_done:
        return _client
    with _lock:
        # Re-check under the lock; a concurrent caller may have finished building.
        if _client is not None or _init_done:
            return _client
        if not config.COST_STORE_TABLE_ENDPOINT:
            _unavailable = "COST_STORE__TABLE_ENDPOINT not set"
            _init_done = True
            return None
        try:
            from azure.data.tables import TableClient
            from azure.identity import DefaultAzureCredential

            cred = DefaultAzureCredential(
                exclude_interactive_browser_credential=True,
                exclude_shared_token_cache_credential=True,
                exclude_visual_studio_code_credential=True,
            )
            client = TableClient(
                endpoint=config.COST_STORE_TABLE_ENDPOINT,
                table_name=_TABLE,
                credential=cred,
            )
            try:
                client.create_table()
            except Exception:
                pass  # already exists
            _client = client
            _unavailable = None
            logger.info("Cost ledger ready (Azure Table %s).", config.COST_STORE_TABLE_ENDPOINT)
        except Exception as exc:  # pragma: no cover - depends on env
            _unavailable = f"{type(exc).__name__}: {exc}"
            logger.info("Cost ledger unavailable (using in-memory only): %s", _unavailable)
            _client = None
        finally:
            # Set only after the build attempt so a concurrent first-caller blocks
            # on the lock and then sees the finished client (rather than a null one).
            _init_done = True
        return _client


def persist_point(point: dict) -> None:
    """Fire-and-forget durable append of one cost point. Never raises/blocks."""
    if not enabled():
        return

    def _write():
        try:
            client = _get_client()
            if client is None:
                return
            from azure.data.tables import EntityProperty, EdmType

            ts = int(point.get("t") or (time.time() * 1000))
            mgr = str(point.get("managerId") or "all")
            # RowKey sorts newest-first within a manager partition.
            row = f"{(10**16 - ts):016d}-{str(point.get('jobId') or '')[:24]}"
            entity = {
                "PartitionKey": mgr,
                "RowKey": row,
                # Epoch-ms overflows Edm.Int32 (the SDK's default for Python int),
                # so it must be stored as Int64 explicitly.
                "t": EntityProperty(ts, EdmType.INT64),
                "cost": float(point.get("cost") or 0.0),
                "tokens": int(point.get("tokens") or 0),
                "inputTokens": int(point.get("inputTokens") or 0),
                "outputTokens": int(point.get("outputTokens") or 0),
                "priced": bool(point.get("priced", True)),
                "model": str(point.get("model") or ""),
                "account": str(point.get("account") or ""),
                "managerId": mgr,
            }
            client.upsert_entity(entity)
        except Exception as exc:  # pragma: no cover - depends on live storage
            logger.warning("cost point persist failed: %s", exc)

    threading.Thread(target=_write, name="cost-persist", daemon=True).start()


def _edm_int(value) -> int:
    """Read an int that may come back wrapped in an EntityProperty (Int64)."""
    return int(getattr(value, "value", value) or 0)


def load_points(since_ms: int | None = None, limit: int = 5000) -> list[dict]:
    """Load persisted cost points (best-effort, newest-first). Empty on failure."""
    client = _get_client()
    if client is None:
        return []
    try:
        out: list[dict] = []
        for e in client.query_entities(query_filter=None, results_per_page=1000):
            t = _edm_int(e.get("t"))
            if since_ms and t < int(since_ms):
                continue
            out.append({
                "jobId": (e.get("RowKey") or "").split("-", 1)[-1],
                "t": t,
                "cost": float(e.get("cost") or 0.0),
                "tokens": _edm_int(e.get("tokens")),
                "inputTokens": _edm_int(e.get("inputTokens")),
                "outputTokens": _edm_int(e.get("outputTokens")),
                "priced": bool(e.get("priced", True)),
                "model": e.get("model") or "",
                "account": e.get("account") or "",
                "managerId": e.get("managerId") or e.get("PartitionKey") or "all",
            })
            if len(out) >= limit:
                break
        out.sort(key=lambda p: p["t"])
        return out
    except Exception as exc:  # pragma: no cover - depends on live storage
        logger.info("cost point load failed: %s", exc)
        return []
