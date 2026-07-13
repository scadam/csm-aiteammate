"""
Real Microsoft 365 grounding via Microsoft Graph (server-side, app-only).

This is the **control-plane** path to genuine Microsoft 365 grounding. The Work IQ
MCP server is Microsoft Entra **delegated-only** (it must be called on a signed-in
manager's behalf via OBO), which the bot/agent host does. The control plane,
however, runs **server-side with no per-turn user token**, so it grounds by
reading the manager's *real* mailbox and calendar directly from Microsoft Graph
using the host's **managed identity** (a normal service principal that Exchange
Online accepts), with the application permissions ``Mail.Read`` and
``Calendars.Read``.

Microsoft Entra **Agent Identity** tokens are *not* accepted by Exchange Online
for mailbox access (they return HTTP 401), which is why the managed identity —
not the agent blueprint/agent-identity — is used here.

The summary returned has the same human-readable shape the Content Build step
expects, so a draft can open on a real, recent touchpoint with the contact.
Returns ``None`` when Graph is unavailable (caller falls back to the offline
fixture) and ``""`` when Graph is reachable but there is no recent activity.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import graph_app

logger = logging.getLogger(__name__)


def _norm(v: str | None) -> str:
    return (v or "").strip().lower()


def _addr(recipient: dict) -> str:
    return _norm(((recipient or {}).get("emailAddress") or {}).get("address"))


def _mentions_contact(msg: dict, contact_email: str, contact_first: str, contact_last: str) -> bool:
    """True if a message involves the contact (sender or any recipient, or name in subject)."""
    ce = _norm(contact_email)
    people = [_addr(msg.get("from"))]
    for bucket in ("toRecipients", "ccRecipients"):
        people += [_addr(r) for r in (msg.get(bucket) or [])]
    if ce and ce in people:
        return True
    # Fall back to a name match in the subject/preview (handles shared-mailbox quirks).
    blob = f"{msg.get('subject', '')} {msg.get('bodyPreview', '')}".lower()
    return bool(contact_last) and contact_last in blob and bool(contact_first) and contact_first in blob


async def _recent_emails(manager_upn: str, contact_email: str, contact_first: str,
                         contact_last: str, token: str) -> list[dict] | None:
    path = f"/users/{urllib.parse.quote(manager_upn)}/messages"
    params = {
        "$top": "40",
        "$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }
    data = await graph_app.graph_get(path, token, params=params)
    if data is None:
        return None
    out = []
    for msg in data.get("value", []):
        if _mentions_contact(msg, contact_email, contact_first, contact_last):
            out.append(msg)
        if len(out) >= 3:
            break
    return out


async def _recent_meetings(manager_upn: str, contact_email: str, contact_first: str,
                           contact_last: str, token: str) -> list[dict] | None:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/users/{urllib.parse.quote(manager_upn)}/calendarView"
    params = {
        "startDateTime": start, "endDateTime": end,
        "$select": "subject,start,attendees,bodyPreview",
        "$top": "40", "$orderby": "start/dateTime desc",
    }
    data = await graph_app.graph_get(path, token, params=params)
    if data is None:
        return None
    ce = _norm(contact_email)
    out = []
    for ev in data.get("value", []):
        attendees = [_addr(a) for a in (ev.get("attendees") or [])]
        blob = f"{ev.get('subject', '')} {ev.get('bodyPreview', '')}".lower()
        if (ce and ce in attendees) or (contact_last and contact_last in blob):
            out.append(ev)
        if len(out) >= 2:
            break
    return out


async def ground_with_contact(
    *, manager_upn: str, manager_name: str, contact_name: str, contact_email: str, topic: str = ""
) -> str | None:
    """Summarise the manager's REAL recent Microsoft 365 activity with the contact.

    ``None``  → Graph unavailable (e.g. local dev without the managed identity);
    ``""``    → Graph reachable but no recent activity with this contact;
    ``str``   → a readable summary of real emails + meetings.
    """
    if not manager_upn:
        return None
    token = await graph_app.app_token()
    if not token:
        return None

    parts = (contact_name or "").split()
    contact_first = _norm(parts[0]) if parts else ""
    contact_last = _norm(parts[-1]) if parts else ""

    emails = await _recent_emails(manager_upn, contact_email, contact_first, contact_last, token)
    meetings = await _recent_meetings(manager_upn, contact_email, contact_first, contact_last, token)
    if emails is None and meetings is None:
        logger.info("M365 grounding: Graph unavailable for %s (%s).", manager_upn,
                    graph_app.unavailable_reason() or "no reason")
        return None

    emails = emails or []
    meetings = meetings or []
    if not emails and not meetings:
        return ""  # real, but nothing recent with this contact

    lines = [f"Recent Microsoft 365 activity between {manager_name or 'the CSM'} and {contact_name}:"]
    for e in emails:
        when = str(e.get("receivedDateTime", ""))[:10]
        sender = _addr(e.get("from"))
        direction = "from them" if sender == _norm(contact_email) else "from you"
        preview = (e.get("bodyPreview") or "").strip().replace("\r", " ").replace("\n", " ")[:160]
        lines.append(f"- Email ({when}, {direction}) “{e.get('subject', '')}”: {preview}")
    for m in meetings:
        when = str((m.get("start") or {}).get("dateTime", ""))[:10]
        preview = (m.get("bodyPreview") or "").strip().replace("\r", " ").replace("\n", " ")[:140]
        lines.append(f"- Meeting ({when}) “{m.get('subject', '')}”: {preview}")
    return "\n".join(lines)
