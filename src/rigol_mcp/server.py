"""Command-line entry point: registers the tools and runs over stdio or HTTP."""

from __future__ import annotations

import argparse
import os

from . import __version__, app
from . import tools_control, tools_export, tools_log, tools_read, tools_sessions, tools_setup  # noqa: F401  (registers the tools)
from .app import Config, configure, mcp  # noqa: F401  (re-exported for convenience)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rigol-mcp", description="MCP server for Rigol DS1000Z oscilloscopes")
    parser.add_argument("--version", action="version", version=f"rigol-mcp {__version__}")
    parser.add_argument("--scope", metavar="HOST", help="scope IP or host name (env RIGOL_HOST)")
    parser.add_argument("--http", action="store_true", default=os.environ.get("RIGOL_TRANSPORT") == "http",
                        help="serve over HTTP with a screenshot gallery instead of stdio (env RIGOL_TRANSPORT=http)")
    parser.add_argument("--bind", default=os.environ.get("RIGOL_HTTP_HOST", "0.0.0.0"), help="HTTP bind address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RIGOL_HTTP_PORT", "8790")), help="HTTP port")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if args.scope:
        cfg.host = args.scope
    configure(cfg)

    if args.http:
        from .web import serve

        serve(args.bind, args.port)
    else:
        app.mcp.run()


if __name__ == "__main__":
    main()
