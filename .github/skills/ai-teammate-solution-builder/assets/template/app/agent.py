"""Microsoft 365 Agents SDK application with Agent 365 identity and telemetry."""

from __future__ import annotations

import logging
from os import environ

from dotenv import load_dotenv
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from . import agent_identity, config, observability, reasoning
from .capabilities import CapabilityRegistry
from .data import DataCatalog
from .state import create_state_store


logger = logging.getLogger(__name__)
load_dotenv()
agents_sdk_config = load_configuration_from_env(environ)

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
agent_identity.configure_identity(AUTHORIZATION, CONNECTION_MANAGER.get_default_connection())

_STATE = create_state_store()
_DATA = DataCatalog(config.SPEC)
CAPABILITY_REGISTRY = CapabilityRegistry(config.SPEC, _DATA, _STATE)
from .workflows import WorkflowEngine

WORKFLOW_ENGINE = WorkflowEngine(config.SPEC, _DATA, _STATE, CAPABILITY_REGISTRY)
CAPABILITY_REGISTRY.bind_workflow_engine(WORKFLOW_ENGINE)


def _conversation_id(context: TurnContext) -> str:
    conversation = getattr(context.activity, "conversation", None)
    return getattr(conversation, "id", None) or "default"


def _manager_id(context: TurnContext) -> str:
    sender = getattr(context.activity, "from_property", None)
    sender_id = getattr(sender, "aad_object_id", None) or getattr(sender, "id", None)
    assigned = next(
        (item for item in config.SPEC.managers if item.id == config.AGENT_MANAGER_ID), None
    )
    if assigned is None:
        raise PermissionError("This agent instance has no declared manager assignment")
    if sender_id and assigned.principal_id != sender_id:
        raise PermissionError("The signed-in user is not this agent instance's manager")
    return assigned.id


@AGENT_APP.conversation_update("membersAdded")
async def on_members_added(context: TurnContext, _state: TurnState):
    await context.send_activity(config.SPEC.agent.introduction)
    return True


@AGENT_APP.activity("message", auth_handlers=[config.OBO_HANDLER_ID])
async def on_message(context: TurnContext, _state: TurnState):
    user_text = (context.activity.text or "").strip()
    if not user_text:
        await context.send_activity("Send me a request related to my configured skills and workflows.")
        return True
    manager_id = _manager_id(context)
    conversation_id = _conversation_id(context)
    activity_id = getattr(context.activity, "id", None) or f"message:{hash(user_text)}"
    request = agent_identity.request_context(
        manager_id,
        conversation_id,
        turn_context=context,
        principal_id=getattr(getattr(context.activity, "from_property", None), "aad_object_id", "") or "",
        activity_id=str(activity_id),
    )
    token = agent_identity.set_context(request)
    scope = None
    try:
        await observability.setup_export_token(AGENT_APP.auth, context)
        with observability.invoke_agent_scope(
            user_text,
            session_id=request.session_key,
            conversation_id=conversation_id,
        ) as scope:
            response = await reasoning.run_turn(
                user_text,
                session_key=request.session_key,
                registry=CAPABILITY_REGISTRY,
                context={
                    "manager": {"id": manager_id},
                    "conversationId": conversation_id,
                    "idempotencyKey": f"turn:{request.session_key}:{activity_id}",
                },
            )
            observability.record_response(scope, response)
            await context.send_activity(response)
    except Exception as exc:
        observability.record_error(scope, exc)
        logger.exception("Agent turn failed")
        await context.send_activity("The request could not be completed safely. Please try again.")
    finally:
        agent_identity.reset_context(token)
        observability.force_flush()
    return True


@AGENT_APP.error
async def on_error(context: TurnContext, error: Exception):
    logger.exception("Unhandled Agent SDK error", exc_info=error)
    try:
        await context.send_activity("The agent encountered an unexpected error.")
    finally:
        observability.force_flush()
