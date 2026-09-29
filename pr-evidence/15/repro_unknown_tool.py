"""Model calls a real tool and a hallucinated one in the same turn, against the real stdio MCP demo server."""
import logging, os, sys
from pathlib import Path
from unittest.mock import Mock

from data_designer.config.mcp import LocalStdioMCPProvider, ToolConfig
from data_designer.engine.mcp.facade import MCPFacade
from data_designer.engine.model_provider import MCPProviderRegistry
from data_designer.engine.models.clients.types import AssistantMessage, ChatCompletionResponse, ToolCall

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
src = Path.cwd() / "tests_e2e" / "src"
provider = LocalStdioMCPProvider(name="demo-mcp", command=sys.executable,
    args=["-m", "data_designer_e2e_tests.mcp_demo_server"], env={"PYTHONPATH": str(src)})
kwargs = {"unknown_tool_fallback": True} if "--fallback" in sys.argv else {}
tool_config = ToolConfig(tool_alias="demo-tools", providers=["demo-mcp"], **kwargs)
print(f"ToolConfig: {tool_config.model_dump(exclude={'tool_alias', 'providers'})}")
facade = MCPFacade(tool_config=tool_config, secret_resolver=Mock(),
    mcp_provider_registry=MCPProviderRegistry(providers=[provider]))
response = ChatCompletionResponse(message=AssistantMessage(content=None, tool_calls=[
    ToolCall(id="c1", name="get_fact", arguments_json='{"topic": "mcp"}'),
    ToolCall(id="c2", name="no_such_tool", arguments_json="{}"),
]))
print("model turn: c1=get_fact(topic='mcp'), c2=no_such_tool()")
try:
    messages = facade.process_completion_response(response)
except Exception as exc:
    print(f"RECORD DROPPED -> {type(exc).__name__}: {exc}")
    sys.exit(1)
for m in messages[1:]:
    print(f"tool message {m.tool_call_id}: {m.content}")
print("RECORD KEPT")
