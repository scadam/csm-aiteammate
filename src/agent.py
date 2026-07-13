"""
The CSM AI Teammate agent application.

Builds the Microsoft 365 Agents SDK ``AgentApplication`` (import-time singletons,
per §4 of the instructions), wires the GitHub Copilot reasoning loop, and sets a
per-turn :class:`~src.identity.RequestContext` so tools can resolve the manager
and perform On-Behalf-Of token exchange. Each turn is wrapped in an A365
``InvokeAgentScope`` for observability.
"""

from __future__ import annotations

import asyncio
import logging
from os import environ

from dotenv import load_dotenv

from microsoft_agents.activity import Activity, load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from . import config, identity, notifications, observability

# The GitHub Copilot SDK wheel is Windows-only. When it's available (local dev),
# the agent reasons through it for the richest streaming experience; in the Linux
# container it isn't installed, so the agent falls back to the Azure OpenAI
# tool-calling loop in ``reasoning`` over the same shared tools.
try:
    from . import copilot_session

    _HAS_COPILOT = True
except Exception:  # pragma: no cover - depends on platform/SDK availability
    copilot_session = None  # type: ignore[assignment]
    _HAS_COPILOT = False

from . import reasoning

logger = logging.getLogger(__name__)

load_dotenv()
agents_sdk_config = load_configuration_from_env(environ)

# Import-time singletons (constructed once).
STORAGE = MemoryStorage()
CONNECTION_MANAGER = MsalConnectionManager(**agents_sdk_config)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
AUTHORIZATION = Authorization(STORAGE, CONNECTION_MANAGER, **agents_sdk_config)

AGENT_APP = AgentApplication[TurnState](
    storage=STORAGE,
    adapter=ADAPTER,
    authorization=AUTHORIZATION,
    **agents_sdk_config,
)

# Register A365 inbound notification handlers (email / Word comment / lifecycle),
# modelled on the Microsoft sample-agent. No-op when the notifications SDK isn't
# installed (e.g. minimal hosts).
NOTIFIER = notifications.register_inbound_handlers(AGENT_APP)


async def _keep_typing(context: TurnContext, every: float = 4.0) -> None:
    """Keep the Teams "…" typing animation alive until cancelled.

    Typing indicators time out after ~5s, so we re-send every ~4s. Only visible
    in 1:1 and small group chats (sample-agent pattern).
    """
    try:
        while True:
            await asyncio.sleep(every)
            await context.send_activity(Activity(type="typing"))
    except asyncio.CancelledError:  # pragma: no cover - expected on completion
        pass


def _conversation_id(context: TurnContext) -> str:
    conv = getattr(context.activity, "conversation", None)
    return getattr(conv, "id", None) or "default"


def _inbound_principal(context: TurnContext):
    """Resolve the human messaging the instance from the inbound activity.

    The A365 platform populates ``activity.from_property`` on every message with
    ``aad_object_id`` / ``id`` / ``name`` — no API call needed. In a 1:1 chat the
    sender **is** the manager who owns this instance, so we resolve them to a CSM
    record by their Entra object id (or UPN). Returns the resolved
    :class:`~src.identity.UserPrincipal`, or ``None`` when there's no inbound
    sender (autonomous runs).
    """
    frm = getattr(context.activity, "from_property", None)
    if not frm:
        return None
    oid = getattr(frm, "aad_object_id", None)
    name = getattr(frm, "name", None)
    if not oid:
        return None
    return identity.resolve_user(object_id=oid, display_name=name, source="teams_sso")


def _request_context(context: TurnContext) -> "identity.RequestContext":
    """Build the per-turn context, scoped to the REAL inbound manager when known.

    Each agent instance acts on behalf of exactly one manager — the human who
    created/owns it. We take that manager from the inbound sender (the canonical
    A365 pattern) and only fall back to the configured assignment for autonomous
    runs with no inbound activity.
    """
    principal = _inbound_principal(context)
    if principal and principal.manager_id:
        return identity.RequestContext(
            manager_id=principal.manager_id,
            conversation_id=_conversation_id(context),
            turn_context=context,
            entra_object_id=principal.entra_object_id,
            upn=principal.upn,
        )
    # Fallback: the configured assignment (autonomous runs / unknown sender).
    mgr = identity.resolve_manager(config.AGENT_MANAGER_USER_ID) or {}
    return identity.RequestContext(
        manager_id=config.AGENT_MANAGER_USER_ID or "default",
        conversation_id=_conversation_id(context),
        turn_context=context,
        entra_object_id=mgr.get("entra_object_id"),
        upn=mgr.get("upn"),
    )



@AGENT_APP.conversation_update("membersAdded")
async def on_members_added(context: TurnContext, _state: TurnState):
    # Name the actual person who added me (the manager I work for), resolved from
    # the inbound activity — not a hardcoded assignment.
    ctx = _request_context(context)
    manager = identity.resolve_manager(ctx.manager_id)
    who = f" I work alongside {manager['display_name']}." if manager else ""
    await context.send_activity(
        f"Hello — I'm {config.AGENT_DISPLAY_NAME}, your Digital Customer Success Manager teammate "
        f"for your book of business.{who} I watch product adoption signals, draft outreach in your voice from approved "
        "content, and route anything that needs your judgment to your review queue. Ask me about an "
        "account, a signal, or what needs attention today."
    )
    return True


@AGENT_APP.activity("message")
async def on_message(context: TurnContext, _state: TurnState):
    user_text = (context.activity.text or "").strip()
    if not user_text:
        await context.send_activity("Send me a question about your accounts, signals, or outreach.")
        return True

    ctx = _request_context(context)
    conversation_id = ctx.conversation_id
    token = identity.set_request_context(ctx)
    try:
        # Exchange + cache the agentic-user token the A365 observability exporter
        # needs (no-op when observability is disabled or no live auth).
        await observability.setup_observability_token(AGENT_APP.auth, context)

        # Per-user GitHub identity for the Copilot runtime (optional).
        github_token = await identity.get_user_token(config.GITHUB_AUTH_HANDLER_ID)
        # Manager OBO token for Work IQ MCP (Microsoft 365 grounding) consumed by the loop.
        workiq_obo = await identity.exchange_obo_token([config.WORKIQ_SCOPE])

        with observability.invoke_agent_scope(
            content=user_text, session_id=conversation_id, conversation_id=conversation_id
        ):
            if _HAS_COPILOT:
                session = await copilot_session.get_session(ctx.session_key, github_token, workiq_obo)
                await copilot_session.stream_turn(session, user_text, context)
            else:
                # Linux/container path: Azure OpenAI tool-calling loop over the same tools.
                # Multiple-messages + typing pattern (sample-agent): acknowledge
                # immediately, keep the "…" animation alive, then send the answer.
                # Streaming is buffered for agentic identities, so we use discrete
                # send_activity calls instead.
                await context.send_activity("Got it — working on it…")
                typing_task = asyncio.create_task(_keep_typing(context))
                try:
                    reply = await reasoning.run_turn(user_text, session_key=ctx.session_key)
                finally:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except asyncio.CancelledError:
                        pass
                await context.send_activity(reply)
    finally:
        identity.reset_request_context(token)
    return True


@AGENT_APP.error
async def on_error(context: TurnContext, error: Exception):
    logger.exception("Unhandled agent error: %s", error)
    try:
        await context.send_activity("Sorry — something went wrong handling that. Please try again.")
    except Exception:  # pragma: no cover
        pass
    finally:
        observability.force_flush()
