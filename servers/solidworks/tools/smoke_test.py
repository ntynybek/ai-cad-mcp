"""Real stdio MCP smoke test; optional live SOLIDWORKS package test.

python tools/smoke_test.py --live --launch
Creates only new test documents; keeps them open for inspection. Does not close/quit SW.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    server = StdioServerParameters(command=sys.executable, args=["-m", "solidworks_mcp", "--config", args.config], env=env)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            print(json.dumps({"server": initialized.serverInfo.name, "tools": len(tools.tools)}, ensure_ascii=False), flush=True)

            async def call(name, data):
                result = await session.call_tool(name, {"request": data}, read_timeout_seconds=__import__("datetime").timedelta(seconds=180))
                value = result.structuredContent or json.loads(result.content[0].text)
                print(json.dumps({"tool": name, "isError": result.isError, "result": value}, ensure_ascii=False), flush=True)
                if result.isError:
                    raise RuntimeError(f"{name} failed")
                while value.get("state") == "pending":
                    await asyncio.sleep(2)
                    result = await session.call_tool("sw_job_status", {"job_id": value["job_id"]})
                    value = result.structuredContent or json.loads(result.content[0].text)
                    if value["state"] == "failed":
                        raise RuntimeError(value["error"])
                    if value["state"] == "succeeded":
                        return value["result"]
                return value.get("result", value)

            await call("sw_status", {})
            await call("sw_library_index", {})
            await call("sw_library_search", {"query": "square", "limit": 3})
            if args.live:
                await call("sw_connect", {"launch": args.launch})
                await call("sw_documents", {})
                if args.package:
                    result = await call("sw_bracket_package", {"name": "bracket_live", "designation": "MCP.001", "drawing": True})
                    assert all(Path(p).is_file() for p in result["outputs"].values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="solidworks-mcp.local.toml")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--package", action="store_true")
    asyncio.run(run(parser.parse_args()))
