"""The local platform: control plane + GameAPI MCP endpoint + an admin API, on one port.

Our unchanged runtime (``python -m agent --match``) talks to it like to the hosted
platform when started with ``ALTRUAGENT_CONTROL_URL=http://127.0.0.1:<port>`` and any
well-formed ``ALTRUAGENT_OFFICIAL_AGENT_KEY`` (``eak_live_`` + 64 hex; a local key never
leaves this machine):

- ``POST /tournament/agent/authenticate``, ``GET /tournament/agent/assignments``,
  ``POST /tournament/agent/assignments/<seat>/grant`` and ``.../lease/renew``;
- ``POST /mcp``: JSON-RPC (``initialize``, ``tools/list``, ``tools/call``, ``ping``) with
  the 11 tools of the hosted GameAPI and their recorded schemas;
- admin (``/sim/...``): create a match (``POST /sim/matches``), list and inspect them.

A match runs in real time (the engine at 25 ticks/s, the round trip slept) or in virtual
time (``ffsim.vclock``: the engine steps as fast as the players allow, every wait is virtual).
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .gameapi import PLATFORM_TIMING, Match, SeatConfig, ToolError
from .vclock import platform_rtts
from .rules import Rules, list_games, tools_list

KEEP_FINISHED_SECONDS = 300
LIST_FINISHED_SECONDS = 30


def agent_id_for(key: str, position: int) -> str:
    return str(uuid.UUID(hashlib.sha256(f"{key}:{position}".encode()).hexdigest()[:32]))


class Platform:
    def __init__(self, engine_factory, *, results_dir: Path | None = None, rtt_ms: float | str = 0.0,
                 time_mode: str = "real", think: str | float = "measured", timing: dict | None = None,
                 seed: int | None = None) -> None:
        self.engine_factory = engine_factory  # (match, seat_kinds, lobby) -> engine
        self.results_dir = results_dir
        # Simulated network round trip for /mcp calls: half before the call is handled (the batch
        # "arrives" later), half before the answer leaves. Our real round trip to the platform was
        # 0.19 s (get_game_state / play_action medians from the wire logs).
        # "platform": draw each call's round trip from the platform's measured ones.
        self.rtts = platform_rtts() if rtt_ms == "platform" else [float(rtt_ms) / 1000.0]
        self.rtt_rng = random.Random(seed)
        self.timing = PLATFORM_TIMING if timing is None else timing
        self.seed = seed
        self.created = 0
        self.time_mode = time_mode  # "real" or "virtual" (the default for new matches)
        self.think = think  # virtual time: the agents' think time ("measured" or seconds)
        self.lock = threading.RLock()
        self.matches: dict[str, Match] = {}
        self.match_meta: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}  # agent session token -> key
        self.seat_tokens: dict[str, tuple[str, int]] = {}  # seat token -> (session_id, position)
        self.on_finish = []
        threading.Thread(target=self._housekeeping, name="ffsim-housekeeping", daemon=True).start()

    # -- admin --------------------------------------------------------------------------

    def create_match(self, *, map_id: str = "trapped", max_minutes: int = 20, seats: list[dict],
                     lobby: dict | None = None, label: str = "", time_mode: str | None = None,
                     think: str | float | None = None) -> dict:
        if len(seats) != 2:
            raise ValueError("Red Alert needs exactly 2 seats")
        session_id = "sim-" + str(uuid.uuid4())
        configs, keys = [], []
        for pos, s in enumerate(seats):
            kind = s.get("kind", "agent")
            key = s.get("key") or ""
            token = "seat_" + secrets.token_hex(16) if kind == "agent" else ""
            agent_id = agent_id_for(key, pos) if kind == "agent" else f"openra-{kind.replace(':', '-')}"
            name = s.get("name") or (f"{s.get('label') or 'local'} (Player{pos})" if kind == "agent" else f"OpenRA {kind[3:]} AI")
            configs.append(SeatConfig(kind=kind, agent_id=agent_id, name=name, token=token))
            keys.append(key)
        kinds = [c.kind for c in configs]
        factory = self.engine_factory
        time_mode = time_mode or self.time_mode
        if time_mode not in ("real", "virtual"):
            raise ValueError("time_mode is real or virtual")
        virtual = ({"rtt": self.rtts, "think": self.think if think is None else think}
                   if time_mode == "virtual" else None)
        self.created += 1
        seed = None if self.seed is None else self.seed * 1000 + self.created
        match = Match(session_id, map_id, configs, lambda m: factory(m, kinds, lobby), max_minutes=max_minutes,
                      virtual=virtual, timing=self.timing, seed=seed)
        with self.lock:
            self.matches[session_id] = match
            self.match_meta[session_id] = {"keys": keys, "label": label, "created": time.time(),
                                           "seats": seats, "map": map_id, "max_minutes": max_minutes,
                                           "time_mode": time_mode}
            for pos, c in enumerate(configs):
                if c.token:
                    self.seat_tokens[c.token] = (session_id, pos)
        return self.describe(session_id)

    def describe(self, session_id: str) -> dict:
        match = self.matches[session_id]
        meta = self.match_meta[session_id]
        with match.lock:
            return {"session_id": session_id, "map": match.map_id, "max_minutes": match.max_minutes,
                    "phase": match.phase, "label": meta.get("label"),
                    "seats": [{"seat": s.name, "kind": s.config.kind, "agent_id": s.config.agent_id,
                               "name": s.config.name, "checked_in": s.checked_in, "faction": s.faction,
                               "tiebreak": list(match.tiebreak_score(s)) if s.obs or not s.is_agent else None,
                               "military": (s.obs or {}).get("military")} for s in match.seats],
                    "game_seconds": round(match.game_seconds(), 1), "result": match.result,
                    "termination_reason": match.termination_reason, "failure_reason": match.failure_reason,
                    "time_mode": meta.get("time_mode", "real"),
                    "clock": match.vclock.report() if match.vclock is not None else None,
                    "engine": _engine_report(match.engine),
                    "log": match.log[-20:]}

    def cancel(self, session_id: str) -> None:
        self.matches[session_id].fail("cancelled")

    def _housekeeping(self) -> None:
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            with self.lock:
                items = list(self.matches.items())
            for sid, match in items:
                match.tick()
                if match.phase == "finished" and not self.match_meta[sid].get("reported"):
                    self.match_meta[sid]["reported"] = True
                    self._report(sid)
                if match.finished_at is not None and now - match.finished_at > KEEP_FINISHED_SECONDS:
                    with self.lock:
                        self.matches.pop(sid, None)

    def _report(self, session_id: str) -> None:
        info = self.describe(session_id)
        if "started_wall" in self.match_meta[session_id]:
            info["_started"] = self.match_meta[session_id]["started_wall"]
            info["wall_seconds"] = round(time.monotonic() - info["_started"], 1)
        info["reasoning"] = self.matches[session_id].reasoning[-200:]
        if self.results_dir is not None:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            (self.results_dir / f"{session_id}.json").write_text(json.dumps(info, indent=1))
        for callback in list(self.on_finish):
            try:
                callback(info)
            except Exception:  # noqa: BLE001
                pass

    # -- control plane ------------------------------------------------------------------

    def authenticate(self, key: str) -> dict:
        if not (isinstance(key, str) and key.startswith("eak_live_") and len(key) == 73):
            raise PermissionError("invalid_official_agent_key")
        token = "agentsess_" + secrets.token_hex(16)
        with self.lock:
            self.sessions[token] = key
        return {"access_token": token, "expires_in": 3600}

    def clock_for(self, seat_token: str):
        """The virtual clock and seat position of an MCP caller, or (None, None) in real time."""
        where = self.seat_tokens.get(seat_token)
        if where is None:
            return None, None
        match = self.matches.get(where[0])
        if match is None or match.vclock is None:
            return None, None
        return match.vclock, where[1]

    def key_for_session(self, token: str) -> str | None:
        return self.sessions.get(token)

    def assignments(self, key: str) -> list[dict]:
        rows = []
        now = time.monotonic()
        with self.lock:
            for sid, match in self.matches.items():
                if match.finished_at is not None and now - match.finished_at > LIST_FINISHED_SECONDS:
                    continue
                for pos, k in enumerate(self.match_meta[sid]["keys"]):
                    if k == key and match.seats[pos].is_agent:
                        rows.append({"match_id": sid, "seat_id": f"{sid}:{pos}", "game_type": "red_alert",
                                     "seat_position": pos, "seat_count": 2,
                                     "match_status": "finished" if match.phase == "finished" else "active",
                                     "seat_status": "assigned", "context": "testing",
                                     "opponents": [{"name": s.config.name} for s in match.seats if s.position != pos]})
        return rows

    def grant(self, key: str, seat_id: str, base_url: str) -> dict:
        sid, _, pos = seat_id.rpartition(":")
        with self.lock:
            match = self.matches.get(sid)
            if match is None or not pos.isdigit() or self.match_meta[sid]["keys"][int(pos)] != key:
                raise LookupError("assignment_not_found")
            seat = match.seats[int(pos)]
        return {"access_token": seat.config.token, "agent_id": seat.config.agent_id, "game_session_id": sid,
                "gameapi_server_url": base_url, "expires_at": None, "match_id": sid, "seat_id": seat_id,
                "seat_position": seat.position, "seat_count": 2, "game_type": "red_alert", "match_status": "active"}

    # -- GameAPI tools ------------------------------------------------------------------

    def call_tool(self, seat_token: str, name: str, arguments: dict) -> dict:
        """One MCP tool; returns the payload (an error payload for ToolError)."""
        try:
            return self._call_tool(seat_token, name, arguments)
        except ToolError as exc:
            return exc.payload()

    def _seat(self, seat_token: str, session_id) -> tuple[Match, object]:
        found = self.seat_tokens.get(seat_token)
        match = self.matches.get(session_id) if isinstance(session_id, str) else None
        if match is None:
            raise ToolError("SESSION_NOT_FOUND", f"Game session not found: {session_id}", [{
                "tool": "get_agent_status", "hint": (
                    "Use your seat grant's game_session_id as session_id (your Official Agent Key runtime gets one "
                    "grant per game). A session is removed a few minutes after it ends.")}])
        if found is None or found[0] != session_id:
            raise ToolError("AGENT_NOT_IN_SESSION", "This seat's token is for a different match.")
        return match, match.seats[found[1]]

    def _call_tool(self, seat_token: str, name: str, a: dict) -> dict:
        if name == "list_games":
            return list_games()
        if name == "get_game_config":
            if a.get("game_type") != "red_alert":
                raise ToolError("GAME_NOT_FOUND", f"Unknown game type: {a.get('game_type')}")  # unverified
            return Rules.load().config
        if name == "get_agent_status":
            found = self.seat_tokens.get(seat_token)
            agent = None
            if found and found[0] in self.matches:
                agent = self.matches[found[0]].seats[found[1]].config.agent_id
            return {"agent": {"id": agent}, "note": "Local ffsim platform: your games arrive by seat grant.",
                    "next_actions": [{"tool": "get_game_state", "hint": "Read the session your seat grant names."}]}
        match, seat = self._seat(seat_token, a.get("session_id"))
        if name in ("send_message", "get_messages"):
            raise ToolError("RUNTIME_UNAVAILABLE", f"Runtime adapter 'openra' does not support '{name}'")
        match.check_in(seat)
        if name == "get_game_state":
            return match.state(seat)
        if name == "get_legal_actions":
            return match.legal_actions(seat)
        if name == "wait_for_update":
            return match.wait_for_update(seat, a.get("since_version"), a.get("since_is_current_actor"),
                                         a.get("since_phase"), a.get("timeout_seconds", 20.0))
        if name == "play_action":
            if a.get("action") is None and a.get("action_id") is not None:
                raise ToolError("INVALID_ACTION", "Red Alert has no action ids: send action={\"type\": \"orders\", \"orders\": [...]}")
            return match.play_action(seat, a.get("state_version"), a.get("action"), a.get("reasoning_summary"))
        if name == "get_result":
            return match.get_result(seat)
        if name == "resign":
            return match.resign(seat)
        raise KeyError(name)


REQUIRED = {t["name"]: t.get("inputSchema", {}).get("required", []) for t in tools_list()["tools"]}


def _engine_report(engine) -> dict | None:
    if engine is None or not getattr(engine, "lockstep", False):
        return None
    return {"steps": engine.steps, "engine_s": round(engine.engine_us / 1e6, 2),
            "step_wall_s": round(engine.step_wall, 2), "tick": engine.tick}


def _pydantic_repr(value) -> str:
    """pydantic-core's input_value: a repr longer than 50 characters keeps its first 25 and last 24."""
    r = repr(value)
    return r if len(r) <= 50 else r[:25] + "..." + r[-24:]


def _pydantic_missing(tool: str, missing: list[str], arguments: dict) -> str:
    lines = [f"Error executing tool {tool}: {len(missing)} validation error{'s' if len(missing) > 1 else ''} for {tool}Arguments"]
    for m in missing:
        lines += [m, f"  Field required [type=missing, input_value={_pydantic_repr(arguments)}, input_type=dict]",
                  "    For further information visit https://errors.pydantic.dev/2.12/v/missing"]
    return "\n".join(lines)


def make_handler(platform: Platform):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True  # headers and body go out as separate writes: no 40 ms delayed-ACK stall

        def log_message(self, *args):
            pass

        def _send(self, code: int, data=None, content_type: str = "application/json") -> None:
            raw = b"" if data is None else json.dumps(data, separators=(",", ":"), default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {"__invalid__": True}

        def _bearer(self) -> str:
            auth = self.headers.get("Authorization") or ""
            return auth[7:] if auth.startswith("Bearer ") else ""

        def _base_url(self) -> str:
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
            return f"http://{host}"

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/tournament/agent/assignments":
                key = platform.key_for_session(self._bearer())
                if key is None:
                    return self._send(401, {"error": "unauthorized", "detail": "agent session expired"})
                return self._send(200, {"assignments": platform.assignments(key)})
            if path == "/sim/matches":
                with platform.lock:
                    ids = list(platform.matches)
                return self._send(200, {"matches": [platform.describe(i) for i in ids]})
            if path.startswith("/sim/matches/"):
                sid = path.rsplit("/", 1)[1]
                if sid not in platform.matches:
                    return self._send(404, {"error": "not_found"})
                return self._send(200, platform.describe(sid))
            if path == "/mcp":
                return self._send(405, {"error": "method_not_allowed"})
            if path == "/health":
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not_found"})

        def do_DELETE(self):
            path = self.path.split("?")[0]
            if path.startswith("/sim/matches/"):
                sid = path.rsplit("/", 1)[1]
                if sid in platform.matches:
                    platform.cancel(sid)
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not_found"})

        def do_POST(self):
            received = time.monotonic()
            path = self.path.split("?")[0]
            body = self._body()
            if path == "/tournament/agent/authenticate":
                try:
                    return self._send(200, platform.authenticate(body.get("official_agent_key")))
                except PermissionError:
                    return self._send(401, {"error": "invalid_official_agent_key", "detail": "unknown key"})
            if path.startswith("/tournament/agent/assignments/"):
                key = platform.key_for_session(self._bearer())
                if key is None:
                    return self._send(401, {"error": "unauthorized", "detail": "agent session expired"})
                rest = path[len("/tournament/agent/assignments/"):]
                if rest.endswith("/lease/renew"):
                    return self._send(200, {"ok": True, "lease_seconds": 30})
                if rest.endswith("/grant"):
                    try:
                        return self._send(200, platform.grant(key, rest[:-len("/grant")], self._base_url()))
                    except LookupError:
                        return self._send(404, {"error": "assignment_not_found", "detail": "no such seat"})
                return self._send(404, {"error": "not_found"})
            if path == "/sim/matches":
                try:
                    return self._send(200, platform.create_match(**body))
                except (TypeError, ValueError, KeyError) as exc:
                    return self._send(400, {"error": "bad_request", "detail": str(exc)})
            if path == "/mcp":
                clock, position = platform.clock_for(self._bearer())
                if clock is not None:  # virtual time: the round trip is virtual too
                    params = body.get("params") if isinstance(body, dict) else None
                    tool = (params or {}).get("name") if body.get("method") == "tools/call" else body.get("method")
                    call = clock.begin(position, str(tool or ""), received)
                    try:
                        return self._mcp(body)
                    finally:
                        clock.end(call)
                half = platform.rtt_rng.choice(platform.rtts) / 2
                if half > 0:
                    time.sleep(half)
                return self._mcp(body, half)
            return self._send(404, {"error": "not_found"})

        def _mcp(self, body: dict, half: float = 0.0) -> None:
            token = self._bearer()
            if token not in platform.seat_tokens:
                return self._send(401, {"error": "unauthorized", "detail": "invalid or expired seat token"})
            if body.get("__invalid__") or not isinstance(body, dict):
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            rid, method, params = body.get("id"), body.get("method"), body.get("params") or {}
            if rid is None:  # a notification
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "ffsim-gameapi", "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = tools_list()
            elif method == "tools/call":
                result = self._tool(token, params.get("name"), params.get("arguments") or {})
            else:
                return self._send(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            if half > 0:
                time.sleep(half)
            return self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

        def _tool(self, token: str, name, arguments: dict) -> dict:
            if name not in REQUIRED:
                return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
            missing = [r for r in REQUIRED[name] if r not in arguments]
            if missing:
                return {"content": [{"type": "text", "text": _pydantic_missing(name, missing, arguments)}], "isError": True}
            if name == "play_action" and arguments.get("action_id") is not None and not isinstance(arguments["action_id"], str):
                text = (f"Error executing tool play_action: 1 validation error for play_actionArguments\naction_id\n"
                        f"  Input should be a valid string [type=string_type, input_value={_pydantic_repr(arguments['action_id'])}, "
                        f"input_type={type(arguments['action_id']).__name__}]\n"
                        "    For further information visit https://errors.pydantic.dev/2.12/v/string_type")
                return {"content": [{"type": "text", "text": text}], "isError": True}
            try:
                payload = platform.call_tool(token, name, arguments)
            except Exception as exc:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"Error executing tool {name}: {exc!r}"}], "isError": True}
            return {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"), default=str)}],
                    "structuredContent": payload, "isError": False}

    return Handler


def serve(platform: Platform, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(platform))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="ffsim-http", daemon=True).start()
    return server
