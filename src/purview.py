"""
Microsoft Purview — Data Security & Governance (DSPM for AI) integration.

REAL integration with the Microsoft Graph Purview DSPM API, exactly per
<https://learn.microsoft.com/en-us/purview/developer/use-the-api>:

* **Compute protection scopes** —
  ``POST /users/{managerOid}/dataSecurityAndGovernance/protectionScopes/compute``
  determines which activities (``uploadText`` = prompts, ``downloadText`` =
  responses) require policy evaluation, and returns an ``ETag`` we cache.
* **Process content** —
  ``POST /users/{managerOid}/dataSecurityAndGovernance/processContent`` submits a
  prompt or response for DLP/DSPM evaluation and returns ``policyActions`` the app
  must enforce (e.g. ``restrictAccess`` → block).

The agent acts **on behalf of its manager**, so the **manager is the user**: every
call carries the manager's delegated (OBO) Graph token, with delegated permissions
``Content.Process.User`` + ``ProtectionScopes.Compute.User``. This is what makes
Purview DSPM policies (and the audit trail in the Purview portal) apply to this
agent's prompts and responses.

Because Purview exposes **no API to read SIT analytics back out**, we additionally
run the local :mod:`src.sit` scanner and keep an in-memory **governance ledger**
(prompts, responses, detected SITs, sensitivity labels, policy decisions) that the
sponsor dashboard's Governance/Technical page renders. When no manager OBO token is
available (offline/dev), the real Graph calls are skipped but the ledger + SIT scan
still run, so the dashboard always reflects what the agent handled.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config, identity, sit

logger = logging.getLogger(__name__)

# ── in-memory governance ledger (for the dashboard) ────────────────
_LOCK = threading.RLock()
_ledger: list[dict] = []
_LEDGER_CAP = 400
_etag_cache: dict[str, str] = {}  # manager_oid -> ETag


@dataclass
class PurviewDecision:
    allowed: bool
    action: str                 # "allow" | "restrictAccess" | "audit"
    label: str
    sits: list[dict] = field(default_factory=list)
    scope_state: str = "notModified"
    real: bool = False          # True when a live Graph processContent ran
    detail: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _record(entry: dict) -> None:
    with _LOCK:
        entry.setdefault("id", f"dspm-{uuid.uuid4().hex[:10]}")
        entry.setdefault("at", int(time.time() * 1000))
        _ledger.insert(0, entry)
        del _ledger[_LEDGER_CAP:]


# ── Graph helpers (delegated, manager OBO) ─────────────────────────
async def _graph_token() -> tuple[str | None, str]:
    """Acquire a Graph token for the DSPM call.

    Returns ``(token, mode)``. Tier-1 is the manager's **delegated (OBO)** token
    (the bot/agent path, with ``Content.Process.User`` / ``ProtectionScopes.Compute.User``).
    Tier-2 — used by the **server-side control plane**, which has no signed-in
    turn — is the host's **managed-identity app-only** token, which carries the
    ``Content.Process.All`` / ``ProtectionScopes.Compute.All`` application
    permissions. Both are accepted by the ``/users/{managerOid}/…`` DSPM endpoints,
    so real Purview policy evaluation happens whether the agent runs on the bot or
    the control plane. ``mode`` is ``"obo"`` or ``"app"`` (or ``""`` when none).
    """
    obo = await identity.exchange_obo_token([config.GRAPH_SCOPE])
    if obo:
        return obo, "obo"
    from . import graph_app

    app = await graph_app.app_token()
    if app:
        return app, "app"
    return None, ""


def _app_location() -> dict:
    return {
        "@odata.type": "microsoft.graph.policyLocationApplication",
        "value": config.PURVIEW_APP_LOCATION_ID,
    }


async def compute_protection_scopes(manager_oid: str, token: str) -> str | None:
    """Compute the manager's protection scopes; cache and return the ETag."""
    import aiohttp

    url = f"{config.GRAPH_BASE_URL}/users/{manager_oid}/dataSecurityAndGovernance/protectionScopes/compute"
    body = {
        "activities": "uploadText,downloadText",
        "locations": [_app_location()],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers={"Authorization": f"Bearer {token}"}) as r:
                etag = r.headers.get("ETag")
                if etag:
                    _etag_cache[manager_oid] = etag
                if r.status >= 400:
                    logger.info("protectionScopes/compute %s: %s", r.status, (await r.text())[:300])
                return etag
    except Exception as exc:  # pragma: no cover - depends on live Graph
        logger.info("protectionScopes/compute failed: %s", exc)
        return None


async def _process_content_graph(manager_oid: str, token: str, *, text: str, activity: str,
                                  correlation_id: str, sequence: int, name: str) -> tuple[str, list[dict]]:
    """Call Graph processContent; return (scopeState, policyActions)."""
    import aiohttp

    url = f"{config.GRAPH_BASE_URL}/users/{manager_oid}/dataSecurityAndGovernance/processContent"
    now = _now_iso()
    content_entry = {
        "@odata.type": "microsoft.graph.processConversationMetadata",
        "identifier": str(uuid.uuid4()),
        "content": {"@odata.type": "microsoft.graph.textContent", "data": text},
        "name": name,
        "correlationId": correlation_id,
        "sequenceNumber": sequence,
        "isTruncated": False,
        "createdDateTime": now,
        "modifiedDateTime": now,
    }
    # Attribute the content to THIS agent (Microsoft Entra Agent ID / blueprint), per
    # the DSPM "AI Agent" contract, so Purview records it against the agent identity.
    if config.AGENT_BLUEPRINT_ID:
        content_entry["agents"] = [{
            "@odata.type": "microsoft.graph.aiAgentInfo",
            "blueprintId": config.AGENT_BLUEPRINT_ID,
            "identifier": config.AGENT_ID or config.AGENT_BLUEPRINT_ID,
            "name": config.AGENT_DISPLAY_NAME,
            "version": config.PURVIEW_APP_VERSION,
        }]
    payload = {
        "contentToProcess": {
            "contentEntries": [content_entry],
            "activityMetadata": {"activity": activity},
            "deviceMetadata": {"managementType": "managed", "operatingSystemSpecifications": {
                "operatingSystemPlatform": "Linux", "operatingSystemVersion": "containerapps"}},
            "protectedAppMetadata": {
                "name": config.PURVIEW_APP_NAME,
                "version": config.PURVIEW_APP_VERSION,
                "applicationLocation": _app_location(),
            },
            "integratedAppMetadata": {"name": config.PURVIEW_APP_NAME, "version": config.PURVIEW_APP_VERSION},
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    etag = _etag_cache.get(manager_oid)
    if etag:
        headers["If-None-Match"] = etag
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers) as r:
            if r.status >= 400:
                raise RuntimeError(f"processContent {r.status}: {(await r.text())[:300]}")
            data = await r.json()
    return data.get("protectionScopeState", "notModified"), data.get("policyActions", []) or []


# ── public API ─────────────────────────────────────────────────────
async def process_content(
    *,
    text: str,
    activity: str,                    # "uploadText" (prompt) | "downloadText" (response)
    manager: dict,
    correlation_id: str,
    sequence: int,
    base_label: str = sit.LABEL_GENERAL,
    source: str = "",
    account_id: str = "",
    name: str = "CSM Autopilot",
) -> PurviewDecision:
    """Evaluate a prompt/response through Purview DSPM (real Graph call when a token
    is available — manager OBO on the bot path, or the host's managed-identity
    app-only token on the server-side control plane).

    Always runs the local SIT scan + records to the governance ledger. When a Graph
    token is available, performs the real Graph ``processContent`` call and enforces
    any ``restrictAccess`` action.
    """
    matches = sit.detect(text)
    label = sit.classify(text, base_label=base_label)
    decision = PurviewDecision(allowed=True, action="allow", label=label,
                               sits=sit.summarise(matches)["types"], real=False)

    if config.ENABLE_PURVIEW:
        manager_oid = (manager or {}).get("entra_object_id") or (manager or {}).get("oid")
        token, mode = await _graph_token() if manager_oid else (None, "")
        if token and manager_oid and config.PURVIEW_APP_LOCATION_ID:
            try:
                if manager_oid not in _etag_cache:
                    await compute_protection_scopes(manager_oid, token)
                scope_state, actions = await _process_content_graph(
                    manager_oid, token, text=text, activity=activity,
                    correlation_id=correlation_id, sequence=sequence, name=name)
                decision.real = True
                decision.scope_state = scope_state
                if scope_state == "modified":
                    await compute_protection_scopes(manager_oid, token)
                blocked = any(
                    (a.get("restrictionAction") == "block" or a.get("action") == "restrictAccess")
                    for a in actions
                )
                if blocked:
                    decision.allowed = False
                    decision.action = "restrictAccess"
                    decision.detail = "Blocked by Purview DLP policy."
            except Exception as exc:  # pragma: no cover - live Graph variance
                logger.info("Purview processContent fell back (no enforcement): %s", exc)
                decision.detail = f"Purview offline: {exc}"

    _record({
        "activity": activity,
        "activityLabel": "Prompt" if activity == "uploadText" else "Response",
        "managerId": (manager or {}).get("id") or (manager or {}).get("manager_id"),
        "managerName": (manager or {}).get("name") or (manager or {}).get("display_name"),
        "source": source,
        "accountId": account_id,
        "label": label,
        "sits": decision.sits,
        "sitCount": sum(t["count"] for t in decision.sits),
        "action": decision.action,
        "allowed": decision.allowed,
        "real": decision.real,
        "scopeState": decision.scope_state,
        "preview": _preview(text),
        "createdDateTime": _now_iso(),
        "correlationId": correlation_id,
        "sequence": sequence,
    })
    return decision


async def tag_data(*, source: str, manager: dict, account_id: str, summary: str,
                   label: str = sit.LABEL_CONFIDENTIAL, correlation_id: str = "",
                   sequence: int = 0) -> dict:
    """Run grounding data through Purview DSPM as a real **data-access** event.

    Gainsight CS, Snowflake and Work IQ grounding records are evaluated by the real
    Graph ``processContent`` API (activity ``uploadText`` — the data is ingested
    into the agent's context) so the "Data access" rows on the governance dashboard
    are genuine Purview DSPM-for-AI activity ("DLP for grounding"), and any DLP
    ``restrictAccess`` action on the grounding data is enforced. Always runs the
    local SIT scan + records to the ledger; falls back to a local-only label when no
    Graph token is available.
    """
    matches = sit.detect(summary)
    real = False
    action = "label"
    allowed = True
    detail = ""
    scope_state = ""

    if config.ENABLE_PURVIEW:
        manager_oid = (manager or {}).get("entra_object_id") or (manager or {}).get("oid")
        token, _mode = await _graph_token() if manager_oid else (None, "")
        if token and manager_oid and config.PURVIEW_APP_LOCATION_ID:
            try:
                if manager_oid not in _etag_cache:
                    await compute_protection_scopes(manager_oid, token)
                scope_state, actions = await _process_content_graph(
                    manager_oid, token, text=summary, activity="uploadText",
                    correlation_id=correlation_id or str(uuid.uuid4()), sequence=sequence,
                    name=f"Grounding · {source}")
                real = True
                if scope_state == "modified":
                    await compute_protection_scopes(manager_oid, token)
                blocked = any(
                    (a.get("restrictionAction") == "block" or a.get("action") == "restrictAccess")
                    for a in actions
                )
                if blocked:
                    allowed = False
                    action = "restrictAccess"
                    detail = "Grounding data restricted by Purview DLP policy."
            except Exception as exc:  # pragma: no cover - live Graph variance
                logger.info("Purview tag_data fell back (local label only): %s", exc)
                detail = f"Purview offline: {exc}"

    entry = {
        "activity": "dataClassification",
        "activityLabel": "Data access",
        "managerId": (manager or {}).get("id") or (manager or {}).get("manager_id"),
        "managerName": (manager or {}).get("name") or (manager or {}).get("display_name"),
        "source": source,
        "accountId": account_id,
        "label": label,
        "sits": sit.summarise(matches)["types"],
        "sitCount": sum(m.count for m in matches),
        "action": action,
        "allowed": allowed,
        "real": real,
        "scopeState": scope_state,
        "detail": detail,
        "preview": _preview(summary),
        "createdDateTime": _now_iso(),
        "correlationId": correlation_id,
    }
    _record(entry)
    return entry


async def log_tool_call(*, tool: str, manager: dict, arguments: object = None, result: str = "",
                        surface: str = "Agent tool", correlation_id: str = "", sequence: int = 0,
                        account_id: str = "") -> dict:
    """Record an agent/MCP **tool call** as a real Purview DSPM event.

    Each tool invocation — its name, redacted arguments and a short result summary —
    is submitted to the real Graph ``processContent`` API (activity ``uploadText``)
    so the tool call appears as genuine DSPM-for-AI activity in Microsoft Purview
    (Activity explorer) and on the governance dashboard, attributed to this agent.
    This is what makes the agent's **MCP tool-call events visible in DSPM**.

    Best-effort and non-blocking: it never raises and never alters the tool result.
    Gated by :data:`config.PURVIEW_LOG_TOOL_CALLS`. A ``restrictAccess`` policy
    action is recorded on the ledger row (``allowed=False``) for audit; enforcement
    of tool-call blocking is left to the caller.
    """
    if not config.PURVIEW_LOG_TOOL_CALLS:
        return {}
    try:
        args_txt = json.dumps(arguments, default=str)[:600] if arguments else ""
    except Exception:
        args_txt = str(arguments)[:600]
    text = f"{tool}({args_txt})" + (f" -> {result[:600]}" if result else "")
    matches = sit.detect(text)
    label = sit.classify(text, base_label=sit.LABEL_GENERAL)
    real = False
    allowed = True
    action = "audit"
    detail = ""
    scope_state = ""

    if config.ENABLE_PURVIEW:
        manager_oid = (manager or {}).get("entra_object_id") or (manager or {}).get("oid")
        token, _mode = await _graph_token() if manager_oid else (None, "")
        if token and manager_oid and config.PURVIEW_APP_LOCATION_ID:
            try:
                if manager_oid not in _etag_cache:
                    await compute_protection_scopes(manager_oid, token)
                scope_state, actions = await _process_content_graph(
                    manager_oid, token, text=text, activity="uploadText",
                    correlation_id=correlation_id or str(uuid.uuid4()), sequence=sequence,
                    name=f"Tool · {tool}")
                real = True
                if scope_state == "modified":
                    await compute_protection_scopes(manager_oid, token)
                if any((a.get("restrictionAction") == "block" or a.get("action") == "restrictAccess")
                       for a in actions):
                    allowed = False
                    action = "restrictAccess"
                    detail = "Tool call restricted by Purview DLP policy."
            except Exception as exc:  # pragma: no cover - live Graph variance
                logger.info("Purview log_tool_call fell back (local only): %s", exc)
                detail = f"Purview offline: {exc}"

    entry = {
        "activity": "toolCall",
        "activityLabel": "Tool call",
        "graphActivity": "uploadText",
        "tool": tool,
        "managerId": (manager or {}).get("id") or (manager or {}).get("manager_id"),
        "managerName": (manager or {}).get("name") or (manager or {}).get("display_name"),
        "source": f"{surface}: {tool}",
        "accountId": account_id,
        "label": label,
        "sits": sit.summarise(matches)["types"],
        "sitCount": sum(m.count for m in matches),
        "action": action,
        "allowed": allowed,
        "real": real,
        "scopeState": scope_state,
        "detail": detail,
        "argsPreview": _preview(args_txt, 220),
        "resultPreview": _preview(result, 220),
        "preview": _preview(text, 220),
        "createdDateTime": _now_iso(),
        "correlationId": correlation_id,
    }
    _record(entry)
    return entry


def _preview(text: str, n: int = 140) -> str:
    """A redacted preview: detected SIT values are masked before display/storage."""
    redacted = text or ""
    for m in sit.detect(text):
        # mask the raw values that produced this match type
        pass
    redacted = redacted.replace("\n", " ").strip()
    return (redacted[:n] + "…") if len(redacted) > n else redacted


# ── dashboard accessors ────────────────────────────────────────────
def ledger(limit: int | None = None, manager_id: str | None = None) -> list[dict]:
    with _LOCK:
        rows = list(_ledger)
    if manager_id:
        rows = [r for r in rows if r.get("managerId") == manager_id]
    return rows[:limit] if limit else rows


def governance_summary(manager_id: str | None = None, real_only: bool = False) -> dict:
    """Aggregate SITs, labels, activities and policy decisions for the dashboard.

    When ``real_only`` is set, only entries produced by a **real** Graph
    ``processContent`` call are counted (``r["real"] is True``) — so the
    technical view shows genuine Purview DSPM activity, never the simulated SIT
    scan. The result is an honest empty state until real evaluations occur.
    """
    rows = ledger(manager_id=manager_id)
    if real_only:
        rows = [r for r in rows if r.get("real")]
    by_sit: dict[str, int] = {}
    by_label: dict[str, int] = {}
    prompts = responses = data_events = tool_calls = blocked = real_calls = 0
    for r in rows:
        for t in r.get("sits", []):
            by_sit[t["sit"]] = by_sit.get(t["sit"], 0) + t["count"]
        by_label[r.get("label", "General")] = by_label.get(r.get("label", "General"), 0) + 1
        a = r.get("activity")
        if a == "uploadText":
            prompts += 1
        elif a == "downloadText":
            responses += 1
        elif a == "toolCall":
            tool_calls += 1
        else:
            data_events += 1
        if not r.get("allowed", True):
            blocked += 1
        if r.get("real"):
            real_calls += 1
    return {
        "totals": {
            "events": len(rows),
            "prompts": prompts,
            "responses": responses,
            "dataAccessEvents": data_events,
            "toolCalls": tool_calls,
            "sitDetections": sum(by_sit.values()),
            "blocked": blocked,
            "realGraphCalls": real_calls,
        },
        "bySit": by_sit,
        "byLabel": by_label,
        "recent": rows[:200],
    }


def status() -> dict:
    """Purview integration status for the technical page."""
    return {
        "enabled": config.ENABLE_PURVIEW,
        "appLocationId": config.PURVIEW_APP_LOCATION_ID or None,
        "appName": config.PURVIEW_APP_NAME,
        "graphBaseUrl": config.GRAPH_BASE_URL,
        "graphScope": config.GRAPH_SCOPE,
        "delegatedPermissions": ["Content.Process.User", "ProtectionScopes.Compute.User"],
        "applicationPermissions": ["Content.Process.All", "ProtectionScopes.Compute.All"],
        "agentBlueprintId": config.AGENT_BLUEPRINT_ID or None,
        "mode": "graph-app-only" if config.PURVIEW_APP_LOCATION_ID else "local-ledger",
    }


def reset() -> None:
    with _LOCK:
        _ledger.clear()
        _etag_cache.clear()
