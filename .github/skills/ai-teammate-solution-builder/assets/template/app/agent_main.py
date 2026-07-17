"""Agent host entry point. Run with: python -m app.agent_main"""

from __future__ import annotations

from .observability import configure_a365

configure_a365()

import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("microsoft_agents").setLevel(logging.INFO)

from .agent import AGENT_APP, CONNECTION_MANAGER
from .start_server import start_server


def main() -> None:
    start_server(
        AGENT_APP,
        CONNECTION_MANAGER.get_default_connection_configuration(),
    )


if __name__ == "__main__":
    main()
