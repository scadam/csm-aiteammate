"""
Discover the real Agent 365 footprint in Entra — blueprint + agent instances.

The technical/governance view must show the **truth** about the agent's identity
in the tenant, not invented ids. This module reads Microsoft Entra (app-only,
read-only) to report:

* the agent **blueprint** application/service principal (looked up by its
  ``appId`` = :data:`config.AGENT_BLUEPRINT_ID`), and
* any deployed **agent instances** (per-manager Agent IDs created from the
  blueprint).

There is currently **no fabricated instance id**. Instances are only reported
when a real discovery filter is configured (``A365__INSTANCE__SP_FILTER``) and
Graph returns matching service principals. Until the owner creates instances
from the demo users, the result is an **honest empty state** that names the
blueprint and lists the real CSM users as *candidates* (a business concept),
clearly separated from real Entra instances.
"""

from __future__ import annotations

import logging
import os

from . import config, data_store, graph_app

logger = logging.getLogger(__name__)

# Optional, operator-supplied OData filter to discover instance service principals
# (e.g. "startswith(displayName,'CSM Autopilot ')"). When unset, NO instances are
# invented — the view shows the honest empty state.
_INSTANCE_FILTER = os.getenv("A365__INSTANCE__SP_FILTER", "")

# Substring that identifies *our* agent's instances by their agent-user display
# name (e.g. "Siva's CSM Autopilot"). This is how a real Agent 365 instance is
# tied back to the CSM who owns it — combined with the agent user's Entra
# manager relationship. Configurable so it never hard-codes a person.
_INSTANCE_NAME_MATCH = os.getenv("A365__INSTANCE__NAME_MATCH", "CSM Autopilot")

# Cache of real CSM instances, keyed by manager_id. ``None`` means "not yet
# discovered" (e.g. Graph unreadable) — callers must treat that as "unknown" and
# fall back rather than assume zero instances.
_csm_instances: dict[str, dict] | None = None


def _norm(v: str | None) -> str:
    return (v or "").strip().lower()



async def _blueprint_sp(token: str) -> dict | None:
    app_id = config.AGENT_BLUEPRINT_ID
    if not app_id:
        return None
    data = await graph_app.graph_get(
        "/servicePrincipals", token,
        params={"$filter": f"appId eq '{app_id}'",
                "$select": "id,appId,displayName,accountEnabled,servicePrincipalType"},
    )
    vals = (data or {}).get("value") or []
    return vals[0] if vals else None


async def _discover_instances(token: str) -> list[dict]:
    """Discover real instance service principals — only when a filter is configured."""
    if not _INSTANCE_FILTER:
        return []
    data = await graph_app.graph_get(
        "/servicePrincipals", token,
        params={"$filter": _INSTANCE_FILTER,
                "$select": "id,appId,displayName,accountEnabled,servicePrincipalType,createdDateTime"},
    )
    out = []
    for sp in (data or {}).get("value") or []:
        if sp.get("appId") == config.AGENT_BLUEPRINT_ID:
            continue  # the blueprint itself, not an instance
        out.append({
            "instanceObjectId": sp.get("id"),
            "appId": sp.get("appId"),
            "displayName": sp.get("displayName"),
            "enabled": sp.get("accountEnabled"),
            "createdDateTime": sp.get("createdDateTime"),
            "source": "entra",
        })
    return out


def _candidates() -> list[dict]:
    """The real CSM users the owner can turn into instances (a business concept, not instances)."""
    out = []
    for m in data_store.table("managers"):
        out.append({
            "managerId": m.get("manager_id"),
            "name": m.get("display_name"),
            "upn": m.get("upn"),
            "entraObjectId": m.get("entra_object_id"),
        })
    return out


async def discover() -> dict:
    """Return the real Entra agent footprint, with an honest empty-instances state."""
    token = await graph_app.app_token()
    blueprint_id = config.AGENT_BLUEPRINT_ID or None

    if not token:
        return {
            "blueprintId": blueprint_id,
            "blueprint": None,
            "instances": [],
            "instanceCount": 0,
            "discovered": False,
            "candidates": _candidates(),
            "note": (
                "Directory not readable with the host identity "
                f"({graph_app.unavailable_reason() or 'no Graph token'}). "
                "Showing the blueprint id from configuration; instance discovery is unavailable."
            ),
        }

    blueprint = await _blueprint_sp(token)
    instances = await _discover_instances(token)

    # Also surface the REAL per-CSM agent-user instances (the same ones the manager
    # cockpit shows). These are discovered by agent-user display name + the Entra
    # manager relationship, so an instance appears here the moment it exists in the
    # tenant — no SP filter required.
    csm = await discover_csm_instances()
    seen_ids = {i.get("instanceObjectId") for i in instances}
    for inst in (csm or {}).values():
        if inst.get("agentUserId") in seen_ids:
            continue
        instances.append({
            "instanceObjectId": inst.get("agentUserId"),
            "appId": inst.get("instanceAppId"),
            "displayName": inst.get("displayName"),
            "enabled": inst.get("enabled"),
            "createdDateTime": None,
            "source": "entra (agent user)",
            "managerName": inst.get("managerName"),
            "agentUserUpn": inst.get("agentUserUpn"),
        })
    discovered = True

    if instances:
        note = f"{len(instances)} agent instance(s) discovered in Entra."
    elif not _INSTANCE_FILTER:
        note = (
            "No agent instances exist yet. Create one per CSM from the candidates below; "
            "set A365__INSTANCE__SP_FILTER to enable live instance discovery once created."
        )
    else:
        note = "No agent instances match the configured discovery filter yet."

    return {
        "blueprintId": blueprint_id,
        "blueprint": (
            {
                "objectId": blueprint.get("id"),
                "appId": blueprint.get("appId"),
                "displayName": blueprint.get("displayName"),
                "enabled": blueprint.get("accountEnabled"),
                "type": blueprint.get("servicePrincipalType"),
            }
            if blueprint else None
        ),
        "instances": instances,
        "instanceCount": len(instances),
        "discovered": discovered,
        "candidates": _candidates(),
        "note": note,
    }


# ── Real CSM instances (agent users ↔ manager relationship) ────────
def _managers_by_oid() -> dict[str, dict]:
    out = {}
    for m in data_store.table("managers"):
        oid = _norm(m.get("entra_object_id"))
        if oid:
            out[oid] = m
    return out


async def _agent_user_manager_oid(token: str, user_id: str) -> str | None:
    """The human (oid) the agent user reports to in Entra (its real owner/manager)."""
    data = await graph_app.graph_get(f"/users/{user_id}/manager", token,
                                      params={"$select": "id,displayName,userPrincipalName"})
    return (data or {}).get("id")


async def discover_csm_instances() -> dict[str, dict] | None:
    """Map each CSM (manager_id) to their REAL Agent 365 instance, via Graph.

    An instance is *ours* when its agent user's display name contains
    :data:`_INSTANCE_NAME_MATCH` and the agent user's Entra **manager** resolves
    to a CSM in the fixtures. Returns ``{}`` when discovery ran but found none,
    or ``None`` when the directory could not be read (caller must not treat that
    as zero).
    """
    global _csm_instances
    token = await graph_app.app_token()
    if not token:
        logger.info("CSM instance discovery skipped: %s", graph_app.unavailable_reason() or "no Graph token")
        return None

    data = await graph_app.graph_get(
        "/users", token,
        params={"$select": "id,displayName,userPrincipalName,accountEnabled,assignedLicenses", "$top": "200"},
    )
    if data is None:
        return None

    by_oid = _managers_by_oid()
    found: dict[str, dict] = {}
    for u in data.get("value") or []:
        name = u.get("displayName") or ""
        if _INSTANCE_NAME_MATCH.lower() not in name.lower():
            continue
        mgr_oid = await _agent_user_manager_oid(token, u["id"])
        manager = by_oid.get(_norm(mgr_oid))
        if not manager:
            continue  # an instance whose manager isn't one of our CSMs — ignore
        found[manager["manager_id"]] = {
            "managerId": manager["manager_id"],
            "managerName": manager.get("display_name"),
            "agentUserId": u.get("id"),
            "agentUserUpn": u.get("userPrincipalName"),
            "displayName": name,
            "enabled": u.get("accountEnabled"),
            "licensed": bool(u.get("assignedLicenses")),
            "source": "entra",
        }
    _csm_instances = found
    logger.info("Discovered %d real CSM instance(s): %s", len(found), list(found))
    return found


async def warm_csm_instances() -> None:
    """Best-effort population of the CSM-instance cache (call on startup)."""
    try:
        await discover_csm_instances()
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("warm_csm_instances failed: %s", exc)


def cached_csm_instances() -> dict[str, dict] | None:
    """Sync read of discovered CSM instances (None = not discovered yet)."""
    return _csm_instances


def active_manager_ids() -> set[str] | None:
    """Manager ids that have a REAL instance, or ``None`` if discovery hasn't run.

    ``None`` tells callers to fall back to the full fixture set (offline/dev/tests)
    rather than hide everything.
    """
    if _csm_instances is None:
        return None
    return set(_csm_instances.keys())


# ── Entra ID Governance: the agents' access package (real, read-only) ──
async def access_package_status() -> dict:
    """Read the REAL Entra access package governing the agents + its live assignments.

    Reads (app-only, read-only via the host managed identity which holds
    ``EntitlementManagement.Read.All``) the access package named
    :data:`config.ACCESS_PACKAGE_NAME` and the per-agent assignment state. This is
    the honest Agent 365 governance signal shown on the Technical tab — when the
    package isn't configured/created yet, it returns ``configured: False`` rather
    than inventing anything.
    """
    name = config.ACCESS_PACKAGE_NAME
    result: dict = {
        "configured": bool(name),
        "name": name or None,
        "catalog": config.ACCESS_PACKAGE_CATALOG or None,
        "groupName": config.ACCESS_PACKAGE_GROUP or None,
        "exists": False,
        "assignments": [],
        "assignmentsByUserId": {},
        "note": "",
    }
    if not name:
        result["note"] = "No access package configured (set A365__ACCESS_PACKAGE__NAME)."
        return result

    token = await graph_app.app_token()
    if not token:
        result["note"] = (f"Directory not readable with the host identity "
                          f"({graph_app.unavailable_reason() or 'no Graph token'}).")
        return result

    base = "/identityGovernance/entitlementManagement"
    try:
        safe = name.replace("'", "''")
        pkgs = await graph_app.graph_get(
            f"{base}/accessPackages", token,
            params={"$filter": f"displayName eq '{safe}'", "$select": "id,displayName,description"})
        vals = (pkgs or {}).get("value") or []
        if not vals:
            result["note"] = f"Access package '{name}' is not created in this tenant yet."
            return result
        pkg = vals[0]
        result["exists"] = True
        result["id"] = pkg.get("id")
        result["description"] = pkg.get("description")

        asg = await graph_app.graph_get(
            f"{base}/assignments", token,
            params={"$filter": f"accessPackage/id eq '{pkg.get('id')}'",
                    "$expand": "target", "$select": "id,state,status,schedule"})
        out = []
        by_user: dict[str, dict] = {}
        for a in (asg or {}).get("value") or []:
            tgt = a.get("target") or {}
            exp = ((a.get("schedule") or {}).get("expiration") or {}).get("endDateTime")
            row = {
                "assignmentId": a.get("id"),
                "state": a.get("state"),
                "status": a.get("status"),
                "targetName": tgt.get("displayName"),
                "targetUpn": tgt.get("email") or tgt.get("principalName"),
                "targetObjectId": tgt.get("objectId"),
                "expires": exp,
            }
            out.append(row)
            if tgt.get("objectId"):
                by_user[tgt["objectId"]] = row
        result["assignments"] = out
        result["assignmentsByUserId"] = by_user
        delivered = sum(1 for r in out if str(r.get("state", "")).lower() == "delivered")
        result["deliveredCount"] = delivered
        result["note"] = (f"Access package live: {delivered} of {len(out)} agent assignment(s) delivered."
                          if out else "Access package exists; no agent assignments yet.")
    except Exception as exc:  # pragma: no cover - depends on live Graph
        logger.info("access_package_status read failed: %s", exc)
        result["note"] = f"Access package read unavailable: {exc}"
    return result


def reset() -> None:
    global _csm_instances
    _csm_instances = None

