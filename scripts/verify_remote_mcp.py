"""
Smoke-test a deployed (or local) CSM AI Teammate MCP server: list its tools and,
optionally, call a couple of them end-to-end.

The MCP server is FastMCP streamable-HTTP, served at ``<base>/mcp``. This script
connects as an MCP client, initialises a session, lists the tools, and (with
``--call``) exercises the real back ends (Azure OpenAI NL-to-SQL + Snowflake via
``query_csm_database`` and Gainsight via ``get_account_context``).

Usage:
    # Uses MCP__PUBLIC_URL from the environment/.env by default.
    python -m scripts.verify_remote_mcp
    python -m scripts.verify_remote_mcp --url https://<fqdn>/mcp --call
"""

from __future__ import annotations

import argparse
import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def _run(url: str, do_call: bool) -> None:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"Remote MCP OK - {len(names)} tools at {url}:")
            for name in names:
                print(f"  - {name}")

            if do_call:
                print("\n--- query_csm_database('How many accounts are there?') ---")
                res = await session.call_tool(
                    "query_csm_database", {"question": "How many accounts are there?"}
                )
                for chunk in res.content:
                    print(getattr(chunk, "text", str(chunk))[:600])

                print("\n--- get_account_context('ACC-1001') ---")
                res = await session.call_tool("get_account_context", {"account_id": "ACC-1001"})
                for chunk in res.content:
                    print(getattr(chunk, "text", str(chunk))[:600])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("MCP__PUBLIC_URL", "http://127.0.0.1:8000/mcp"),
        help="MCP endpoint (default: $MCP__PUBLIC_URL or local).",
    )
    parser.add_argument(
        "--call",
        action="store_true",
        help="Also invoke query_csm_database + get_account_context end-to-end.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.url, args.call))


if __name__ == "__main__":
    main()
