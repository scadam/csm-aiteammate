"""
GitHub Copilot SDK session lifecycle.

One ``CopilotClient`` is shared per distinct GitHub identity (the SDK takes the
``github_token`` at the **client** level — the installed ``create_session`` has no
such argument), and one session is created per ``manager_id:conversation_id`` so
each manager gets isolated, multi-turn context. Responses are streamed to the
agent's ``streaming_response`` via session events.
"""

from __future__ import annotations

import asyncio
import logging

from copilot import CopilotClient, SessionEventType

from . import config
from .persona import build_persona
from .tools import COPILOT_TOOLS

logger = logging.getLogger(__name__)

_SKILLS_DIR = str((__import__("pathlib").Path(__file__).resolve().parent / "skills"))


def _provider_config() -> dict | None:
    """BYOK provider for the agentic loop.

    When ``COPILOT_PROVIDER=azure`` and the host has a managed-identity token, the
    Copilot SDK runs the loop against **Azure OpenAI** (no API key, no GitHub
    token) — honest token-billed inference on e.g. ``gpt-5.4-1``. Otherwise the
    SDK uses the GitHub Copilot identity/runtime.
    """
    if config.COPILOT_PROVIDER != "azure":
        return None
    base_url = config.azure_openai_base_url()
    if not base_url:
        logger.info("COPILOT_PROVIDER=azure but AZURE_OPENAI_ENDPOINT is unset; using default provider.")
        return None
    from .openai_client import aoai_bearer_token

    token = aoai_bearer_token()
    if not token:
        logger.info("No Azure OpenAI bearer token; using default Copilot provider.")
        return None
    return {
        "type": "azure",
        "base_url": base_url,
        "bearer_token": token,
        "azure": {"api_version": config.AZURE_OPENAI_API_VERSION},
        "model_id": config.COPILOT_MODEL,
    }

# One client per github token (None = the client's default/dev identity).
_clients: dict[str | None, CopilotClient] = {}
_client_locks: dict[str | None, asyncio.Lock] = {}
# One session per manager:conversation key.
_sessions: dict[str, object] = {}
_sessions_lock = asyncio.Lock()


async def _get_client(github_token: str | None) -> CopilotClient:
    key = github_token or None
    lock = _client_locks.setdefault(key, asyncio.Lock())
    async with lock:
        client = _clients.get(key)
        if client is None:
            client = CopilotClient(github_token=github_token or config.GITHUB_TOKEN or None)
            await client.start()
            _clients[key] = client
            logger.info("Started Copilot client (identity keyed=%s).", bool(github_token))
        return client


def _mcp_servers(obo_token: str | None) -> dict:
    """
    Build the remote MCP servers the reasoning loop consumes.

    - **Work IQ MCP** (Microsoft 365 grounding) on the manager's behalf (OBO).
    - The agent's **custom MCP** (Snowflake + Gainsight + CSM tools) via the
      **A365 Tooling Gateway** endpoint when registered as a BYO MCP server
      (governed) — falling back to the raw local MCP URL only if the gateway
      endpoint is not configured.

    Both are keyed by name and passed to ``create_session(mcp_servers=...)``.
    """
    servers: dict = {}
    if config.USE_WORKIQ and config.WORKIQ_MCP_ENDPOINT:
        headers = {"Authorization": f"Bearer {obo_token}"} if obo_token else {}
        servers["workiq"] = {
            "type": "http",
            "url": config.WORKIQ_MCP_ENDPOINT,
            "headers": headers,
        }
    gateway_url = config.MCP_GATEWAY_ENDPOINT or config.MCP_PUBLIC_URL
    if gateway_url:
        servers["csm_teammate"] = {
            "type": "http",
            "url": gateway_url,
        }
    return servers


async def get_session(session_key: str, github_token: str | None, obo_token: str | None = None):
    """Return (creating if needed) the Copilot session for a manager:conversation key."""
    async with _sessions_lock:
        session = _sessions.get(session_key)
        if session is not None:
            return session

        client = await _get_client(github_token)
        from copilot.session import PermissionHandler

        create_kwargs: dict = dict(
            model=config.COPILOT_MODEL,
            on_permission_request=PermissionHandler.approve_all,  # dev/local only
            tools=COPILOT_TOOLS,
            streaming=True,
            system_message={"mode": "append", "content": build_persona()},
        )
        # BYOK Azure provider (managed identity) — the loop runs on Azure OpenAI,
        # not GitHub premium requests, when configured.
        provider = _provider_config()
        if provider:
            create_kwargs["provider"] = provider
            logger.info("Copilot session using BYOK Azure provider (model=%s).", config.COPILOT_MODEL)
        # Let the SDK discover this repo's skills natively (SKILL.md folders).
        if config.COPILOT_ENABLE_SKILLS:
            create_kwargs["enable_skills"] = True
            create_kwargs["skill_directories"] = [_SKILLS_DIR]
        mcp_servers = _mcp_servers(obo_token)
        if mcp_servers:
            create_kwargs["mcp_servers"] = mcp_servers
        session = await client.create_session(**create_kwargs)
        _sessions[session_key] = session
        logger.info("Created Copilot session for %s (mcp_servers=%s).", session_key, list(mcp_servers))
        return session


async def stream_turn(session, user_text: str, context) -> None:
    """
    Send ``user_text`` to ``session`` and stream assistant deltas to the agent's
    ``context.streaming_response``. Resolves when the session goes idle.
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    error_holder: dict[str, str] = {}

    def on_event(evt) -> None:
        etype = getattr(evt, "type", None)
        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            delta = getattr(getattr(evt, "data", None), "delta_content", None)
            if delta:
                context.streaming_response.queue_text_chunk(delta)
        elif etype == SessionEventType.SESSION_IDLE:
            loop.call_soon_threadsafe(done.set)
        elif etype == SessionEventType.SESSION_ERROR:
            error_holder["error"] = str(getattr(evt, "data", "unknown error"))
            loop.call_soon_threadsafe(done.set)

    unsubscribe = session.on(on_event)
    try:
        await session.send(user_text)
        await done.wait()
    finally:
        unsubscribe()
        await context.streaming_response.end_stream()

    if "error" in error_holder:
        logger.warning("Copilot session error: %s", error_holder["error"])


async def reset() -> None:
    """Dispose all sessions/clients (used on shutdown)."""
    async with _sessions_lock:
        _sessions.clear()
    for client in list(_clients.values()):
        disconnect = getattr(client, "disconnect", None) or getattr(client, "stop", None)
        if disconnect:
            try:
                result = disconnect()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # pragma: no cover
                logger.debug("client disconnect failed: %s", exc)
    _clients.clear()
