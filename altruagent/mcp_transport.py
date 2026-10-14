"""Synchronous bridge to Agent_ACP's MCP gameplay server, built on the
OFFICIAL `mcp` Python SDK client — not a hand-rolled JSON-RPC-over-HTTP
implementation (see "why" below).

History: an earlier version of this module hand-rolled the JSON-RPC 2.0
envelope directly over ``AltruAgentClient.request()``. It was replaced with
the official ``mcp.client.streamable_http.streamablehttp_client`` +
``mcp.ClientSession`` pattern Agent_ACP's own scripts use, which removes any
doubt about hand-rolled-protocol correctness. (The "Invalid Host header"
HTTP 421 first seen against the deployed server was a platform-side
allowed-hosts issue, since fixed in Agent_ACP; no client change was needed.)

Sync bridge design: the official SDK is async-only, but the rest of this
starter (``altruagent.runner``, ``altruagent.worker``, etc.) is deliberately
synchronous — turning that into asyncio-based code was explicitly ruled
out. Each call here therefore opens a **fresh** connection + MCP session
for that ONE tool call, via ``asyncio.run(...)``:

    asyncio.run() -> streamablehttp_client (fresh HTTP connection)
                  -> ClientSession -> initialize() -> call_tool() -> close

rather than keeping one long-lived async session open across an entire
match (which could span many minutes between turns). This is deliberate,
not a shortcut:

- The server is ``stateless_http=True`` — confirmed in server.py, this
  means the server itself holds no per-connection session state between
  requests, so there is no server-side affinity to preserve across calls.
- ``asyncio.run()`` is documented-safe to call repeatedly, sequentially, in
  the same process (each call creates and cleanly tears down its own event
  loop) — the only hard restriction is not calling it from *inside* an
  already-running loop, which never happens here since nothing else in this
  process runs asyncio.
- This sidesteps every cross-call event-loop-lifetime question entirely
  (Windows' default Proactor loop, multiprocessing ``spawn`` workers each
  starting with no inherited loop state, a match potentially idling a long
  time between turns) — there is no persistent
  loop or connection to keep alive, reconnect, or leak.
- The cost is one extra TCP handshake + MCP ``initialize`` round-trip per
  tool call versus a kept-open session. For a turn-based game polling at
  most a few times a second, this is immaterial.

JWT refresh: this bypasses ``AltruAgentClient.request()`` (the official SDK
owns its own HTTP transport), so the request()-level one-retry-after-401
logic can't be reused directly — but the *policy* is preserved exactly,
through ``AltruAgentClient._current_access_token()`` (same client, same
auth state, not a second auth system): fetch the current token, try once;
on a 401 (surfaced by the SDK's own ``response.raise_for_status()`` as
``httpx.HTTPStatusError`` — confirmed in mcp/client/streamable_http.py),
force a fresh login and retry exactly once, exactly like ``request()``.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx

try:  # orjson, when installed, parses a game state several times faster (same result)
    import orjson as _orjson
except ImportError:  # pragma: no cover
    _orjson = None
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client
from pydantic_core import PydanticSerializationError

from .errors import PlatformError

if TYPE_CHECKING:
    from .client import AltruAgentClient

# How long one tool call may take. The SDK's own defaults are 30 s to connect
# and 300 s to read, so a connection that silently died (a Wi-Fi or VPN
# switch, a laptop waking up) would freeze the agent for five minutes. The
# longest healthy call is wait_for_update: the server holds it at most 25 s
# (GameAPI's WAIT_MAX_TIMEOUT_SECONDS), so the read timeout stays well above
# that plus network time. The session timeout is a backstop for an answer
# that trickles in without ever finishing. A call that times out raises
# MCPToolError (no error code), which the runner retries in place.
MCP_CONNECT_TIMEOUT_SECONDS = 10.0
MCP_READ_TIMEOUT_SECONDS = 40.0
MCP_SESSION_TIMEOUT_SECONDS = 45.0


class MCPToolError(PlatformError):
    """A tool call reached the server but failed — either a structured
    application-level error the tool itself returned (``error_code`` is one
    of MCP's own codes, e.g. ``STALE_STATE``/``GAME_ALREADY_COMPLETE``), or a
    protocol/transport-level failure (``error_code`` is ``None`` in that
    case — including an HTTP-level rejection, surfaced with its
    ``status_code``).

    ``protocol_error`` is True when the server answered, but with an MCP tool
    error (``isError``) rather than a result: the tool's arguments failed
    validation, or the tool itself crashed. GameAPI sends every game error
    as a normal result with an ``error`` code, so this is a call the server
    can't handle, likely to fail the same way again. It is not a connection
    problem (``errors.is_transient_error`` is False for it).

    ``local_error`` is True when the call never left this process: its
    arguments can't be sent as JSON (for example a numpy number, bytes, or an
    object of the agent's own class inside a move). Neither a connection nor
    a server problem; sending the same arguments again fails the same way
    (``errors.is_transient_error`` is False for it too).
    """

    def __init__(
        self, message: str, *, protocol_error: bool = False, local_error: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.protocol_error = protocol_error
        self.local_error = local_error


def _unsendable_arguments(name: str, exc: BaseException) -> MCPToolError:
    return MCPToolError(
        f"MCP tool {name!r} can't send its arguments as JSON: {exc}",
        status_code=None,
        error_code=None,
        local_error=True,
    )


def _check_arguments(name: str, arguments: dict) -> None:
    """Raise ``MCPToolError(local_error=True)`` if the SDK couldn't send
    ``arguments``: the same JSON conversion ``ClientSession.call_tool`` does,
    done before any connection is opened. Inside the SDK that failure would
    surface wrapped in anyio ExceptionGroups, indistinguishable from a
    dropped connection.
    """
    try:
        types.CallToolRequestParams(name=name, arguments=arguments).model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
    except ValueError as exc:  # PydanticSerializationError, UnicodeDecodeError, ValidationError
        raise _unsendable_arguments(name, exc) from exc


def _find_serialization_error(exc: BaseException) -> PydanticSerializationError | None:
    """A ``PydanticSerializationError`` anywhere in anyio's ExceptionGroup
    wrapping: the SDK failed to turn this call's arguments into JSON (a
    backstop for ``_check_arguments``)."""
    if isinstance(exc, PydanticSerializationError):
        return exc
    for sub in getattr(exc, "exceptions", None) or ():
        found = _find_serialization_error(sub)
        if found is not None:
            return found
    return None


async def _call_tool_once(
    mcp_url: str, token: str, name: str, arguments: dict, *, httpx_client_factory=None
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    kwargs: dict[str, Any] = {
        "headers": headers,
        # The SDK builds httpx.Timeout(timeout, read=sse_read_timeout) from these.
        "timeout": MCP_CONNECT_TIMEOUT_SECONDS,
        "sse_read_timeout": MCP_READ_TIMEOUT_SECONDS,
    }
    if httpx_client_factory is not None:
        # Test-only seam: lets unit tests exercise the real streamablehttp_client
        # / ClientSession code paths (JSON-RPC framing, error unwrapping) against
        # an httpx.MockTransport-backed AsyncClient instead of real sockets,
        # rather than replacing the SDK's classes with hand-built fakes.
        kwargs["httpx_client_factory"] = httpx_client_factory
    async with streamablehttp_client(mcp_url, **kwargs) as (read, write, _):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=MCP_SESSION_TIMEOUT_SECONDS)
        ) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)

    if result.isError:
        raise MCPToolError(
            f"MCP tool {name!r} failed at the protocol level: {_extract_text(result)}",
            status_code=None,
            error_code=None,
            protocol_error=True,
        )

    payload = result.structuredContent
    if payload is None:
        text = _extract_text(result)
        try:
            payload = json.loads(text) if text else {}
        except ValueError:
            payload = {}

    if isinstance(payload, dict) and "error" in payload:
        raise MCPToolError(
            payload.get("detail") or f"MCP tool {name!r} returned error {payload['error']!r}.",
            status_code=None,
            error_code=payload["error"],
            next_action=(payload.get("next_actions") or [None])[0],
        )

    return payload if isinstance(payload, dict) else {}


def _extract_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text is not None:
            return str(text)
    return str(result)


def _find_http_status_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    """Unwrap anyio's ExceptionGroup wrapping.

    Confirmed empirically (not assumed): ``streamablehttp_client`` runs its
    read/write loops inside ``anyio.create_task_group()``
    (mcp/client/streamable_http.py), so an HTTP-level failure — including
    the "Invalid Host header" 421 this module's docstring describes — never
    arrives as a bare ``httpx.HTTPStatusError``; it's always wrapped in an
    ``ExceptionGroup``/``BaseExceptionGroup``, one or more levels deep. A
    plain ``except httpx.HTTPStatusError`` here would silently never match,
    which would have made the 401-retry-once behavior in ``call_tool`` dead
    code — probed directly against a real ``httpx.MockTransport`` before
    trusting this, not inferred from documentation.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc
    for sub in getattr(exc, "exceptions", None) or ():
        found = _find_http_status_error(sub)
        if found is not None:
            return found
    return None


def _wrap_http_status_error(name: str, exc: httpx.HTTPStatusError) -> MCPToolError:
    """Build a clean ``MCPToolError`` from an HTTP-level failure.

    ``exc.response`` is a *streaming* response: the SDK's own transport
    (``mcp.client.streamable_http``'s ``stream_within_origin``) calls
    ``raise_for_status()`` while the response is still open, then closes it
    via its own ``finally: await response.aclose()`` as this exception
    unwinds — before it ever reaches ``call_tool()``'s except clause, and
    without the body ever having been read. Accessing ``.text``/``.content``
    on a closed, never-read streaming response raises
    ``httpx.ResponseNotRead`` — confirmed empirically (a real 421/401
    response body is fully drained by the time it gets here) — which would
    otherwise mask the original HTTP status behind an unrelated crash. We
    have no way to read the body earlier (that would mean patching the SDK's
    own transport), so fall back to ``str(exc)`` instead: httpx's own
    ``HTTPStatusError`` message already includes the status code, reason
    phrase, and URL, which is enough to act on even without the body.
    """
    try:
        body = exc.response.text
        detail = body[:500] if body else str(exc)
    except httpx.ResponseNotRead:
        detail = str(exc)
    return MCPToolError(
        f"MCP tool {name!r} request failed with HTTP {exc.response.status_code}: {detail}",
        status_code=exc.response.status_code,
        error_code=None,
    )


# --- Fast path: one JSON-RPC POST per call over a kept-open connection ---------------
#
# friendly-fighter: the SDK path above opens a fresh connection and MCP session
# for every call (TCP + TLS + initialize + call), which measured ~1.9 s per call
# from a European host: a real-time Red Alert agent then acts only every ~2 s.
# GameAPI is stateless, and its docs (skill/mcp.md, "Raw JSON-RPC") describe a
# single POST with no initialize as supported, recommending one kept-alive HTTP
# client. That is this path: one round trip per call. Errors map to exactly the
# same MCPToolError shapes as the SDK path. If the server ever refuses the raw
# form (a 4xx other than 401/429, or a JSON-RPC protocol error), this process
# falls back to the SDK path for good. FF_MCP_TRANSPORT=sdk forces the SDK path.

_raw_client: httpx.Client | None = None
_raw_lock = threading.Lock()
_raw_disabled = os.environ.get("FF_MCP_TRANSPORT", "").lower() == "sdk"
_raw_ids = iter(range(1, 1 << 62))


class _RawUnsupported(Exception):
    """The server answered the raw JSON-RPC form in a way that suggests it
    doesn't accept it; the caller falls back to the SDK path."""


def _shared_raw_client() -> httpx.Client:
    global _raw_client
    with _raw_lock:
        if _raw_client is None:
            _raw_client = httpx.Client(
                timeout=httpx.Timeout(MCP_READ_TIMEOUT_SECONDS, connect=MCP_CONNECT_TIMEOUT_SECONDS),
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=60.0),
                http2=False,
            )
        return _raw_client


def _parse_rpc_body(response: httpx.Response) -> dict:
    """The JSON-RPC envelope from a plain JSON reply or a one-event SSE reply."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        text = response.text
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
        envelope = json.loads(text) if text else {}
    else:
        raw = response.content
        envelope = (_orjson.loads(raw) if _orjson is not None else json.loads(raw)) if raw.strip() else {}
    if not isinstance(envelope, dict):
        raise ValueError("JSON-RPC reply is not an object")
    return envelope


def _payload_from_result(name: str, result: Any) -> dict:
    if not isinstance(result, dict):
        return {}
    if result.get("isError"):
        content = result.get("content") or [{}]
        text = content[0].get("text") if isinstance(content[0], dict) else None
        raise MCPToolError(
            f"MCP tool {name!r} failed at the protocol level: {text or result}",
            status_code=None,
            error_code=None,
            protocol_error=True,
        )
    payload = result.get("structuredContent")
    if payload is None:
        content = result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        try:
            payload = json.loads(text) if text else {}
        except ValueError:
            payload = {}
    if isinstance(payload, dict) and "error" in payload:
        raise MCPToolError(
            payload.get("detail") or f"MCP tool {name!r} returned error {payload['error']!r}.",
            status_code=None,
            error_code=payload["error"],
            next_action=(payload.get("next_actions") or [None])[0],
        )
    return payload if isinstance(payload, dict) else {}


def _call_tool_raw(http: httpx.Client, mcp_url: str, token: str, name: str, arguments: dict) -> dict:
    """One tools/call POST. Raises MCPToolError like the SDK path, httpx's
    HTTPStatusError for a 401 (the caller re-logs in), or _RawUnsupported."""
    body = {"jsonrpc": "2.0", "id": next(_raw_ids), "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    try:
        response = http.post(
            mcp_url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    except httpx.HTTPError as exc:  # connect/read timeouts, dropped connections
        raise MCPToolError(f"MCP tool {name!r} failed: {exc!r}", status_code=None, error_code=None) from exc
    status = response.status_code
    if status == 401:
        raise httpx.HTTPStatusError("401 Unauthorized", request=response.request, response=response)
    if status >= 500 or status in (408, 429):
        raise MCPToolError(
            f"MCP tool {name!r} request failed with HTTP {status}: {response.text[:500]}",
            status_code=status,
            error_code=None,
        )
    if status >= 400:
        raise _RawUnsupported(f"HTTP {status}: {response.text[:200]}")
    try:
        envelope = _parse_rpc_body(response)
    except ValueError as exc:
        raise _RawUnsupported(f"unreadable reply: {exc}") from exc
    if "error" in envelope and "result" not in envelope:
        raise _RawUnsupported(f"JSON-RPC error: {str(envelope['error'])[:200]}")
    return _payload_from_result(name, envelope.get("result"))


def _call_tool_fast(client: "AltruAgentClient", mcp_url: str, name: str, arguments: dict) -> dict:
    http = _shared_raw_client()
    token = client._current_access_token()
    try:
        return _call_tool_raw(http, mcp_url, token, name, arguments)
    except httpx.HTTPStatusError:
        token = client._current_access_token(force_relogin=True)
        try:
            return _call_tool_raw(http, mcp_url, token, name, arguments)
        except httpx.HTTPStatusError as exc:
            raise MCPToolError(
                f"MCP tool {name!r} request failed with HTTP 401 after re-login",
                status_code=401,
                error_code=None,
            ) from exc


# -- friendly-fighter: wire log ------------------------------------------------------
# Every tool call and its answer (or error), with send/receive times, goes to
# recordings/<session_id>/wire-<pid>.jsonl.gz (one worker process = one seat).
# Off with FF_RECORD=0 or FF_WIRE=0.

_WIRE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings")
_wire_lock = threading.Lock()
_wire_files: dict[str, Any] = {}
_wire_pending = 0
last_endpoint: tuple[Any, str] | None = None  # (client, mcp_url) of the latest call, for agent/probe.py


def _wire_on() -> bool:
    return os.environ.get("FF_RECORD", "1") != "0" and os.environ.get("FF_WIRE", "1") != "0"


def _wire_close() -> None:
    with _wire_lock:
        for handle in _wire_files.values():
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        _wire_files.clear()


def wire_log(record: dict) -> None:
    """Append one record to this process's wire log (never raises)."""
    global _wire_pending
    if not _wire_on():
        return
    session = str(record.get("session_id") or (record.get("arguments") or {}).get("session_id") or "no-session")
    session = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session)[:80]
    try:
        import gzip

        with _wire_lock:
            handle = _wire_files.get(session)
            if handle is None:
                folder = os.path.join(_WIRE_ROOT, session)
                os.makedirs(folder, exist_ok=True)
                handle = gzip.open(os.path.join(folder, f"wire-{os.getpid()}.jsonl.gz"), "ab")
                if not _wire_files:
                    import atexit

                    atexit.register(_wire_close)
                _wire_files[session] = handle
            handle.write(json.dumps(record, separators=(",", ":"), default=repr).encode() + b"\n")
            _wire_pending += 1
            if _wire_pending >= 25 or record.get("tool") in ("get_result", "resign"):
                handle.flush()
                _wire_pending = 0
            answer = record.get("answer")
            if isinstance(answer, dict) and answer.get("is_terminal"):
                handle.flush()
    except Exception:  # noqa: BLE001 - logging must never break play
        pass


def call_tool(
    client: "AltruAgentClient",
    mcp_url: str,
    name: str,
    arguments: dict,
    *,
    httpx_client_factory=None,
) -> dict:
    """``_call_tool`` (below) plus the friendly-fighter wire log."""
    global last_endpoint
    import time as _time

    last_endpoint = (client, mcp_url)
    sent = _time.time()
    try:
        answer = _call_tool(client, mcp_url, name, arguments, httpx_client_factory=httpx_client_factory)
    except MCPToolError as exc:
        wire_log({"t": round(sent, 3), "dt": round(_time.time() - sent, 3), "tool": name, "arguments": arguments,
                  "error": getattr(exc, "error_code", None), "detail": str(exc)[:2000],
                  "status_code": getattr(exc, "status_code", None)})
        raise
    wire_log({"t": round(sent, 3), "dt": round(_time.time() - sent, 3), "tool": name, "arguments": arguments,
              "answer": answer})
    return answer


def _call_tool(
    client: "AltruAgentClient",
    mcp_url: str,
    name: str,
    arguments: dict,
    *,
    httpx_client_factory=None,
) -> dict:
    """Call one MCP tool and return its parsed result payload as a plain dict.

    ``mcp_url`` is ``{game_server_url}/mcp`` (see ``MCPGameSession``). Raises
    ``MCPToolError`` (a ``PlatformError`` subclass) for both application-level
    tool errors and transport/HTTP-level failures, so callers only need one
    except clause. A 401 triggers exactly one re-login + retry, reusing
    ``AltruAgentClient``'s own auth state — see the module docstring.

    ``httpx_client_factory`` is a test-only seam (see ``_call_tool_once``) —
    production callers never pass it.
    """
    global _raw_disabled
    _check_arguments(name, arguments)
    if httpx_client_factory is None and not _raw_disabled:
        try:
            return _call_tool_fast(client, mcp_url, name, arguments)
        except _RawUnsupported as exc:
            _raw_disabled = True
            from .console import print_line

            print_line(f"[mcp] raw JSON-RPC calls refused ({exc}); using full MCP sessions from now on")
    token = client._current_access_token()
    try:
        return asyncio.run(
            _call_tool_once(mcp_url, token, name, arguments, httpx_client_factory=httpx_client_factory)
        )
    except MCPToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately broad: network errors,
        # anyio ExceptionGroups, or any other mcp-SDK-internal failure should
        # all surface as an AltruAgentError, not a raw third-party exception
        # leaking out of this SDK. _find_http_status_error unwraps whatever
        # anyio's task-group wrapping did, so the 401 check below still works
        # regardless of how many ExceptionGroup layers deep it's nested.
        serialization_exc = _find_serialization_error(exc)
        if serialization_exc is not None:
            raise _unsendable_arguments(name, serialization_exc) from exc
        http_exc = _find_http_status_error(exc)
        if http_exc is None:
            raise MCPToolError(
                f"MCP tool {name!r} failed: {exc!r}", status_code=None, error_code=None
            ) from exc
        if http_exc.response.status_code != 401:
            raise _wrap_http_status_error(name, http_exc) from exc

        token = client._current_access_token(force_relogin=True)
        try:
            return asyncio.run(
                _call_tool_once(mcp_url, token, name, arguments, httpx_client_factory=httpx_client_factory)
            )
        except MCPToolError:
            raise
        except Exception as exc2:  # noqa: BLE001
            http_exc2 = _find_http_status_error(exc2)
            if http_exc2 is not None:
                raise _wrap_http_status_error(name, http_exc2) from exc2
            raise MCPToolError(
                f"MCP tool {name!r} failed after re-login: {exc2!r}", status_code=None, error_code=None
            ) from exc2
