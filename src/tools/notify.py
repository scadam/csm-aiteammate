"""Proactive manager notification tool (HITL escalation).

Lets the teammate send a 1:1 Teams message **to its manager**, authored as its
own agentic-user identity — the human-in-the-loop escalation path. Backed by
:func:`src.notifications.notify_manager`, which mints a delegated Graph token via
the agentic-user federation and posts to the manager's 1:1 chat.
"""

from __future__ import annotations

import logging

from .. import identity, observability

logger = logging.getLogger(__name__)


async def notify_manager(message: str, title: str = "") -> str:
    """Send a proactive 1:1 Teams message to the current manager.

    Use this to escalate a decision that needs the CSM's judgment, or to share a
    prepared brief. Returns a short human-readable status string.
    """
    ctx = identity.current_context()
    turn_context = getattr(ctx, "turn_context", None) if ctx else None
    if turn_context is None:
        return "Notification skipped: no live conversation to send from (offline/dev run)."

    # Imported lazily to avoid an import cycle (agent -> tools -> agent).
    from ..agent import CONNECTION_MANAGER
    from .. import notifications

    with observability.execute_tool_scope("notify_manager", {"title": title}):
        result = await notifications.notify_manager(
            turn_context, message, connection_manager=CONNECTION_MANAGER, title=title or None
        )

    status = result.get("status")
    if status == "sent":
        return f"Sent a 1:1 Teams message to {result.get('manager', 'your manager')}."
    if status == "skipped":
        return f"Notification not sent ({result.get('reason', 'unavailable')})."
    return f"Notification failed: {result.get('reason', 'unknown error')}."
