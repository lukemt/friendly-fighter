"""The MCP transport's fast path: one JSON-RPC POST per tool call over a
kept-open connection (altruagent/mcp_transport.py, friendly-fighter).

The server is an httpx.MockTransport; nothing touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from altruagent import mcp_transport
from altruagent.errors import is_transient_error
from altruagent.mcp_transport import MCPToolError, call_tool

URL = "https://gameapi.example/mcp"


class FakeClient:
    def __init__(self) -> None:
        self.tokens = ["tok-1", "tok-2"]
        self.relogins = 0

    def _current_access_token(self, force_relogin: bool = False) -> str:
        if force_relogin:
            self.relogins += 1
        return self.tokens[min(self.relogins, len(self.tokens) - 1)]


def rpc_result(payload: dict, *, is_error: bool = False) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload, "isError": is_error}}


@pytest.fixture
def server(monkeypatch):
    """Install a mock server; returns the list of requests it saw and a setter for its handler."""
    seen: list[httpx.Request] = []
    state = {"handler": lambda request: httpx.Response(200, json=rpc_result({"ok": True}))}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["handler"](request)

    monkeypatch.setattr(mcp_transport, "_raw_client", httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(mcp_transport, "_raw_disabled", False)

    def set_handler(fn):
        state["handler"] = fn

    return seen, set_handler


def test_one_post_per_call_with_no_initialize(server):
    seen, set_handler = server
    set_handler(lambda r: httpx.Response(200, json=rpc_result({"state_version": 7})))
    assert call_tool(FakeClient(), URL, "get_game_state", {"session_id": "s"}) == {"state_version": 7}
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["method"] == "tools/call" and body["params"] == {"name": "get_game_state", "arguments": {"session_id": "s"}}
    assert seen[0].headers["authorization"] == "Bearer tok-1"
    assert seen[0].headers["accept"] == "application/json, text/event-stream"


def test_text_content_is_used_without_structured_content(server):
    _, set_handler = server
    set_handler(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": '{"a": 1}'}], "isError": False}}))
    assert call_tool(FakeClient(), URL, "get_game_state", {}) == {"a": 1}


def test_sse_reply_is_parsed(server):
    _, set_handler = server
    sse = "event: message\ndata: " + json.dumps(rpc_result({"phase": "playing"})) + "\n\n"
    set_handler(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    assert call_tool(FakeClient(), URL, "get_game_state", {}) == {"phase": "playing"}


def test_game_errors_keep_their_code(server):
    _, set_handler = server
    set_handler(lambda r: httpx.Response(200, json=rpc_result(
        {"error": "STALE_STATE", "detail": "old", "next_actions": [{"tool": "get_game_state"}]})))
    with pytest.raises(MCPToolError) as info:
        call_tool(FakeClient(), URL, "play_action", {})
    assert info.value.error_code == "STALE_STATE"
    assert not is_transient_error(info.value)


def test_tool_is_error_is_a_protocol_error(server):
    _, set_handler = server
    set_handler(lambda r: httpx.Response(200, json=rpc_result({"x": 1}, is_error=True)))
    with pytest.raises(MCPToolError) as info:
        call_tool(FakeClient(), URL, "play_action", {})
    assert info.value.protocol_error


def test_401_relogs_in_once_and_retries(server):
    seen, set_handler = server
    set_handler(lambda r: httpx.Response(401) if r.headers["authorization"] == "Bearer tok-1"
                else httpx.Response(200, json=rpc_result({"ok": 1})))
    client = FakeClient()
    assert call_tool(client, URL, "get_game_state", {}) == {"ok": 1}
    assert client.relogins == 1 and len(seen) == 2


def test_5xx_and_network_errors_are_transient(server, monkeypatch):
    _, set_handler = server
    set_handler(lambda r: httpx.Response(503, text="busy"))
    with pytest.raises(MCPToolError) as info:
        call_tool(FakeClient(), URL, "get_game_state", {})
    assert info.value.status_code == 503 and is_transient_error(info.value)

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    set_handler(boom)
    with pytest.raises(MCPToolError) as info:
        call_tool(FakeClient(), URL, "get_game_state", {})
    assert info.value.status_code is None and is_transient_error(info.value)


def test_a_refused_raw_call_falls_back_to_the_sdk_path_for_good(server, monkeypatch):
    seen, set_handler = server
    set_handler(lambda r: httpx.Response(400, text="initialize first"))
    sdk_calls = []

    async def fake_sdk(mcp_url, token, name, arguments, *, httpx_client_factory=None):
        sdk_calls.append(name)
        return {"via": "sdk"}

    monkeypatch.setattr(mcp_transport, "_call_tool_once", fake_sdk)
    assert call_tool(FakeClient(), URL, "get_game_state", {}) == {"via": "sdk"}
    assert call_tool(FakeClient(), URL, "get_game_state", {}) == {"via": "sdk"}
    assert len(seen) == 1 and sdk_calls == ["get_game_state", "get_game_state"]
