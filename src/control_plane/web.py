"""
aiohttp web layer for the CSM Autopilot control plane.

Serves **two** distinct, real-time business surfaces:

* **Manager cockpit** (``/manager``) — what a single human CSM sees: *their own*
  CSM Autopilot, *their* accounts and products, *their* HITL review queue.
  Product- and customer-focused. "Managing a team of one." Scoped to the
  signed-in / requested manager.

* **Sponsor dashboard** (``/sponsor``) — what the programme owner sees across the
  whole fleet: every CSM and their autopilot, cost and performance, per-agent
  HITL queue length, response times. "Responsible for a team of CSMs, each with
  their own CSM Autopilot."

Both poll scoped JSON APIs and stream live job journeys over Server-Sent Events.
None of these routes import the (Windows-only) Copilot SDK, so the control plane
runs unchanged in the Linux container.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from aiohttp import web

from .. import config, data_store, directory, email_render, identity, memory, observability, scenarios, skills
from . import auth, engine, store

logger = logging.getLogger(__name__)

_STATIC = Path(__file__).resolve().parent / "static"

# Hosts allowed to embed the dashboards in an iframe. This is what lets the tab
# load cleanly inside Microsoft Teams / Outlook / the Microsoft 365 app (and in a
# plain browser) without a "can't be displayed / may have issues" frame error.
_FRAME_ANCESTORS = " ".join([
    "'self'",
    "https://teams.microsoft.com", "https://*.teams.microsoft.com",
    "https://teams.cloud.microsoft", "https://*.teams.cloud.microsoft",
    "https://*.cloud.microsoft",
    "https://outlook.office.com", "https://outlook.office365.com", "https://*.office.com",
    "https://*.microsoft365.com", "https://microsoft365.com",
    "https://*.microsoft.com", "https://microsoft.com",
    "https://*.sharepoint.com",
])

# Tab page routes whose content must never be served stale (forces revalidation).
_NO_CACHE_PATHS = {
    "/app", "/manager", "/sponsor", "/technical", "/signin",
    "/control-plane", "/sim/in-product", "/sim/email",
}


@web.middleware
async def embed_headers_middleware(request: web.Request, handler):
    """Allow the dashboards to be framed by Teams/M365 hosts; never block framing.

    Sets a permissive ``frame-ancestors`` Content-Security-Policy and removes any
    ``X-Frame-Options`` so the tab renders inside the Teams web client instead of
    showing the "this app may have issues / open in desktop" fallback.
    """
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        exc.headers["Content-Security-Policy"] = f"frame-ancestors {_FRAME_ANCESTORS};"
        exc.headers.pop("X-Frame-Options", None)
        raise
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {_FRAME_ANCESTORS};"
    resp.headers.pop("X-Frame-Options", None)

    # Never let Teams / the browser serve a stale dashboard. The tab pages and
    # their JS change on every deploy, so force a revalidation on each load
    # instead of serving the cached copy (which is why "refresh shows the old
    # app"). API responses are dynamic anyway; only static media may be cached.
    path = request.path
    ctype = (resp.headers.get("Content-Type") or "").lower()
    is_page = path in _NO_CACHE_PATHS or path == "/" or path.startswith("/static/")
    is_doc = "text/html" in ctype or "javascript" in ctype or "/json" in ctype
    if is_page or is_doc:
        if path.startswith("/static/") and not (path.endswith(".js") or path.endswith(".css")):
            pass  # images/fonts can be cached
        else:
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            resp.headers.pop("Last-Modified", None)
            resp.headers.pop("ETag", None)
    return resp


# ── identity-driven scoping ────────────────────────────────────────
def _default_manager_id() -> str:
    managers = data_store.table("managers")
    ids = {m.get("manager_id") for m in managers}
    if config.AGENT_MANAGER_USER_ID in ids:
        return config.AGENT_MANAGER_USER_ID
    return managers[0]["manager_id"] if managers else config.AGENT_MANAGER_USER_ID


def _acting(req: web.Request) -> identity.UserPrincipal:
    """Who is making this request (Teams SSO → session → default)."""
    return auth.resolve_acting_user(req)


def _scope_manager_id(req: web.Request, user: identity.UserPrincipal | None = None) -> str:
    """The CSM whose data this request may see.

    A CSM is locked to their own autopilot. The programme **owner** may pass
    ``?manager=<id>`` to view any CSM ("view as"); with no override the owner
    defaults to their own CSM record (svasireddy is both) or the first CSM.
    """
    user = user or _acting(req)
    requested = req.query.get("manager") or req.match_info.get("manager_id")
    if user.is_owner and requested and data_store.get("managers", "manager_id", requested):
        return requested
    if user.manager_id:
        return user.manager_id
    if user.is_owner:
        return _default_manager_id()
    return _default_manager_id()


# ── UI ─────────────────────────────────────────────────────────────
async def handle_root(_req: web.Request) -> web.Response:
    raise web.HTTPFound("/app")


async def handle_app_ui(_req: web.Request) -> web.Response:
    return web.FileResponse(_STATIC / "app.html")


async def handle_signin_ui(_req: web.Request) -> web.Response:
    return web.FileResponse(_STATIC / "signin.html")


async def handle_manager_ui(_req: web.Request) -> web.Response:
    return web.FileResponse(_STATIC / "manager.html")


async def handle_sponsor_ui(_req: web.Request) -> web.Response:
    # Owner-only content is gated inside the page (inline message) so the tab never
    # has to navigate to another tab — which is unreliable inside Teams.
    return web.FileResponse(_STATIC / "sponsor.html")


async def handle_technical_ui(_req: web.Request) -> web.Response:
    # Owner-only content is gated inside the page (inline message), same as sponsor.
    return web.FileResponse(_STATIC / "technical.html")


async def handle_control_plane(_req: web.Request) -> web.Response:
    # Back-compat alias used by the Teams bot manifest.
    raise web.HTTPFound("/app")


async def handle_in_product(_req: web.Request) -> web.Response:
    return web.FileResponse(_STATIC / "in-product.html")


async def handle_email_ui(_req: web.Request) -> web.Response:
    """Simulated email client — shows the branded HTML email as the customer sees it."""
    return web.FileResponse(_STATIC / "email.html")


async def handle_email_preview(req: web.Request) -> web.Response:
    """Render a delivered/queued outcome as the real branded HTML email.

    Used by the email simulator (and any "view email" link) so the preview is the
    *exact* HTML the customer receives — rendered by the same
    :func:`src.email_render.render_email_html` used at send time.
    """
    outcome_id = req.query.get("o") or req.match_info.get("outcome_id", "")
    outcome = store.get_outcome(outcome_id) if outcome_id else None
    if outcome is None:
        # Fall back to the most recent email-style outcome so the simulator isn't blank.
        outcome = next((o for o in store.outcomes() if o.get("channel") in ("email", "csm_review", "queued_agent")), None)
    if outcome is None:
        return web.Response(text="<p style='font-family:Segoe UI,Arial;padding:40px;color:#6B7A8D'>"
                                 "No email delivered yet.</p>", content_type="text/html")
    account = data_store.resolve_account(outcome.get("account_id", "")) or {}
    manager = data_store.get("managers", "manager_id", outcome.get("manager_id", "")) or {}
    html = email_render.render_email_html(
        subject=outcome.get("subject") or outcome.get("messageType") or "A note from your Customer Success team",
        body_text=outcome.get("body", ""),
        manager_name=manager.get("display_name") or outcome.get("manager") or "Your Customer Success Manager",
        manager_role=manager.get("role") or "Customer Success Manager",
        manager_email=manager.get("upn") or "customer.success@example.com",
        recipient_name=account.get("primary_contact") or outcome.get("recipient", ""),
        account_name=account.get("account_name") or outcome.get("account_name", ""),
    )
    return web.Response(text=html, content_type="text/html")


async def handle_health(_req: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": config.SERVICE_NAME})


# ── who am I (Teams SSO / session) ─────────────────────────────────
async def handle_me(req: web.Request) -> web.Response:
    """Resolve the signed-in user and what they can see.

    The page POSTs the Teams SSO token (``{token}``) here, or GETs with the
    session cookie. Returns the resolved principal, roles, the CSM scope, and
    (for owners) the list of CSMs they can view.
    """
    body_token = None
    if req.method == "POST" and req.can_read_body:
        try:
            body_token = (await req.json()).get("token")
        except Exception:
            body_token = None
    user = auth.resolve_acting_user(req, body_token=body_token)
    managers = [
        {"manager_id": m["manager_id"], "name": m.get("display_name"),
         "role": directory.cached_role_region(m.get("entra_object_id")).get("role"),
         "region": directory.cached_role_region(m.get("entra_object_id")).get("region")}
        for m in data_store.table("managers")
    ] if user.is_owner else []
    return web.json_response({
        "user": user.to_public(),
        "scopeManagerId": _scope_manager_id(req, user),
        "defaultView": "sponsor" if user.is_owner else "manager",
        "managers": managers,
        "auth": auth.auth_status(),
    })


async def handle_signin(req: web.Request) -> web.Response:
    """Simulated sign-in (browser fallback): start a session as a real tenant user."""
    body = await req.json() if req.can_read_body else {}
    manager_id = body.get("manager_id")
    oid = body.get("oid")
    upn = body.get("upn")
    if manager_id and not data_store.get("managers", "manager_id", manager_id):
        return web.json_response({"error": "unknown manager"}, status=400)
    if manager_id:
        m = data_store.get("managers", "manager_id", manager_id) or {}
        oid, upn = m.get("entra_object_id"), m.get("upn")
    user = identity.resolve_user(object_id=oid, upn=upn, source="session")
    resp = web.json_response({"ok": True, "user": user.to_public()})
    auth.set_session(resp, manager_id=user.manager_id, object_id=oid, upn=upn)
    return resp


async def handle_signout(_req: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    auth.clear_session(resp)
    return resp


async def handle_autopilot_memory(req: web.Request) -> web.Response:
    """Return the working memory of one CSM Autopilot (owner-only; technical view)."""
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    manager_id = req.match_info.get("manager_id", "")
    mgr = data_store.get("managers", "manager_id", manager_id)
    if not mgr:
        return web.json_response({"error": "unknown manager"}, status=404)
    text = memory.load(manager_id, mgr.get("display_name", ""))
    return web.json_response({
        "managerId": manager_id,
        "managerName": mgr.get("display_name"),
        "stats": memory.stats(manager_id),
        "markdown": text,
    })


async def handle_signin_options(_req: web.Request) -> web.Response:
    """Demo sign-in picker options: every real tenant user (CSMs + owner), de-duplicated.

    A person who is both a CSM and the owner (e.g. svasireddy) appears once, with
    a ``manager_id`` so signing in resolves both roles.
    """
    by_oid: dict[str, dict] = {}
    order: list[str] = []

    def _key(u: dict) -> str:
        return (u.get("entra_object_id") or u.get("upn") or u.get("manager_id") or u.get("owner_id") or "").lower()

    # Managers first (so the manager_id wins for a dual-role person).
    for m in data_store.table("managers"):
        k = _key(m)
        rr = directory.cached_role_region(m.get("entra_object_id"))
        by_oid[k] = {
            "name": m.get("display_name"), "role": rr.get("role"), "upn": m.get("upn"),
            "oid": m.get("entra_object_id"), "manager_id": m.get("manager_id"),
            "isOwner": False, "region": rr.get("region"),
        }
        order.append(k)
    for o in data_store.table("owners"):
        k = _key(o)
        if k in by_oid:
            by_oid[k]["isOwner"] = True  # dual role — mark existing CSM as owner too
        else:
            orr = directory.cached_role_region(o.get("entra_object_id"))
            by_oid[k] = {
                "name": o.get("display_name"), "role": orr.get("role"), "upn": o.get("upn"),
                "oid": o.get("entra_object_id"), "manager_id": None, "isOwner": True,
                "region": orr.get("region"),
            }
            order.append(k)
    users = [by_oid[k] for k in order]
    return web.json_response({"users": users})


# ── managers directory (owner-only switcher) ───────────────────────
async def handle_managers(req: web.Request) -> web.Response:
    user = _acting(req)
    if not user.is_owner:
        return web.json_response({"error": "forbidden"}, status=403)
    out = []
    for m in data_store.table("managers"):
        ap = store.autopilot_for_manager(m["manager_id"]) or {}
        rr = directory.cached_role_region(m.get("entra_object_id"))
        out.append({
            "manager_id": m["manager_id"],
            "name": m.get("display_name"),
            "role": rr.get("role"),
            "region": rr.get("region"),
            "accountCount": ap.get("accountCount", 0),
            "status": ap.get("status", "idle"),
        })
    return web.json_response({"managers": out, "default": _scope_manager_id(req, user)})


# ── manager-scoped API ─────────────────────────────────────────────
async def handle_manager_summary(req: web.Request) -> web.Response:
    mid = _scope_manager_id(req)
    ap = store.autopilot_for_manager(mid)
    if not ap:
        return web.json_response({"error": f"no autopilot for {mid}"}, status=404)
    return web.json_response({
        "manager": ap["manager"],
        "owner": ap.get("owner"),
        "autopilot": ap,
        "metrics": store.metrics(mid),
        "reviewQueue": store.review_queue(mid),
        "outcomes": store.outcomes(limit=25, manager_id=mid),
        "jobs": store.jobs(limit=25, manager_id=mid),
    })


async def handle_manager_metrics(req: web.Request) -> web.Response:
    return web.json_response(store.metrics(_scope_manager_id(req)))


async def handle_manager_review_queue(req: web.Request) -> web.Response:
    return web.json_response(store.review_queue(_scope_manager_id(req)))


async def handle_manager_jobs(req: web.Request) -> web.Response:
    return web.json_response(store.jobs(manager_id=_scope_manager_id(req)))


async def handle_manager_outcomes(req: web.Request) -> web.Response:
    return web.json_response(store.outcomes(manager_id=_scope_manager_id(req)))


# ── sponsor-scoped API (owner only) ────────────────────────────────
async def handle_sponsor_overview(req: web.Request) -> web.Response:
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    return web.json_response(store.sponsor_overview())


async def handle_sponsor_metrics(req: web.Request) -> web.Response:
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    return web.json_response(store.metrics())


async def handle_sponsor_technical(req: web.Request) -> web.Response:
    """Technical / governance view: identities, OTEL endpoint, Purview + SITs (owner only)."""
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    from .. import purview, agent_instances, directory

    # Real Entra footprint — blueprint + any instances (honest empty state, no fabrication).
    footprint = await agent_instances.discover()

    # Logical CSM autopilots (business concept) enriched with REAL Entra role/region.
    access_package = await agent_instances.access_package_status()
    ap_by_user = access_package.get("assignmentsByUserId", {}) or {}
    autopilots = []
    for ap in store.fleet():
        m = ap.get("manager", {})
        rr = await directory.role_and_region(m.get("entra_object_id"))
        inst = ap.get("realInstance") or {}
        agent_user_id = inst.get("agentUserId")
        assignment = ap_by_user.get(agent_user_id) if agent_user_id else None
        autopilots.append({
            "autopilotId": ap["id"],
            "manager": {"id": m.get("id"), "name": m.get("name"), "upn": m.get("upn"),
                        "entraObjectId": m.get("entra_object_id"),
                        "role": rr.get("role"), "region": rr.get("region"),
                        "attributeSource": rr.get("source")},
            "accounts": ap.get("accountCount", 0),
            "status": ap.get("status"),
            "memory": memory.stats(m.get("id")) if m.get("id") else None,
            "instance": ({
                "displayName": inst.get("displayName"),
                "agentUserId": inst.get("agentUserId"),
                "agentUserUpn": inst.get("agentUserUpn"),
                "enabled": inst.get("enabled"),
                "licensed": inst.get("licensed"),
                "source": inst.get("source"),
            } if inst else None),
            "accessPackage": ({
                "state": assignment.get("state"),
                "status": assignment.get("status"),
                "expires": assignment.get("expires"),
            } if assignment else None),
        })

    obs = observability.observability_status() if hasattr(observability, "observability_status") else {}
    governance = purview.governance_summary(real_only=True)
    return web.json_response({
        "agentIdentity": {
            "displayName": config.AGENT_DISPLAY_NAME,
            "blueprintId": config.AGENT_BLUEPRINT_ID or None,
            "agentId": config.AGENT_ID or None,
            "tenantId": config.AGENT_TENANT_ID or None,
        },
        "footprint": footprint,
        "instances": footprint.get("instances", []),
        "autopilots": autopilots,
        "observability": {
            "serviceName": config.SERVICE_NAME,
            "otelEndpoint": config.OTEL_EXPORTER_OTLP_ENDPOINT or None,
            "a365Enabled": config.ENABLE_A365_OBSERVABILITY,
            "a365ExporterEnabled": config.ENABLE_A365_OBSERVABILITY_EXPORTER,
            "clusterCategory": getattr(config, "A365_CLUSTER_CATEGORY", None),
            **(obs or {}),
        },
        "purview": purview.status(),
        "governance": governance,
        "accessPackage": access_package,
        "skills": [
            {"name": s.name, "description": s.description, "path": s.path,
             "allowedTools": getattr(s, "allowed_tools", []) or []}
            for s in skills.list_skills()
        ],
        "generatedAt": store.now_ms(),
    })


async def handle_security_scenarios(req: web.Request) -> web.Response:
    """The AI security scenarios catalogue (risk → control) + live posture (owner only)."""
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    return web.json_response({
        "scenarios": [s.public() for s in scenarios.list_scenarios()],
        "status": scenarios.status(),
        "generatedAt": store.now_ms(),
    })


async def handle_security_simulate(req: web.Request) -> web.Response:
    """Run one security scenario against a safe synthetic attack and report (owner only)."""
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    scenario_id = req.match_info.get("scenario_id", "")
    mid = _scope_manager_id(req)
    ap = store.autopilot_for_manager(mid)
    manager = (ap or {}).get("manager", {}) or {"id": mid}
    try:
        result = await scenarios.simulate(scenario_id, manager)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("security scenario simulate failed")
        return web.json_response({"error": f"simulation failed: {exc}"}, status=500)
    status = 404 if result.get("error") else 200
    return web.json_response(result, status=status)


# ── generic API ────────────────────────────────────────────────────
async def handle_metrics(_req: web.Request) -> web.Response:
    return web.json_response(store.metrics())


async def handle_fleet(_req: web.Request) -> web.Response:
    return web.json_response(store.fleet())


async def handle_jobs(_req: web.Request) -> web.Response:
    return web.json_response(store.jobs())


async def handle_job(req: web.Request) -> web.Response:
    job = store.get_job(req.match_info["job_id"])
    if not job:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(job)


async def handle_outcomes(_req: web.Request) -> web.Response:
    return web.json_response(store.outcomes())


async def handle_review_queue(_req: web.Request) -> web.Response:
    return web.json_response(store.review_queue())


async def handle_review_decision(req: web.Request) -> web.Response:
    outcome_id = req.match_info["outcome_id"]
    body = await req.json()
    decision = body.get("decision", "")
    if decision not in ("accept", "edit", "discard", "draft"):
        return web.json_response({"error": "decision must be accept|edit|discard|draft"}, status=400)
    updated = await _apply_review_decision(outcome_id, decision, body.get("finalText", ""))
    if updated is None:
        return web.json_response({"error": "review item not found"}, status=404)
    return web.json_response(updated)


async def _apply_review_decision(outcome_id: str, decision: str, final_text: str = "") -> dict | None:
    """Apply one review decision and perform the real side effect (send / draft).

    accept/edit → send the branded email as the manager; draft → create a real
    Outlook draft in the manager's mailbox; discard → just record it. The honest
    result is written back so the cockpit never shows a fabricated state. Any
    decision removes the item from the review queue.
    """
    updated = store.decide_review(outcome_id, decision, final_text)
    if updated is None:
        return None
    from .. import mail

    if decision in ("accept", "edit") and updated.get("channel") in ("csm_review", "email", "queued_agent", None):
        result = await mail.deliver_email(
            account_id=updated.get("account_id", ""),
            subject=updated.get("subject") or updated.get("messageType") or "Outreach",
            body=updated.get("body", ""),
            manager_id=updated.get("manager_id"),
        )
        store.mark_outcome_sent(outcome_id, result)
        updated["deliveryDetail"] = result.get("detail")
        updated["delivered"] = bool(result.get("sent"))
        updated["status"] = "delivered" if result.get("sent") else "send_failed"
    elif decision == "draft":
        result = await mail.save_draft(
            account_id=updated.get("account_id", ""),
            subject=updated.get("subject") or updated.get("messageType") or "Outreach",
            body=updated.get("body", ""),
            manager_id=updated.get("manager_id"),
        )
        store.mark_outcome_drafted(outcome_id, result)
        updated["deliveryDetail"] = result.get("detail")
        updated["status"] = "drafted" if result.get("saved") else "draft_failed"
    return updated


async def handle_review_bulk(req: web.Request) -> web.Response:
    """Apply one decision to many review items at once (the filtered list).

    Body: ``{"decision": "accept|draft|discard", "ids": [outcome_id, ...]}``.
    Returns a per-item summary so the UI can report how many sent / drafted / failed.
    """
    body = await req.json()
    decision = body.get("decision", "")
    ids = body.get("ids") or []
    if decision not in ("accept", "draft", "discard"):
        return web.json_response({"error": "decision must be accept|draft|discard"}, status=400)
    if not isinstance(ids, list) or not ids:
        return web.json_response({"error": "ids must be a non-empty list"}, status=400)

    results = []
    ok = 0
    for outcome_id in ids:
        updated = await _apply_review_decision(outcome_id, decision, "")
        if updated is None:
            results.append({"id": outcome_id, "status": "not_found"})
            continue
        status = updated.get("status", "")
        if status in ("delivered", "drafted", "discarded"):
            ok += 1
        results.append({"id": outcome_id, "status": status})
    return web.json_response({"decision": decision, "requested": len(ids), "succeeded": ok, "results": results})



async def handle_evidence(req: web.Request) -> web.Response:
    """Produce an OTEL-style evidence pack for a single job (forensic download)."""
    job_id = req.match_info["job_id"]
    job = store.get_job(job_id)
    if not job:
        return web.json_response({"error": f"unknown job {job_id}"}, status=404)
    evidence = {
        "schema": "csm-autopilot-job-evidence/1.0",
        "generatedAt": store.now_ms(),
        "job": job,
        "agentIdentity": {
            "displayName": config.AGENT_DISPLAY_NAME,
            "blueprintId": config.AGENT_BLUEPRINT_ID,
            "tenantId": config.AGENT_TENANT_ID,
            "manager": job.get("manager", {}),
        },
        "costModel": {
            "basis": "Azure OpenAI token pricing ($/1M tokens)",
            "model": job["stats"].get("model"),
            "inputTokens": job["stats"].get("prompt_tokens", 0),
            "outputTokens": job["stats"].get("completion_tokens", 0),
            "totalTokens": job["stats"].get("total_tokens", 0),
            "inputPricePerM": job["stats"].get("input_price_per_m"),
            "outputPricePerM": job["stats"].get("output_price_per_m"),
            "costUsd": job["stats"].get("cost_usd"),
            "priced": job["stats"].get("priced", True),
        },
        "observability": observability.observability_status() if hasattr(observability, "observability_status") else {
            "enabled": config.ENABLE_A365_OBSERVABILITY,
            "exporterEnabled": config.ENABLE_A365_OBSERVABILITY_EXPORTER,
            "endpoint": config.OTEL_EXPORTER_OTLP_ENDPOINT or None,
            "serviceName": config.SERVICE_NAME,
        },
        "journey": job.get("stages", []),
        "toolCalls": job.get("toolData", {}),
    }
    resp = web.json_response(evidence)
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", job_id)
    resp.headers["Content-Disposition"] = f"attachment; filename=csm-autopilot-evidence-{safe}.json"
    return resp


# ── triggers ───────────────────────────────────────────────────────
async def _stream_job(req: web.Request, autopilot: dict, account: dict, trigger: str) -> web.StreamResponse:
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)
    try:
        async for event, data in engine.run_job(autopilot, account, source="manager-cockpit", trigger=trigger):
            await resp.write(f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode())
    except ConnectionResetError:  # client navigated away
        logger.info("SSE client disconnected")
    finally:
        observability.force_flush() if hasattr(observability, "force_flush") else None
    return resp


async def handle_manager_start(req: web.Request) -> web.StreamResponse:
    """Start the manager's autopilot on its top-priority account; stream the journey."""
    mid = _scope_manager_id(req)
    autopilot = store.autopilot_for_manager(mid)
    if not autopilot:
        return web.json_response({"error": f"no autopilot for {mid}"}, status=404)

    body = {}
    if req.can_read_body:
        try:
            body = await req.json()
        except Exception:
            body = {}
    account_id = body.get("account_id")
    account = data_store.get("accounts", "account_id", account_id) if account_id else engine.pick_top_account(autopilot)
    if not account:
        return web.json_response({"error": "no account with an actionable signal"}, status=409)
    return await _stream_job(req, autopilot, account, body.get("trigger", "manual_start"))


async def handle_manager_sweep(req: web.Request) -> web.Response:
    """Work every account of this manager that has an open signal (background)."""
    mid = _scope_manager_id(req)
    autopilot = store.autopilot_for_manager(mid)
    if not autopilot:
        return web.json_response({"error": f"no autopilot for {mid}"}, status=404)
    started = []
    for account in engine.accounts_with_signals(autopilot):
        _spawn_job(req.app, autopilot, account, "manager_sweep")
        started.append(account["account_id"])
    return web.json_response({"status": "started", "accounts": started, "count": len(started)})


async def handle_fleet_start(req: web.Request) -> web.Response:
    """Kick off a sweep across every CSM Autopilot (programme-wide; owner only)."""
    if not _acting(req).is_owner:
        return web.json_response({"error": "forbidden — owner only"}, status=403)
    body = {}
    if req.can_read_body:
        try:
            body = await req.json()
        except Exception:
            body = {}
    trigger = body.get("trigger", "fleet_sweep")
    started = []
    for autopilot in store.fleet():
        for account in engine.accounts_with_signals(autopilot):
            _spawn_job(req.app, autopilot, account, trigger)
            started.append(account["account_id"])
    return web.json_response({"status": "started", "accounts": started, "count": len(started)})


def _spawn_job(app: web.Application, autopilot: dict, account: dict, trigger: str) -> None:
    """Run a job to completion in the background (drains the engine generator)."""
    async def _drain() -> None:
        try:
            async for _event, _data in engine.run_job(autopilot, account, source="fleet-sweep", trigger=trigger):
                pass
        except Exception:  # pragma: no cover
            logger.exception("Background job failed for %s / %s", autopilot["id"], account.get("account_id"))

    task = app.loop.create_task(_drain())
    app.setdefault("_cp_tasks", set()).add(task)
    task.add_done_callback(lambda t: app["_cp_tasks"].discard(t))


# ── mount ──────────────────────────────────────────────────────────
def attach_control_plane(app: web.Application) -> None:
    """Register all control-plane routes on an existing aiohttp application."""
    # Make the dashboards embeddable in Teams/M365 (and a plain browser tab).
    if embed_headers_middleware not in app.middlewares:
        app.middlewares.append(embed_headers_middleware)
    # UI
    app.router.add_get("/", handle_root)
    app.router.add_get("/app", handle_app_ui)
    app.router.add_get("/signin", handle_signin_ui)
    app.router.add_get("/manager", handle_manager_ui)
    app.router.add_get("/sponsor", handle_sponsor_ui)
    app.router.add_get("/technical", handle_technical_ui)
    app.router.add_get("/control-plane", handle_control_plane)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/sim/in-product", handle_in_product)
    app.router.add_get("/sim/email", handle_email_ui)

    # identity (Teams SSO / simulated session)
    app.router.add_get("/api/me", handle_me)
    app.router.add_post("/api/me", handle_me)
    app.router.add_post("/api/signin", handle_signin)
    app.router.add_get("/api/signin/options", handle_signin_options)
    app.router.add_post("/api/signout", handle_signout)

    # directory (owner-only switcher)
    app.router.add_get("/api/managers", handle_managers)

    # manager-scoped
    app.router.add_get("/api/manager/summary", handle_manager_summary)
    app.router.add_get("/api/manager/metrics", handle_manager_metrics)
    app.router.add_get("/api/manager/review-queue", handle_manager_review_queue)
    app.router.add_get("/api/manager/jobs", handle_manager_jobs)
    app.router.add_get("/api/manager/outcomes", handle_manager_outcomes)
    app.router.add_post("/api/manager/start", handle_manager_start)
    app.router.add_post("/api/manager/sweep", handle_manager_sweep)

    # sponsor-scoped
    app.router.add_get("/api/sponsor", handle_sponsor_overview)
    app.router.add_get("/api/sponsor/metrics", handle_sponsor_metrics)
    app.router.add_get("/api/sponsor/technical", handle_sponsor_technical)
    app.router.add_get("/api/autopilot/{manager_id}/memory", handle_autopilot_memory)

    # security scenarios (risk -> control), owner only
    app.router.add_get("/api/security/scenarios", handle_security_scenarios)
    app.router.add_post("/api/security/scenarios/{scenario_id}/simulate", handle_security_simulate)

    # generic
    app.router.add_get("/api/metrics", handle_metrics)
    app.router.add_get("/api/fleet", handle_fleet)
    app.router.add_get("/api/jobs", handle_jobs)
    app.router.add_get("/api/jobs/{job_id}", handle_job)
    app.router.add_get("/api/jobs/{job_id}/evidence", handle_evidence)
    app.router.add_get("/api/outcomes", handle_outcomes)
    app.router.add_get("/api/email/preview", handle_email_preview)
    app.router.add_get("/api/review-queue", handle_review_queue)
    app.router.add_post("/api/review/bulk", handle_review_bulk)
    app.router.add_post("/api/review/{outcome_id}", handle_review_decision)
    app.router.add_post("/api/fleet/start", handle_fleet_start)

    if _STATIC.exists():
        app.router.add_static("/static/", path=str(_STATIC), name="cp_static")

    # Best-effort: warm the Entra directory cache (role/region) so the dashboards
    # show real titles/offices. If Graph is unreadable this is a silent no-op and
    # the UI falls back to an honest em dash — never a fabricated value.
    async def _warm_directory(_app: web.Application) -> None:
        try:
            oids = [m.get("entra_object_id") for m in data_store.table("managers")]
            oids += [o.get("entra_object_id") for o in data_store.table("owners")]
            await directory.warm(oids)
        except Exception:  # pragma: no cover - best effort
            logger.debug("directory warm skipped")

    # Load the durable cost ledger so the cost/token chart survives recycles.
    async def _warm_cost(_app: web.Application) -> None:
        try:
            await asyncio.to_thread(store.warm_cost_points)
        except Exception:  # pragma: no cover - best effort
            logger.debug("cost ledger warm skipped")

    # Best-effort: discover the REAL Agent 365 instances (agent users ↔ manager)
    # so the fleet/sponsor views reflect instances that actually exist, refreshed
    # periodically. If Graph is unreadable the views fall back to the simulated
    # fixtures rather than hiding everything.
    async def _warm_instances(app: web.Application) -> None:
        from .. import agent_instances

        async def _loop() -> None:
            while True:
                try:
                    await agent_instances.warm_csm_instances()
                except Exception:  # pragma: no cover - best effort
                    logger.debug("instance discovery skipped")
                await asyncio.sleep(300)  # refresh every 5 minutes

        app["_cp_instance_task"] = app.loop.create_task(_loop())

    app.on_startup.append(_warm_directory)
    app.on_startup.append(_warm_cost)
    app.on_startup.append(_warm_instances)
    logger.info("CSM Autopilot control plane mounted (/app adaptive: /manager, /sponsor).")
