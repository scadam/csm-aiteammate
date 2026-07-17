"""Control-plane entry point with A365 configured before FastAPI import."""

from __future__ import annotations

import os

import uvicorn

from . import config, observability


def main() -> None:
    observability.configure_a365()
    port = int(
        os.getenv(
            config.SPEC.runtime.control_plane.port_env,
            str(config.SPEC.runtime.control_plane.default_port),
        )
    )
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
