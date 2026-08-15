"""Run QueryGaP's MCP server over stdio or Streamable HTTP."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from querygap_mcp.server import mcp


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the QueryGaP MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for Streamable HTTP (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for Streamable HTTP (default: 8000).",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="Endpoint path for Streamable HTTP (default: /mcp).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.transport == "stdio":
        mcp.run()
        return

    mcp.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path=args.path,
        stateless_http=True,
        json_response=True,
        max_request_body_size=65_536,
    )


if __name__ == "__main__":
    main()
