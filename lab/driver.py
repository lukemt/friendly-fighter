"""Bots inside the lab's process: the runtime's own play loop (``altruagent.runner.run_game``) on a
``LocalGame``, which calls the local platform directly instead of over MCP/HTTP.

``LocalGame`` has ``MCPGameSession``'s methods. Each call goes through the virtual clock exactly as
an MCP request does in ``ffsim.server`` (``clock.begin`` / ``clock.end``: the round trip and the
bot's think time are charged in game time), and its answer is encoded and decoded as JSON, as the
transport would: a bot sees what it would see over the wire, and errors arrive as the same
``MCPToolError``. What a lab game leaves out is only the processes and the network: the same game
with the runtime (``scripts/ffsim.py run``) and the same seed plays out the same.
"""

from __future__ import annotations

import threading
import traceback

from altruagent.mcp_transport import MCPToolError
from altruagent.models import DecisionContext, GameState
from altruagent.runner import run_game

try:
    import orjson

    def _roundtrip(data):
        return orjson.loads(orjson.dumps(data, default=str, option=orjson.OPT_NON_STR_KEYS))
except ImportError:  # pragma: no cover
    import json

    def _roundtrip(data):
        return json.loads(json.dumps(data, default=str))


class LocalGame:
    """One seat's view of a match on an in-process ``ffsim.server.Platform``."""

    def __init__(self, platform, session_id: str, position: int) -> None:
        self.platform = platform
        self.session_id = session_id
        self.position = position
        match = platform.matches[session_id]
        self.token = match.seats[position].config.token
        self.clock = match.vclock
        self.calls = 0

    def _call(self, tool: str, arguments: dict) -> dict:
        self.calls += 1
        call = self.clock.begin(self.position, tool) if self.clock is not None else None
        try:
            payload = self.platform.call_tool(self.token, tool, {"session_id": self.session_id, **arguments})
            payload = _roundtrip(payload)
        finally:
            if call is not None:
                self.clock.end(call)
        if isinstance(payload, dict) and "error" in payload:
            raise MCPToolError(payload.get("detail") or f"MCP tool {tool!r} returned error {payload['error']!r}.",
                               status_code=None, error_code=payload["error"],
                               next_action=(payload.get("next_actions") or [None])[0])
        return payload if isinstance(payload, dict) else {}

    # -- MCPGameSession's methods ----------------------------------------------------------

    def get_state(self) -> GameState:
        return GameState.from_mcp_state(self._call("get_game_state", {}))

    def wait_for_update(self, *, since_version: int, since_message_seq: int | None = None,
                        since_is_current_actor: bool | None = None, since_phase: str | None = None,
                        timeout_seconds: float | None = None) -> GameState:
        arguments: dict = {"since_version": since_version}
        if since_is_current_actor is not None:
            arguments["since_is_current_actor"] = since_is_current_actor
        if since_phase is not None:
            arguments["since_phase"] = since_phase
        if timeout_seconds is not None:
            arguments["timeout_seconds"] = timeout_seconds
        return GameState.from_mcp_state(self._call("wait_for_update", arguments))

    def get_game_config(self, game_type: str) -> dict:
        return self._call("get_game_config", {"game_type": game_type})

    def get_legal_actions(self) -> dict:
        return self._call("get_legal_actions", {})

    def play_action(self, *, action_id: str | None = None, action: dict | None = None, state_version: int,
                    reasoning_summary: str | None = None) -> dict:
        arguments: dict = {"state_version": state_version}
        if action is not None:
            arguments["action"] = _roundtrip(action)
        if action_id is not None:
            arguments["action_id"] = action_id
        if reasoning_summary is not None:
            arguments["reasoning_summary"] = reasoning_summary
        return self._call("play_action", arguments)

    def resign(self) -> dict:
        return self._call("resign", {})

    def get_result(self) -> dict:
        return self._call("get_result", {})


class BotSeat:
    """A bot playing one seat of a match, on a thread of its own: ``agent`` is what ``create_agent()``
    returns (an object with ``choose_action``, or the function itself)."""

    def __init__(self, platform, session_id: str, position: int, agent, *, name: str = "") -> None:
        self.game = LocalGame(platform, session_id, position)
        self.agent = agent
        self.position = position
        self.name = name or f"seat{position}"
        self.error: str | None = None
        self.final: GameState | None = None
        self.context = DecisionContext(session_id=session_id, tournament_id=None, game_type="red_alert",
                                       agent_id=platform.matches[session_id].seats[position].config.agent_id,
                                       seat_position=position)
        self.thread = threading.Thread(target=self._run, name=f"bot-{self.name}", daemon=True)

    def start(self) -> "BotSeat":
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            self.final = run_game(self.game, self.context, self.agent, log=lambda line: None)
        except Exception as exc:  # noqa: BLE001  (the match goes on without this seat, as on the platform)
            self.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)
