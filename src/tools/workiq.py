"""
Work IQ (Microsoft 365 grounding) capability — REAL.

Grounds the agent in the manager's Microsoft 365 work data by consuming the
remote **Work IQ MCP** server on the manager's behalf (OBO). Work IQ is
Microsoft Entra delegated-only and permission-trimmed per the manager's
identity, so every call carries the manager's OBO token (never the bare agent
app token).

When ``WORKIQ__MCP__ENDPOINT`` is not configured, the tool falls back to the
offline JSON fixture (``data/workiq.json``) purely for local development — the
real remote server is the default path whenever an endpoint is set.
"""

from __future__ import annotations

import logging

from .. import config, data_store, identity, m365_grounding, workiq_client
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)


def _offline_search(query: str, account_id: str) -> str:
    """Local-dev fallback over data/workiq.json (used only when no endpoint is set).

    Returns a concise, human-readable summary of the manager's recent Microsoft 365
    activity with the account's contact (emails, meetings, Teams) so the Content
    Build step can open the outreach on a real, recent touchpoint. Scoped by
    ``account_id``; the query is used only as a soft relevance hint.
    """
    try:
        workiq = data_store.load("workiq")
    except FileNotFoundError:
        return "No Microsoft 365 data available (offline)."

    def _for_account(bucket: str) -> list:
        rows = [
            item for item in workiq.get(bucket, [])
            if not account_id or item.get("account_id") in ("", account_id)
        ]
        # Newest first when a date-like field is present.
        return sorted(
            rows,
            key=lambda r: str(r.get("received") or r.get("start") or r.get("sent") or r.get("date") or ""),
            reverse=True,
        )

    emails = _for_account("emails")[:3]
    meetings = _for_account("meetings")[:2]
    teams = _for_account("teams")[:3]
    if not (emails or meetings or teams):
        return "No recent Microsoft 365 interactions on record for this contact (offline)."

    account = data_store.get("accounts", "account_id", account_id) or {}
    contact = account.get("primary_contact", "the contact")
    lines: list[str] = [
        f"[offline] Recent Microsoft 365 activity with {contact}"
        f"{(' at ' + account.get('account_name')) if account.get('account_name') else ''}:"
    ]
    for e in emails:
        when = str(e.get("received", ""))[:10]
        lines.append(f"- Email ({when}) “{e.get('subject', '')}”: {e.get('snippet', '')}")
    for m in meetings:
        when = str(m.get("start", ""))[:10]
        lines.append(f"- Meeting ({when}) “{m.get('subject', '')}”: {m.get('notes', '')}")
    for t in teams:
        when = str(t.get("sent", ""))[:10]
        who = t.get("from", "")
        lines.append(f"- Teams ({when}) from {who}: {t.get('text', '')}")
    return "\n".join(lines)


async def search_microsoft_365(query: str, account_id: str = "") -> str:
    """
    Search/ground over the manager's Microsoft 365 work data via Work IQ.

    Uses the Work IQ ``ask`` tool (Microsoft 365 Copilot reasoning over the
    manager's mail, meetings, files, Teams messages, people, and enterprise
    search). Manager-scoped: acquires an OBO token first.

    Grounding resolves in three tiers, most-real first:

    1. **Work IQ MCP (OBO)** — when a signed-in manager turn is present (the
       bot/agent host), call Work IQ ``ask`` on the manager's behalf (delegated).
    2. **Microsoft Graph (managed identity)** — server-side (the control plane has
       no user turn), read the manager's *real* mailbox + calendar with the
       contact via Graph app-only. Genuine Microsoft 365 data, no fixture.
    3. **Offline fixture** — only when neither is available (local dev) or there is
       no real activity, so the experience still works.
    """
    with execute_tool_scope("workiq.search_microsoft_365", {"query": query, "account_id": account_id}):
        # 1) Real Work IQ via the manager's OBO token (bot/agent path).
        if config.USE_WORKIQ:
            token = await identity.exchange_obo_token([config.WORKIQ_SCOPE])
            if token:
                question = query if not account_id else f"{query} (for account {account_id})"
                try:
                    return await workiq_client.call_tool("ask", {"question": question}, token)
                except workiq_client.WorkIQError as exc:
                    logger.warning("Work IQ search failed; falling back to Graph grounding: %s", exc)

        # 2) Real Microsoft 365 grounding via Graph (managed identity) — server-side.
        manager = identity.resolve_manager() or {}
        account = data_store.get("accounts", "account_id", account_id) if account_id else None
        if manager.get("upn") and account:
            try:
                grounded = await m365_grounding.ground_with_contact(
                    manager_upn=manager.get("upn", ""),
                    manager_name=manager.get("display_name", ""),
                    contact_name=account.get("primary_contact", ""),
                    contact_email=account.get("primary_contact_email", ""),
                    topic=query,
                )
            except Exception as exc:  # pragma: no cover - depends on live Graph
                logger.warning("Graph M365 grounding failed: %s", exc)
                grounded = None
            if grounded:  # real, with content
                return grounded
            if grounded == "":  # real, reachable, but no recent activity
                logger.info("M365 grounding: no recent activity with contact; using offline fixture.")

        # 3) Offline fixture fallback (local dev / no real activity).
        return _offline_search(query, account_id)

