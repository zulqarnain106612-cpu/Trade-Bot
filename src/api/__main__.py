"""
Entrypoint that honours the API_* settings.

``uvicorn src.api.main:app --port 8000`` (the command the README used to give)
takes its host, port and reload flag from the command line, so API_PORT and
API_RELOAD were documented as configuration and read by nothing. An operator
setting API_PORT=9000 got a server on whatever the command line said, with no
error and no log line to say the setting had been ignored.

This module closes that gap:

    uv run python -m src.api

API_HOST is validated in main.py's lifespan (it refuses to bind a
non-loopback address without an explicit acknowledgement), so binding is
still guarded exactly as before.
"""

from __future__ import annotations

import uvicorn

from src.config import get_settings


def main() -> None:
    cfg = get_settings().api
    uvicorn.run(
        "src.api.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=cfg.reload,
    )


if __name__ == "__main__":
    main()
