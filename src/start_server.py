"""aiohttp hosting for the agent: exposes ``POST /api/messages``."""

from __future__ import annotations

from os import environ

from aiohttp.web import Application, Request, Response, run_app
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import AgentApplication, AgentAuthConfiguration


def start_server(
    agent_application: AgentApplication,
    auth_configuration: AgentAuthConfiguration,
) -> None:
    async def entry_point(req: Request) -> Response:
        agent: AgentApplication = req.app["agent_app"]
        adapter: CloudAdapter = req.app["adapter"]
        return await start_agent_process(req, agent, adapter)

    APP = Application(middlewares=[jwt_authorization_middleware])
    APP.router.add_post("/api/messages", entry_point)
    APP["agent_configuration"] = auth_configuration
    APP["agent_app"] = agent_application
    APP["adapter"] = agent_application.adapter

    # Mount the CSM Autopilot control plane (/control-plane + business APIs)
    # alongside the bot endpoint so a single local run serves both.
    from .control_plane.web import attach_control_plane

    attach_control_plane(APP)

    run_app(APP, host=environ.get("HOST", "0.0.0.0"), port=int(environ.get("PORT", 3978)))
