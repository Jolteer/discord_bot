"""Start the dashboard: python -m joltbot.web

Runs uvicorn, a production ASGI server, as a single process. Host and port come
from HOST / PORT (defaults 127.0.0.1:8000); --host / --port override them.
One process is plenty here, and it keeps the in-memory Discord caches in one place.
"""

from __future__ import annotations

import argparse

import uvicorn

from ..core.config import get_config
from ..core.logsetup import setup_logging


def main() -> None:
    """CLI entry: run uvicorn with factory ``joltbot.web.app:create_app``.

    Host/port come from ``HOST``/``PORT`` unless overridden by ``--host``/``--port``.
    ``--reload`` is for local development only.
    """
    config = get_config()
    parser = argparse.ArgumentParser(description="Run the JoltBot dashboard")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    parser.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = parser.parse_args()
    setup_logging("web", config.log_level, config.log_dir)
    uvicorn.run(
        "joltbot.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=True,  # trust X-Forwarded-* from the reverse proxy (Caddy)...
        forwarded_allow_ips=config.forwarded_allow_ips,  # ...but only from these addresses
        log_config=None,  # use our logging setup
    )


if __name__ == "__main__":
    main()
