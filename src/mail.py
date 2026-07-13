"""
Real email delivery for the CSM Autopilot.

A customer-facing email is sent **as the manager** (the CSM the agent acts for),
so it lands in the CSM's Sent Items and is delivered to the account's real
contact mailbox. Two transports, tried in order:

1. **Work IQ (OBO)** — when a live manager turn context is present (the agent /
   bot path) and a Work IQ MCP endpoint is configured, send via
   ``do_action /me/sendMail`` on the manager's behalf.
2. **Graph app-only** — otherwise (e.g. the control-plane review/approve flow,
   which has no signed-in turn), send via Graph ``/users/{manager}/sendMail``
   using the host's app-only identity (needs the ``Mail.Send`` application
   permission). Still sends *as the manager*.

Returns a structured result so callers can record an honest delivered/queued
state. Never raises.
"""

from __future__ import annotations

import json
import logging

from . import config, data_store, email_render, graph_app, identity, workiq_client

logger = logging.getLogger(__name__)


def _manager(manager_id: str | None) -> dict:
    return data_store.get("managers", "manager_id", manager_id or "") or {}


def _manager_upn(manager_id: str | None) -> str:
    return _manager(manager_id).get("upn") or ""


async def deliver_email(
    *, account_id: str, subject: str, body: str, manager_id: str | None = None,
    html: str | None = None,
) -> dict:
    """Send a real email to the account's contact, as the manager. Returns a dict.

    The wire body is a polished, branded **HTML** email (rendered from the
    plain-text draft when ``html`` is not supplied), so the customer receives a
    professional, on-brand message rather than raw text.

    ``{"sent": bool, "channel": "email"|..., "to": <addr>, "as": <sender>,
    "detail": <str>}``.
    """
    account = data_store.resolve_account(account_id)
    if account is None:
        return {"sent": False, "channel": "email", "detail": f"account '{account_id}' not found"}
    to_email = account.get("primary_contact_email") or account.get("primary_contact", "")
    mid = manager_id or identity.current_manager_id()
    manager = _manager(mid)
    sender = manager.get("upn") or ""
    if not to_email:
        return {"sent": False, "channel": "email", "detail": "no contact email on account"}

    # Render the branded HTML email once (used by whichever transport sends it).
    html_body = html or email_render.render_email_html(
        subject=subject,
        body_text=body,
        manager_name=manager.get("display_name") or "Your Customer Success Manager",
        manager_role=manager.get("role") or "Customer Success Manager",
        manager_email=sender or "customer.success@example.com",
        recipient_name=account.get("primary_contact", ""),
        account_name=account.get("account_name", ""),
    )

    # 1) Work IQ on the manager's behalf (OBO) — only when a live turn + endpoint exist.
    if config.USE_WORKIQ:
        token = await identity.exchange_obo_token([config.WORKIQ_SCOPE])
        if token:
            mail = json.dumps({
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [{"emailAddress": {"address": to_email}}],
                },
                "saveToSentItems": True,
            })
            try:
                await workiq_client.call_tool("do_action", {"actionUrl": "/me/sendMail", "jsonBody": mail}, token)
                return {"sent": True, "channel": "email", "to": to_email, "as": "manager (Work IQ OBO)",
                        "detail": f"Email sent to {to_email} via Work IQ on the manager's behalf."}
            except workiq_client.WorkIQError as exc:
                logger.info("Work IQ send failed, falling back to Graph app-only: %s", exc)

    # 2) Graph app-only sendMail AS the manager (control-plane / no-turn path).
    if sender:
        sent, detail = await graph_app.send_mail_as_user(sender, to_email, subject, html_body, html=True)
        if sent:
            return {"sent": True, "channel": "email", "to": to_email, "as": sender,
                    "detail": f"Email sent to {to_email} from {sender}'s mailbox."}
        return {"sent": False, "channel": "email", "to": to_email, "as": sender, "detail": detail}

    return {"sent": False, "channel": "email", "to": to_email,
            "detail": "No manager UPN to send as, and no Work IQ token available."}


async def save_draft(
    *, account_id: str, subject: str, body: str, manager_id: str | None = None,
    html: str | None = None,
) -> dict:
    """Create a real **draft** of the branded email in the manager's mailbox.

    The CSM opens it in Outlook to review and send themselves — nothing is sent
    here. Returns ``{"saved": bool, "to": <addr>, "as": <sender>, "detail": str}``.
    """
    account = data_store.resolve_account(account_id)
    if account is None:
        return {"saved": False, "channel": "draft", "detail": f"account '{account_id}' not found"}
    to_email = account.get("primary_contact_email") or account.get("primary_contact", "")
    mid = manager_id or identity.current_manager_id()
    manager = _manager(mid)
    sender = manager.get("upn") or ""
    if not to_email:
        return {"saved": False, "channel": "draft", "detail": "no contact email on account"}
    if not sender:
        return {"saved": False, "channel": "draft", "detail": "no manager mailbox to draft in"}

    html_body = html or email_render.render_email_html(
        subject=subject,
        body_text=body,
        manager_name=manager.get("display_name") or "Your Customer Success Manager",
        manager_role=manager.get("role") or "Customer Success Manager",
        manager_email=sender or "customer.success@example.com",
        recipient_name=account.get("primary_contact", ""),
        account_name=account.get("account_name", ""),
    )
    saved, detail = await graph_app.create_draft_as_user(sender, to_email, subject, html_body, html=True)
    return {"saved": saved, "channel": "draft", "to": to_email, "as": sender, "detail": detail}
