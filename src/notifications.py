"""
Agent 365 notifications — outbound (proactive) and inbound.

Two capabilities, modelled on the Microsoft Agent365 sample (``sample-agent``)
and the working ``ess-mcp`` teammate:

1. **Outbound / proactive** — the teammate posts a 1:1 Teams message to *its
   manager* (e.g. to escalate a human-in-the-loop decision), authored **as its
   own agentic-user identity**. It mints a delegated Graph token via the
   Microsoft Agents SDK agentic-user federation
   (``connection.get_agentic_user_token(...)``), then creates (or reuses) the
   oneOnOne chat and posts the message. This is the architecture's HITL
   escalation path.

2. **Inbound** — A365 notification handlers (email / Word comment / lifecycle)
   registered through :class:`microsoft_agents_a365.notifications.AgentNotification`,
   exactly as the sample does. Each handler runs the agent's reasoning loop over
   the notification content and replies.

Everything is defensive: when the agentic identity, the SDK, or a live turn
context isn't available (local/offline dev), the helpers degrade to a clearly
marked no-op so the rest of the agent runs unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from . import config

logger = logging.getLogger(__name__)

GRAPH_BASE = config.GRAPH_BASE_URL.rstrip("/")


# ── actor extraction from the inbound activity ──────────────────────────────
@dataclass
class ActorContext:
    """The identities involved in a turn, taken from the inbound activity.

    The A365 platform populates these on every activity — no API call needed:
    the **recipient** is this agent's own agentic identity (instance app id +
    agentic-user object id), the **sender** is the human the agent works for.
    """

    tenant_id: str
    instance_app_id: str        # recipient.agentic_app_id (the instance's app id)
    agentic_user_id: str        # recipient.aad_object_id (the agent-user's oid)
    manager_aad_id: str         # from_property.aad_object_id (the manager's oid)
    manager_name: str

    @property
    def is_complete(self) -> bool:
        return bool(
            self.tenant_id
            and self.instance_app_id
            and self.agentic_user_id
            and self.manager_aad_id
        )


def _agentic_user_from_recipient(recipient: Any) -> str:
    """Resolve the agent-user object id from ``recipient``.

    Prefers ``aad_object_id``; falls back to parsing ``recipient.id`` of the
    form ``8:orgid:<objectId>`` that per-user teammate activities carry.
    """
    oid = getattr(recipient, "aad_object_id", None)
    if oid:
        return oid
    rid = getattr(recipient, "id", "") or ""
    if rid.startswith("8:orgid:"):
        return rid.split("8:orgid:", 1)[1]
    return ""


def extract_actor(context: Any) -> ActorContext:
    """Build an :class:`ActorContext` from a turn context (best-effort)."""
    activity = getattr(context, "activity", None)
    recipient = getattr(activity, "recipient", None)
    frm = getattr(activity, "from_property", None)
    return ActorContext(
        tenant_id=(getattr(recipient, "tenant_id", None) or config.AGENT_TENANT_ID or ""),
        instance_app_id=(getattr(recipient, "agentic_app_id", None) or ""),
        agentic_user_id=_agentic_user_from_recipient(recipient) if recipient else "",
        manager_aad_id=(getattr(frm, "aad_object_id", None) or "") if frm else "",
        manager_name=(getattr(frm, "name", None) or "") if frm else "",
    )


# ── outbound: proactive 1:1 Teams message to the manager ────────────────────
async def notify_manager(
    context: Any,
    message: str,
    *,
    connection_manager: Any,
    title: str | None = None,
) -> dict[str, Any]:
    """Post a 1:1 Teams message to the manager **as the agent's own identity**.

    Returns ``{"status": "sent"|"skipped"|"error", ...}``. Never raises — a
    notification failure must not break the turn.
    """
    if not config.ENABLE_MANAGER_NOTIFICATIONS:
        return {"status": "skipped", "reason": "manager notifications disabled"}

    actor = extract_actor(context)
    if not actor.is_complete:
        logger.info("notify_manager: incomplete actor (offline/dev) — skipping.")
        return {"status": "skipped", "reason": "no agentic identity on this turn"}
    if connection_manager is None:
        return {"status": "skipped", "reason": "no connection manager"}

    html = _format_html(title, message)

    # 1) Mint a delegated Graph token via the agentic-user federation.
    try:
        conn = connection_manager.get_default_connection()
        token = await conn.get_agentic_user_token(
            actor.tenant_id,
            actor.instance_app_id,
            actor.agentic_user_id,
            [config.AGENTIC_USER_GRAPH_SCOPE],
        )
    except Exception as exc:  # pragma: no cover - depends on live auth
        logger.warning("notify_manager: agentic-user token failed: %s", exc)
        return {"status": "error", "reason": f"token: {type(exc).__name__}: {exc}"}
    if not token:
        return {"status": "error", "reason": "no agentic-user token"}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    chat_payload = {
        "chatType": "oneOnOne",
        "members": [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH_BASE}/users/{actor.agentic_user_id}",
            },
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"{GRAPH_BASE}/users/{actor.manager_aad_id}",
            },
        ],
    }
    try:
        async with aiohttp.ClientSession() as session:
            # 2) Create or fetch the canonical oneOnOne chat.
            async with session.post(
                f"{GRAPH_BASE}/chats", json=chat_payload, headers=headers
            ) as cr:
                if cr.status not in (200, 201):
                    body = await cr.text()
                    logger.warning("notify_manager: create chat HTTP %s: %s", cr.status, body[:300])
                    return {"status": "error", "reason": f"create chat HTTP {cr.status}"}
                chat = await cr.json()
            chat_id = chat.get("id")
            if not chat_id:
                return {"status": "error", "reason": "chat returned no id"}

            # 3) Post the message.
            import urllib.parse

            encoded = urllib.parse.quote(chat_id, safe="")
            async with session.post(
                f"{GRAPH_BASE}/chats/{encoded}/messages",
                json={"body": {"contentType": "html", "content": html}},
                headers=headers,
            ) as mr:
                if mr.status not in (200, 201):
                    body = await mr.text()
                    logger.warning("notify_manager: post message HTTP %s: %s", mr.status, body[:300])
                    return {"status": "error", "reason": f"post message HTTP {mr.status}"}
                msg = await mr.json()
    except Exception as exc:  # pragma: no cover - network/preview variance
        logger.warning("notify_manager: Graph call raised: %s", exc)
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    logger.info("notify_manager: delivered to %s (chat=%s)", actor.manager_name or actor.manager_aad_id, chat_id)
    return {
        "status": "sent",
        "chatId": chat_id,
        "messageId": msg.get("id"),
        "manager": actor.manager_name or actor.manager_aad_id,
    }


def _format_html(title: str | None, message: str) -> str:
    safe = (message or "").replace("\n", "<br/>")
    if title:
        return f"<b>{title}</b><br/>{safe}"
    return safe


# ── inbound: A365 notification handlers (email / Word) ──────────────────────
def register_inbound_handlers(agent_app: Any) -> Any | None:
    """Register A365 inbound notification handlers on ``agent_app``.

    Mirrors the Microsoft ``sample-agent``: email and Word-comment notifications
    are routed to the agent's reasoning loop. Returns the
    :class:`AgentNotification` instance, or ``None`` when the SDK isn't present.
    """
    try:
        from microsoft_agents_a365.notifications import (
            AgentNotification,
            AgentNotificationActivity,
            EmailResponse,
        )
    except Exception as exc:  # pragma: no cover - SDK optional on some hosts
        logger.info("A365 notifications SDK unavailable; inbound handlers not registered (%s).", exc)
        return None

    from . import reasoning

    try:
        notifier = AgentNotification(agent_app)

        @notifier.on_email()
        async def _on_email(context: Any, _state: Any, notification: "AgentNotificationActivity"):
            email = getattr(notification, "email", None)
            body = (getattr(email, "html_body", "") or getattr(email, "body", "")) if email else ""
            prompt = (
                "You received an email in your manager's mailbox. Read it and decide the right "
                "CSM next step (signal? draft? review task?). Do not invent product claims.\n\n"
                f"{body}"
            )
            reply = await _safe_reason(reasoning, prompt)
            try:
                await context.send_activity(EmailResponse.create_email_response_activity(reply))
            except Exception:  # pragma: no cover - response shape varies in preview
                await context.send_activity(reply)

        @notifier.on_word()
        async def _on_word(context: Any, _state: Any, notification: "AgentNotificationActivity"):
            comment = getattr(notification, "text", "") or "(no comment text)"
            reply = await _safe_reason(
                reasoning,
                f"You were @-mentioned on a Word document comment: {comment}. "
                "Respond helpfully and concisely as the CSM teammate.",
            )
            await context.send_activity(reply)

    except Exception as exc:  # pragma: no cover - preview API/host variance
        logger.warning("A365 inbound notification handlers failed to register: %s", exc)
        return None

    logger.info("A365 inbound notification handlers registered (email, word).")
    return notifier


async def _safe_reason(reasoning_mod: Any, prompt: str) -> str:
    try:
        return await reasoning_mod.run_turn(prompt)
    except Exception as exc:  # pragma: no cover
        logger.warning("notification reasoning failed: %s", exc)
        return "I received this notification but couldn't process it just now."
