import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).resolve().parent.parent / "server" / "main.py")


async def call_tool(name: str, args: dict | None = None):
    """Start the MCP server, call one tool over the MCP protocol, return its result."""
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args or {})

    if result.isError:
        text = result.content[0].text if result.content else "unknown error"
        raise RuntimeError(f"Tool {name} failed: {text}")

    data = result.structuredContent
    if data is None:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data.keys()) == {"result"}:
        data = data["result"]
    return data