"""aiohttp host for POST /api/messages using the Microsoft 365 Agents SDK."""

from __future__ import annotations

import os
from os import environ

from aiohttp import ClientSession, ClientTimeout
from aiohttp.web import Application, Request, Response, StreamResponse, middleware, run_app
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft_agents.hosting.core import AgentApplication, AgentAuthConfiguration

from . import config


_PROXY_EXACT = {"/", "/manager", "/fleet", "/privacy", "/terms", "/health", "/docs", "/openapi.json"}
_PROXY_PREFIXES = ("/api/", "/docs/")
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def _control_plane_path(path: str) -> bool:
    if path == config.SPEC.runtime.agent_host.message_path:
        return False
    return path in _PROXY_EXACT or path.startswith(_PROXY_PREFIXES)


@middleware
async def _message_auth_middleware(request: Request, handler):
    if request.path == config.SPEC.runtime.agent_host.message_path:
        return await jwt_authorization_middleware(request, handler)
    return await handler(request)


async def _proxy_control_plane(request: Request) -> StreamResponse:
    if request.method not in {"GET", "POST"} or not _control_plane_path(request.path):
        return Response(status=404, text="Not found")
    base = request.app["control_plane_internal_url"].rstrip("/")
    target = f"{base}{request.rel_url}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Host"] = request.host
    body = await request.read() if request.can_read_body else None
    session: ClientSession = request.app["control_plane_session"]
    async with session.request(
        request.method,
        target,
        headers=headers,
        data=body,
        allow_redirects=False,
    ) as upstream:
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }
        response = StreamResponse(status=upstream.status, headers=response_headers)
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(16384):
            await response.write(chunk)
        await response.write_eof()
        return response


def create_server(
    agent_application: AgentApplication,
    auth_configuration: AgentAuthConfiguration,
) -> Application:
    async def entry_point(request: Request) -> Response:
        application: AgentApplication = request.app["agent_app"]
        adapter: CloudAdapter = request.app["adapter"]
        return await start_agent_process(request, application, adapter)

    application = Application(middlewares=[_message_auth_middleware])
    application.router.add_post(config.SPEC.runtime.agent_host.message_path, entry_point)
    control_plane_url = os.getenv("CONTROL_PLANE_INTERNAL_URL", "").strip()
    if control_plane_url:
        if not control_plane_url.startswith("http://127.0.0.1:") and not control_plane_url.startswith("http://localhost:"):
            raise ValueError("CONTROL_PLANE_INTERNAL_URL must be a loopback HTTP sidecar URL")

        async def start_proxy(app: Application) -> None:
            app["control_plane_session"] = ClientSession(
                timeout=ClientTimeout(total=None, connect=10, sock_read=None)
            )

        async def stop_proxy(app: Application) -> None:
            await app["control_plane_session"].close()

        application["control_plane_internal_url"] = control_plane_url
        application.on_startup.append(start_proxy)
        application.on_cleanup.append(stop_proxy)
        application.router.add_route("*", "/{tail:.*}", _proxy_control_plane)
    application["agent_configuration"] = auth_configuration
    application["agent_app"] = agent_application
    application["adapter"] = agent_application.adapter
    return application


def start_server(
    agent_application: AgentApplication,
    auth_configuration: AgentAuthConfiguration,
) -> None:
    application = create_server(agent_application, auth_configuration)
    port = int(
        environ.get(
            config.SPEC.runtime.agent_host.port_env,
            str(config.SPEC.runtime.agent_host.default_port),
        )
    )
    run_app(application, host=environ.get("HOST", "0.0.0.0"), port=port)
