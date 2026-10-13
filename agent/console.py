"""Console seat: lets a developer (or Claude) drive one seat live from scripts.

Turned on by ``remote/console.json`` (``scripts/ffc.py on --seat 0``), read
once at a match's first view. The seat's worker process then serves, on
127.0.0.1 only (port 8770 + seat):

- ``GET  /creds``  -> {mcp_url, token, session_id, seat}: the seat's current
  GameAPI access token, so a script can make its own MCP calls with any
  timing (parallel calls, delays, loops);
- ``POST /call``   {method?, tool?, arguments?, params?} -> the raw answer of
  one MCP call made by this process (also in the wire log);
- ``GET  /bot``, ``POST /bot`` {"on": bool} -> whether the scripted bot keeps
  playing this seat (off: the seat only reads views, so a script's
  play_action calls never collide with the bot's).

The address is written to ``remote/console-seat<N>.json``. Nothing here is
reachable from outside the machine. ``scripts/ffc.py`` is the client.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from altruagent import WAIT
from altruagent import mcp_transport
from altruagent.console import print_line

ROOT = Path(__file__).resolve().parent.parent / "remote"
BASE_PORT = 8770


def read_console_control(path: Path | None = None) -> dict:
    try:
        return json.loads((path or ROOT / "console.json").read_text())
    except (OSError, ValueError):
        return {}


class ConsoleSeat:
    def __init__(self, bot, context, *, bot_on: bool = True, root: Path = ROOT, port: int | None = None,
                 caller=None) -> None:
        self.bot = bot
        self.sid = context.session_id
        self.seat = context.seat_position
        self.bot_on = bot_on
        self._caller = caller  # a Probe (its .rpc / .tool), or None to build one lazily
        self.server = ThreadingHTTPServer(("127.0.0.1", port if port is not None else BASE_PORT + int(self.seat or 0)),
                                          self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        try:
            root.mkdir(exist_ok=True)
            (root / f"console-seat{self.seat}.json").write_text(
                json.dumps({"url": self.address, "session_id": self.sid, "seat": self.seat}))
        except OSError:
            pass
        print_line(f"[console] seat {self.seat} at {self.address} (bot {'on' if bot_on else 'off'})")

    # -- the runtime's hooks ---------------------------------------------------------

    def choose_action(self, state, context):
        return self.bot.choose_action(state, context) if self.bot_on else WAIT

    def on_action_result(self, result, context) -> None:
        self.bot.on_action_result(result, context)

    def close(self) -> None:
        self.server.shutdown()

    # -- HTTP -------------------------------------------------------------------------

    def _creds(self) -> dict:
        if mcp_transport.last_endpoint is None:
            return {"error": "no MCP call seen yet"}
        client, url = mcp_transport.last_endpoint
        return {"mcp_url": url, "token": client._current_access_token(), "session_id": self.sid, "seat": self.seat}

    def _call(self, body: dict) -> dict:
        if self._caller is None:
            from .probe import Probe  # its rpc() is the raw call we want

            self._caller = Probe.__new__(Probe)
            self._caller.sid = self.sid
            import httpx

            self._caller.http = httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0))
        if body.get("tool"):
            arguments = {"session_id": self.sid, **(body.get("arguments") or {})}
            return self._caller.rpc("tools/call", {"name": body["tool"], "arguments": arguments})
        return self._caller.rpc(body.get("method") or "tools/list", body.get("params") or {})

    def _handler(self):
        seat = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the agent log clean
                pass

            def _send(self, code: int, data) -> None:
                raw = json.dumps(data, default=repr).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                if self.path == "/creds":
                    self._send(200, seat._creds())
                elif self.path == "/bot":
                    self._send(200, {"on": seat.bot_on})
                else:
                    self._send(404, {"error": "unknown path"})

            def do_POST(self):
                body = self._body()
                try:
                    if self.path == "/call":
                        self._send(200, seat._call(body))
                    elif self.path == "/bot":
                        seat.bot_on = bool(body.get("on"))
                        print_line(f"[console] seat {seat.seat}: bot {'on' if seat.bot_on else 'off'}")
                        self._send(200, {"on": seat.bot_on})
                    else:
                        self._send(404, {"error": "unknown path"})
                except Exception as exc:  # noqa: BLE001
                    self._send(500, {"error": repr(exc)})

        return Handler
