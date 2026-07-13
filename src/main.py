"""
Entry point for the CSM AI Teammate.

Run with:  python -m src.main

OTEL providers and A365 observability are configured **first**, before importing
the agent or server, so global providers are installed before any instrumented
library loads.
"""

from __future__ import annotations

# 1) Observability first (before importing the agent/server).
from .telemetry import configure_otel_providers

configure_otel_providers(service_name="csm_ai_teammate")

from .observability import configure_a365_observability

configure_a365_observability()

# 2) Standard logging for the SDK.
import logging

ms_agents_logger = logging.getLogger("microsoft_agents")
ms_agents_logger.addHandler(logging.StreamHandler())
ms_agents_logger.setLevel(logging.INFO)

# 3) Build the app and start the server.
from .agent import AGENT_APP, CONNECTION_MANAGER
from .start_server import start_server


def main() -> None:
    start_server(
        agent_application=AGENT_APP,
        auth_configuration=CONNECTION_MANAGER.get_default_connection_configuration(),
    )


if __name__ == "__main__":
    main()
