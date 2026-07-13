"""
Standalone entry point for the CSM Autopilot control plane.

Run with:  python -m src.control_plane

Builds a self-contained aiohttp app that serves the control plane and its
business APIs **without importing the agent or the (Windows-only) GitHub Copilot
SDK**, so it runs unchanged in the Linux container. The Teams app's static tab
points at ``/control-plane`` on this host.

Observability (OTEL + A365) is configured first, exactly like the agent host.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _configure_observability() -> None:
    """Best-effort observability setup; missing optional deps must not stop the UI."""
    try:
        from ..telemetry import configure_otel_providers

        configure_otel_providers(service_name=os.getenv("OTEL_SERVICE_NAME", "csm_autopilot_control_plane"))
    except Exception as exc:  # pragma: no cover - optional deps
        logger.warning("OTEL not configured (%s); continuing without it.", exc)
    try:
        from ..observability import configure_a365_observability

        configure_a365_observability()
    except Exception as exc:  # pragma: no cover - optional deps
        logger.warning("A365 observability not configured (%s); continuing.", exc)


def build_app() -> web.Application:
    from .web import attach_control_plane

    app = web.Application()
    attach_control_plane(app)
    return app


def main() -> None:
    _configure_observability()
    port = int(os.getenv("PORT", "3978"))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info("CSM Autopilot control plane listening on http://%s:%d/control-plane", host, port)
    web.run_app(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
