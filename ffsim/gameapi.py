"""A local replica of the platform's Red Alert GameAPI: one ``Match`` per game.

It sits between the MCP tools (``ffsim.server``) and an engine
(``ffsim.engine_openra.OpenRAEngine`` for the real game, ``ffsim.engine_fake.FakeEngine``
for tests) and reproduces what docs/BACKEND_ANALYSIS.md and skill/redalert.md say
the hosted runner does:

- phases ``starting`` (``waiting_for_players`` lists seats not yet checked in) ->
  ``playing`` -> ``finished``; a seat checks in with its first call; the engine starts
  once every agent seat has;
- real-time pacing: one batch in flight (``NOT_YOUR_TURN``), at least 0.2 s between
  accepted batches (a batch sent sooner is held), ``STALE_STATE`` outside the last
  3000 ticks, measured when the batch arrives;
- each batch is stamped with its arrival tick and applied by the engine 12 ticks later;
  ``play_action`` answers at once with ``pending`` rows, and the engine's verdicts
  settle ``last_orders`` / ``recent_order_problems`` in a later state;
- the end: victory / draw from the engine, resignation, the time limit and the idle rule
  with the ``(kills_cost - deaths_cost, assets_value)`` tiebreak.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import observe
from .orders import Checked, View, check_order
from .rules import Rules
from .vclock import VirtualClock

TICKS_PER_SECOND = 25
APPLY_DELAY_TICKS = 12
STALE_TICKS = 3000
MIN_BATCH_INTERVAL = 0.2
MAX_ORDERS = 20
MAX_COMMANDS = 200
LOST_AFTER_TICKS = 100  # no engine verdict this long after the apply tick: lost (not_applied)
MAX_WAITS = 2
# How long a batch stays "in flight" while GameAPI hands it to the game runner. On the platform two
# play_action calls sent at the same moment give the second NOT_YOUR_TURN (probe a06); locally the
# hand-over would take ~1 ms and the second would slip through.
HANDOVER_SECONDS = 0.02
NO_AGENTS_SECONDS = 300
IDLE_SECONDS = 1200
# The platform's timing, measured in its wire logs (docs/SIMULATOR.md, "Timing"):
# - wait_for_update looks for a change when it arrives and then every 0.1 s: its durations
#   cluster at the round trip + 0, 0.1, 0.2, 0.3 s;
# - a view shows in the served state some time after its tick (45 ms, exponential; one view in
#   ten 0.1 s more), in order, while batches are stamped with the newest tick at once: so a
#   batch sent right after a state usually lands one view later (arrival - state_version:
#   0 / 6 / 12 ticks in ~8 / 83 / 4+ % of batches). Fitted to the lags, the wait_for_update
#   durations and the views each wait advanced (one in 96.5 %) in the wire logs.
PLATFORM_TIMING = {"wait_poll": 0.1, "publish_delay_mean": 0.045, "publish_tail_p": 0.1, "publish_tail_mean": 0.1}
PLAYING_AFTER_VIEWS = 2  # views in "starting" before "playing" (the platform showed two)

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

HINT_PLAYING = ("Real time: the game keeps running. Send orders whenever you are ready with play_action "
                "(state_version={sv}, this state's), then read the new state and act again; up to one batch every 0.2 s.")
HINT_STARTING = "The game server is starting the match; call wait_for_update."
NEXT_WAIT_PLAYERS = {"tool": "wait_for_update", "hint": (
    "Waiting for the other players to connect; the game starts automatically once every seat has.")}
HINT_IN_FLIGHT = "Your previous batch is still being applied; send the next one when its play_action answers."
NEXT_WAIT = {"tool": "wait_for_update", "hint": (
    "Call wait_for_update with this state_version, is_current_actor (since_is_current_actor) and phase "
    "(since_phase), and the highest new_messages seq you have seen; it returns as soon as something changes.")}
NEXT_PLAY = {"tool": "play_action", "hint": (
    "Send 1-20 orders as legal_actions.how_to_send shows; legal_actions lists what you can do now.")}
NEXT_AFTER_PLAY = {"tool": "get_game_state", "hint": "Your orders' verdicts appear in the next state's last_orders."}


class ToolError(Exception):
    """A tool answer of the form {"error": code, "detail": ..., "next_actions"?: [...]}."""

    def __init__(self, code: str, detail: str, next_actions: list | None = None) -> None:
        super().__init__(detail)
        self.code, self.detail, self.next_actions = code, detail, next_actions

    def payload(self) -> dict:
        out = {"error": self.code, "detail": self.detail}
        if self.next_actions:
            out["next_actions"] = self.next_actions
        return out


@dataclass
class SeatConfig:
    kind: str = "agent"  # "agent" (plays through GameAPI) or "ai:<bot type>" (an OpenRA bot, e.g. ai:normal)
    agent_id: str = ""
    name: str = ""
    token: str = ""


@dataclass
class PendingBatch:
    batch: int
    arrival: int
    checked: list[Checked]
    record: dict
    got: dict = field(default_factory=dict)  # order index -> {actor_id: result}


class Seat:
    def __init__(self, position: int, config: SeatConfig) -> None:
        self.position = position
        self.name = f"Multi{position}"
        self.config = config
        self.checked_in = config.kind != "agent"
        self.view: dict | None = None
        self.obs: dict | None = None
        self.views = 0
        self.tracker = observe.EventTracker()
        self.batch = 0
        self.in_flight = False
        self.last_accept = -1e9
        self.last_orders: dict | None = None
        self.settled: deque = deque(maxlen=5)  # problem rows of the last 5 settled batches
        self.pending: dict[int, PendingBatch] = {}
        self.repair_memory: dict = {}
        self.power_memory: dict = {}
        self.faction: str | None = None
        self.queue: deque = deque()  # (publish time, view) not yet in the state

    @property
    def is_agent(self) -> bool:
        return self.config.kind == "agent"

    def problems(self) -> list[dict]:
        rows = [row for batch in self.settled for row in batch]
        return rows[-10:]


class Match:
    def __init__(self, session_id: str, map_id: str, seats: list[SeatConfig], engine_factory, *,
                 max_minutes: int = 20, rules: Rules | None = None, clock=time.monotonic,
                 sleep=time.sleep, match_rules: dict | None = None, virtual: dict | None = None,
                 timing: dict | None = None, seed: int | None = None) -> None:
        """``virtual``: run in virtual time (``ffsim.vclock``) with these ``VirtualClock`` options
        (``rtt``, ``think``...); the engine must then support ``step``. ``timing``: the
        platform's own delays (``PLATFORM_TIMING``); without it, waits wake on every change and
        views show at once."""
        self.session_id = session_id
        self.rules = rules or Rules.load()
        self.map_id = map_id
        self.map = self.rules.map(map_id)
        self.size = (self.map["width"], self.map["height"])
        self.max_minutes = max_minutes
        self.seats = [Seat(i, s) for i, s in enumerate(seats)]
        self.engine_factory = engine_factory
        self.engine = None
        self.clock = clock
        self.sleep = sleep
        self.match_rules = match_rules or {"starting_cash": 5000, "starting_units": "none", "explored_map": False,
                                           "kill_bounties": False, "crates": True}
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.vclock: VirtualClock | None = None
        if virtual is not None:
            self.vclock = VirtualClock(self.cond, [i for i, s in enumerate(seats) if s.kind == "agent"],
                                       **{"seed": str(seed) if seed is not None else session_id, **virtual})
            self.clock, self.sleep = clock, sleep = self.vclock.now, self.vclock.sleep
        self.latest_tick = 0  # the tick of the newest view: GameAPI's "current tick"
        timing = timing or {}
        self.wait_poll: float | None = timing.get("wait_poll")
        self.publish_delay_mean: float = timing.get("publish_delay_mean", 0.0)
        self.publish_tail_p: float = timing.get("publish_tail_p", 0.0)
        self.publish_tail_mean: float = timing.get("publish_tail_mean", 0.0)
        self.rng = random.Random(seed if seed is not None else session_id)
        self.publish_delays: dict[int, float] = {}  # view tick -> its delay (the same for both seats)
        # The platform shows "starting" while it waits for the seats to check in (waiting_for_players lists them).
        self.phase = "starting"
        self.engine_started = False
        self.rev = 0
        self.created = time.monotonic()  # the no-agents rule runs on the wall clock
        self.start_tick: int | None = None
        self.result: dict | None = None
        self.termination_reason: str | None = None
        self.failure_reason: str | None = None
        self.waits = 0
        self.last_accepted_at = clock()
        self.finished_at: float | None = None
        self.log: list[str] = []
        self.reasoning: list[dict] = []

    # -- lifecycle ----------------------------------------------------------------------

    def seat_for_token(self, token: str) -> Seat | None:
        return next((s for s in self.seats if s.is_agent and s.config.token == token), None)

    def check_in(self, seat: Seat) -> None:
        start = False
        with self.lock:
            if not seat.checked_in:
                seat.checked_in = True
                self._notify()
            if not self.engine_started and self.phase == "starting" and all(s.checked_in for s in self.seats):
                self.engine_started = True
                start = True
                self._notify()
        if start:
            self._start_engine()

    def _start_engine(self) -> None:
        try:
            self.engine = self.engine_factory(self)
            self.engine.start(self.on_view, self.on_engine_exit)
            if self.vclock is not None:
                self.vclock.start(self.engine, self.on_views, self.on_engine_exit, self.on_time)
        except Exception as exc:  # noqa: BLE001
            self.fail("engine_start_failed", repr(exc))

    def tick(self) -> None:
        """Housekeeping, called about once a second by the server: no-agent timeout."""
        with self.lock:
            if self.vclock is None:
                self._publish()
            if not self.engine_started and self.phase == "starting" and time.monotonic() - self.created > NO_AGENTS_SECONDS:
                self._finish_no_result("no_agents_connected")

    def on_engine_exit(self, why: str) -> None:
        with self.lock:
            if self.phase != "finished":
                self._finish_no_result("engine_crashed", why)

    def fail(self, reason: str, why: str = "") -> None:
        with self.lock:
            self._finish_no_result(reason, why)

    def _finish_no_result(self, reason: str, why: str = "") -> None:
        if self.phase == "finished":
            return
        self.failure_reason = reason
        self.termination_reason = reason
        self.result = {"outcome": "no_result", "reason": reason, "winner_seat": None, "returns": {}}
        self.log.append(f"no result: {reason} {why}".strip())
        self._end()

    def _finish(self, reason: str, scores: dict[int, float]) -> None:
        if self.phase == "finished":
            return
        self.termination_reason = reason
        winner = next((p for p, v in scores.items() if v == 1.0), None)
        self.result = {"outcome": None, "reason": reason, "winner_seat": f"Multi{winner}" if winner is not None else None,
                       "returns": {self.seats[p].config.agent_id: v for p, v in scores.items()}}
        self.scores = scores
        self.log.append(f"finished: {reason} {scores}")
        self._end()

    def _end(self) -> None:
        self.phase = "finished"
        self.finished_at = time.monotonic()
        for seat in self.seats:
            for pending in list(seat.pending.values()):
                self._settle(seat, pending, final_tick=None)
        self._notify()
        if self.vclock is not None:
            self.vclock.stop()
        engine = self.engine
        if engine is not None:
            threading.Thread(target=engine.stop, name=f"stop-{self.session_id[:8]}", daemon=True).start()

    # -- engine side --------------------------------------------------------------------

    def _notify(self) -> None:
        if self.vclock is not None:
            self.vclock.notify()
        else:
            self.cond.notify_all()

    def _wait(self, deadline: float) -> None:
        """Holding the lock: wait for a change (a notify) or the deadline."""
        if self.vclock is not None:
            self.vclock.wait(self.cond, deadline)
        else:
            self.cond.wait(max(0.0, min(deadline - self.clock(), 0.5)))

    def on_views(self, views: list[tuple[int, dict]]) -> None:
        for position, view in views:
            self.on_view(position, view)

    def on_view(self, position: int, view: dict) -> None:
        """A view from the engine: its tick is GameAPI's "now" at once; the state shows it once
        it is published (``publish_delay_mean`` later)."""
        with self.lock:
            if self.phase == "finished":
                return
            tick = view["tick"]
            self.latest_tick = max(self.latest_tick, tick)
            delay = 0.0
            if self.publish_delay_mean:
                delay = self.publish_delays.get(tick)
                if delay is None:
                    delay = self.rng.expovariate(1.0 / self.publish_delay_mean)
                    if self.publish_tail_p and self.rng.random() < self.publish_tail_p:
                        delay += self.rng.expovariate(1.0 / self.publish_tail_mean)
                    self.publish_delays[tick] = delay
                    self.publish_delays.pop(tick - 600, None)
            # In virtual time a view is made exactly at its tick; in real time when it reaches us.
            made = tick / TICKS_PER_SECOND if self.vclock is not None else self.clock()
            self.seats[position].queue.append((made + delay, view))
            self._publish()

    def on_time(self) -> None:
        """The virtual clock moved: publish what is due."""
        with self.lock:
            self._publish()

    def _publish(self) -> None:
        """Holding the lock: apply every view whose publication time has come."""
        now = self.clock()
        for seat in self.seats:
            while seat.queue and seat.queue[0][0] <= now and self.phase != "finished":
                self._apply_view(seat, seat.queue.popleft()[1])

    def _apply_view(self, seat: Seat, view: dict) -> None:
        seat.view = view
        seat.views += 1
        self.rev += 1
        seat.faction = view.get("faction") or seat.faction
        if self.start_tick is None:
            self.start_tick = view["tick"]
        seat.tracker.update(view)
        seat.obs = observe.observation(view, self.map_id, self.size, seat.tracker.events)
        self._collect_results(seat, view)
        if self.phase == "starting" and all(s.views >= PLAYING_AFTER_VIEWS for s in self.seats if s.is_agent):
            self.phase = "playing"
        self._check_end(view)
        self._notify()

    def _collect_results(self, seat: Seat, view: dict) -> None:
        for r in view.get("command_results") or []:
            pending = seat.pending.get(r["batch"])
            if pending is not None:
                pending.got.setdefault(r["index"], {})[r.get("actor_id", 0)] = r
        for pending in list(seat.pending.values()):
            expected_done = all(
                len(pending.got.get(c.index, {})) >= len(c.commands) for c in pending.checked if c.commands)
            applied = pending.arrival + APPLY_DELAY_TICKS
            if expected_done and view["tick"] >= applied:
                self._settle(seat, pending, final_tick=applied)
            elif view["tick"] >= applied + LOST_AFTER_TICKS:
                self._settle(seat, pending, final_tick=applied)

    def _settle(self, seat: Seat, pending: PendingBatch, final_tick: int | None, *, carried: bool = True) -> None:
        rows = []
        engine_ok_any = False
        for c in pending.checked:
            row = c.row()
            if c.status == "pending":
                got = pending.got.get(c.index, {})
                results = [got.get(cmd.get("actor_id", 0)) for cmd in c.commands]
                if not any(results):
                    row.update(status="lost", reason="not_applied")
                else:
                    failed = list(c.failed)
                    oks = 0
                    for cmd, res in zip(c.commands, results):
                        if res is None:
                            failed.append({"unit": cmd.get("actor_id"), "reason": "not_applied"})
                        elif res["status"] == "ok":
                            oks += 1
                        elif cmd.get("actor_id"):
                            failed.append({"unit": cmd["actor_id"], "reason": res["reason"]})
                        else:
                            failed.append({"unit": None, "reason": res["reason"]})
                    engine_ok_any = engine_ok_any or oks > 0
                    if oks == 0:
                        row.update(status="rejected", reason=failed[len(c.failed)]["reason"] if len(failed) > len(c.failed) else failed[0]["reason"],
                                   failed=failed)
                        if not c.commands[0].get("actor_id"):
                            row["failed"] = list(c.failed)
                    elif failed:
                        row.update(status="partial", reason=failed[0]["reason"], failed=failed)
                    else:
                        row.update(status="ok", reason="ok", failed=[])
            if row["status"] in ("rejected", "lost") and c.cmd in ("repair", "power_toggle") and c.commands:
                # A toggle the engine refused changed nothing: forget it (on the platform, on=true right
                # after a refused power-down answers already_on).
                memory = seat.repair_memory if c.cmd == "repair" else seat.power_memory
                memory.pop(c.commands[0].get("actor_id"), None)
            rows.append(row)
        rec = pending.record
        rec.pop("rev", None)
        rec.update(carried=carried, pending=False, applied_at_tick=final_tick, results=rows,
                   accepted_orders=sum(1 for r in rows if r["status"] != "rejected"),
                   rejected_orders=sum(1 for r in rows if r["status"] == "rejected"))
        seat.settled.append([{"batch": pending.batch, "index": r["index"], "cmd": r["cmd"], "status": r["status"],
                              "reason": r["reason"], "detail": r["detail"]}
                             for r in rows if r["status"] in ("rejected", "partial", "lost")])
        seat.pending.pop(pending.batch, None)
        if engine_ok_any:
            self.last_accepted_at = self.clock()

    def _check_end(self, view: dict) -> None:
        if self.phase != "playing" and not view.get("done"):
            return
        if view.get("done"):
            winner = view.get("winner") or ""
            if view.get("result") == "draw" or not winner:
                self._finish("draw", {s.position: 0.5 for s in self.seats})
            else:
                self._finish("victory", {s.position: 1.0 if s.name == winner else 0.0 for s in self.seats})
            return
        if self.game_seconds() >= self.max_minutes * 60:
            self._tiebreak("time_limit")
        elif self.clock() - self.last_accepted_at >= IDLE_SECONDS:
            self._tiebreak("idle_timeout")

    def tiebreak_score(self, seat: Seat) -> tuple[int, int]:
        mil = (seat.obs or {}).get("military")
        if mil is None:
            # An OpenRA bot seat has no view: its kills/losses mirror the agent seat's, assets unknown (0).
            other = next((s for s in self.seats if s is not seat and s.obs), None)
            if seat.is_agent or other is None:
                return (0, 0)
            om = other.obs["military"]
            return (om["deaths_cost"] - om["kills_cost"], 0)
        return (mil["kills_cost"] - mil["deaths_cost"], mil["assets_value"])

    def _tiebreak(self, reason: str) -> None:
        a, b = (self.tiebreak_score(s) for s in self.seats)
        if a == b:
            scores = {0: 0.5, 1: 0.5}
        else:
            scores = {0: 1.0, 1: 0.0} if a > b else {0: 0.0, 1: 1.0}
        self._finish(reason, scores)

    # -- clock --------------------------------------------------------------------------

    def current_tick(self) -> int:
        """GameAPI's idea of "now": the tick of the newest view. On the platform every batch is
        stamped with a view tick (arrival - state_version is 0, 6 or 12 in every recording)."""
        return self.latest_tick

    def game_seconds(self) -> float:
        ticks = [s.view["tick"] for s in self.seats if s.view]
        if self.start_tick is None or not ticks:
            return 0.0
        return (max(ticks) - self.start_tick) / TICKS_PER_SECOND

    # -- tools --------------------------------------------------------------------------

    def state(self, seat: Seat, *, updated: bool | None = None) -> dict:
        with self.lock:
            self._publish()
            return self._state(seat, updated)

    def _state_version(self, seat: Seat) -> int:
        if self.phase in ("playing", "finished") or (self.phase == "starting" and seat.obs):
            return seat.obs["tick"] if seat.obs else 0
        return 0

    def _status(self) -> str:
        return STATUS_COMPLETED if self.phase == "finished" else STATUS_IN_PROGRESS

    def _players(self, seat: Seat) -> list[dict]:
        return [{"seat": s.name, "agent_id": s.config.agent_id if s is seat else None,
                 "name": s.config.name or s.name,
                 "faction": s.faction if self.engine_started else None,
                 "checked_in": s.checked_in} for s in self.seats]

    def _state(self, seat: Seat, updated: bool | None) -> dict:
        sv = self._state_version(seat)
        playing = self.phase == "playing"
        current = playing and not seat.in_flight
        now = self.clock()
        allowed_in = max(0, int(round((MIN_BATCH_INTERVAL - (now - seat.last_accept)) * 1000)))
        secs = self.game_seconds() if self.start_tick is not None else None
        out = {
            "session_id": self.session_id,
            "game_type": "red_alert",
            "runtime_adapter": "openra",
            "status": self._status(),
            "phase": self.phase,
            "state_version": sv,
            "is_current_actor": current,
            "is_terminal": self.phase == "finished",
            "current_actor": None,
            "your_seat": seat.name,
            "players": self._players(seat),
            "window": None,
            "pacing": {"mode": "realtime", "min_batch_interval_s": MIN_BATCH_INTERVAL, "stale_seconds": 120,
                       "next_batch_allowed_in_ms": allowed_in if playing else 0},
            "time": {"game_seconds": round(secs, 3) if secs is not None and self.phase != "starting" else None,
                     "remaining_seconds": (round(self.max_minutes * 60 - secs, 3)
                                           if secs is not None and playing else None),
                     "max_minutes": self.max_minutes},
            "queue_position": None,
            "map": {"id": self.map_id, "width": self.size[0], "height": self.size[1], "bounds": self.map["bounds"],
                    "layout": (f"A picture of this map (terrain, starting ore, obstacles, spawns) is in "
                               f"get_game_config('red_alert') at rules.maps['{self.map_id}'].layout.")},
            "limits": {"max_orders_per_batch": MAX_ORDERS, "max_units_per_order": 50, "max_train_count": 10,
                       "min_batch_interval_s": MIN_BATCH_INTERVAL, "stale_seconds": 120},
            "match_rules": dict(self.match_rules),
            "observation": seat.obs,
            "last_orders": seat.last_orders,
            "recent_order_problems": seat.problems(),
        }
        if self.phase == "finished":
            out["result"] = self._result_for(seat)
        if playing:
            out["hint"] = HINT_PLAYING.format(sv=sv) if current else HINT_IN_FLIGHT
        elif self.phase == "starting":
            out["hint"] = HINT_STARTING
        else:
            out["hint"] = self._finished_hint(seat)
        out["new_messages"] = []
        waiting = [s.position for s in self.seats if not s.checked_in]
        if self.phase == "starting" and waiting:
            out["waiting_for_players"] = waiting
        if current and seat.obs:
            out["legal_actions"] = observe.legal_actions(self.session_id, sv, seat.obs, self.map["spawns"],
                                                         self.rules.building_items)
        if self.phase == "finished":
            out["next_actions"] = [{"tool": "get_result"}]
        elif current:
            out["next_actions"] = [NEXT_PLAY]
        elif self.phase == "starting" and waiting:
            out["next_actions"] = [NEXT_WAIT_PLAYERS]
        else:
            out["next_actions"] = [NEXT_WAIT]
        if updated is not None:
            out["updated"] = updated
        return out

    def _result_for(self, seat: Seat) -> dict:
        r = dict(self.result or {})
        if r.get("outcome") != "no_result":
            score = (self.result or {}).get("returns", {}).get(seat.config.agent_id)
            if seat.config.agent_id and score is not None:
                r["outcome"] = "win" if score == 1.0 else "loss" if score == 0.0 else "draw"
            else:
                r["outcome"] = "draw" if self.termination_reason == "draw" else None
        return r

    def _finished_hint(self, seat: Seat) -> str:
        r = self._result_for(seat)
        words = {"win": "you won", "loss": "you lost", "draw": "a draw", "no_result": "no result"}
        return f"Game over: {words.get(r.get('outcome'), 'finished')} ({r.get('reason')}). Call get_result."

    def legal_actions(self, seat: Seat) -> dict:
        with self.lock:
            self._publish()
            sv = self._state_version(seat)
            if self.phase == "playing" and seat.obs:
                return observe.legal_actions(self.session_id, sv, seat.obs, self.map["spawns"], self.rules.building_items)
            return observe.empty_legal_actions(self.session_id, sv)

    def get_result(self, seat: Seat) -> dict:
        with self.lock:
            self._publish()
            if self.phase != "finished":
                return {"session_id": self.session_id, "is_terminal": False, "status": STATUS_IN_PROGRESS,
                        "returns": None, "your_return": None, "winner_agent_id": None, "termination_reason": None,
                        "failure_reason": None,
                        "next_actions": [{"tool": "get_game_state", "hint": "Game is still in progress."}]}
            returns = (self.result or {}).get("returns") or {}
            winner = next((a for a, v in returns.items() if v == 1.0), None)
            return {"session_id": self.session_id, "is_terminal": True, "status": STATUS_COMPLETED,
                    "returns": returns, "your_return": returns.get(seat.config.agent_id) if returns else None,
                    "winner_agent_id": winner, "termination_reason": self.termination_reason,
                    "failure_reason": self.failure_reason}

    def resign(self, seat: Seat) -> dict:
        with self.lock:
            self._publish()
            if self.phase == "finished":
                raise ToolError("GAME_ALREADY_COMPLETE", "The game is over. Call get_result.", [{"tool": "get_result"}])
            engine = self.engine
            self._finish("resignation", {s.position: 0.0 if s is seat else 1.0 for s in self.seats})
        if engine is not None:
            try:
                engine.surrender(seat.position)
            except Exception:  # noqa: BLE001
                pass
        return self.state(seat)

    def wait_for_update(self, seat: Seat, since_version: int, since_is_current_actor=None, since_phase=None,
                        timeout_seconds: float = 20.0) -> dict:
        timeout = max(0.0, min(float(timeout_seconds), 25.0))
        with self.lock:
            self._publish()
            if self.waits >= MAX_WAITS:
                raise ToolError("TOO_MANY_WAITS", f"At most {MAX_WAITS} wait_for_update calls may wait at once per game.")
            if since_is_current_actor is None:
                since_is_current_actor = self.phase == "playing" and not seat.in_flight
            if since_phase is None:
                since_phase = self.phase
            self.waits += 1
            deadline = self.clock() + timeout
        try:
            while True:
                with self.lock:
                    self._publish()
                    current = self.phase == "playing" and not seat.in_flight
                    if (self._state_version(seat) != since_version or current != since_is_current_actor
                            or self.phase != since_phase or self.phase == "finished"):
                        return self._state(seat, True)
                    left = deadline - self.clock()
                    if left <= 0:
                        return self._state(seat, False)
                    if not self.wait_poll:
                        self._wait(deadline)
                        continue
                # The platform looks again every 0.1 s.
                self.sleep(min(self.wait_poll, left))
        finally:
            with self.lock:
                self.waits -= 1

    def play_action(self, seat: Seat, state_version, action, reasoning_summary=None) -> dict:
        if not isinstance(state_version, int) or isinstance(state_version, bool):
            raise ToolError("INVALID_ACTION", "Malformed orders batch: state_version: Input should be a valid integer")
        if state_version < 0:
            raise ToolError("INVALID_ACTION", "Malformed orders batch: state_version: Input should be greater than or equal to 0")
        with self.lock:
            self._publish()
            if self.phase == "finished":
                raise ToolError("GAME_ALREADY_COMPLETE", "The game is over. Call get_result.", [{"tool": "get_result"}])
            if self.phase != "playing":
                raise ToolError("NOT_YOUR_TURN", "The game has not started yet; wait for it with wait_for_update.",
                                [NEXT_WAIT])
            if seat.in_flight:
                raise ToolError("NOT_YOUR_TURN",
                                "Your previous batch is still being applied; send the next one after its play_action answers.",
                                [{"tool": "play_action", "hint": (
                                    "Retry play_action in a moment (one batch at a time, at most one every 0.2 s), "
                                    "with the state_version of your newest get_game_state.")}])
            seat.in_flight = True
            self._notify()
        try:
            return self._play(seat, state_version, action, reasoning_summary)
        finally:
            with self.lock:
                seat.in_flight = False
                self._notify()

    def _play(self, seat: Seat, state_version: int, action, reasoning_summary) -> dict:
        if not isinstance(action, dict) or action.get("type") != "orders":
            raise ToolError("INVALID_ACTION",
                            "action must be {\"type\": \"orders\", \"orders\": [...]} (see get_game_config('red_alert').order_schema)")
        orders = action.get("orders")
        if not isinstance(orders, list) or not 1 <= len(orders) <= MAX_ORDERS:
            raise ToolError("INVALID_ACTION", f"orders must be a non-empty list of order objects (at most {MAX_ORDERS})")
        with self.lock:
            arrival = self.current_tick()
            if state_version > arrival or state_version < arrival - STALE_TICKS:
                raise ToolError("STALE_STATE", (
                    "state_version is the tick of the state you acted on (the state_version of get_game_state); "
                    f"it is more than 2 minutes old or in the future (current tick {arrival}, oldest accepted "
                    f"{max(0, arrival - STALE_TICKS)}). Call get_game_state and send its state_version."),
                    [{"tool": "get_game_state", "hint": "Re-fetch state, then retry with the new state_version."}])
            view = View(seat.obs or {}, self.size, now=self.clock(), repair_memory=seat.repair_memory,
                        power_memory=seat.power_memory)
            checked = [check_order(i, o, view) for i, o in enumerate(orders)]
        valid = [c for c in checked if c.status != "rejected"]
        if not valid:
            parts = [f"[{c.index}] {c.cmd}: {c.reason} ({c.detail})" for c in checked]
            raise ToolError("INVALID_ACTION", "No order in the batch was valid, so nothing was sent and it did not "
                                              "count as a batch: " + "; ".join(parts))
        total = sum(c.engine_commands for c in checked)
        if total > MAX_COMMANDS:
            raise ToolError("INVALID_ACTION", f"The batch expands to {total} engine commands; at most {MAX_COMMANDS} "
                                              "per batch. Split it across batches.")  # unverified wording
        # Hold the batch for the rest of the 0.2 s gap since the last accepted one.
        wait = MIN_BATCH_INTERVAL - (self.clock() - seat.last_accept)
        if wait > 0:
            self.sleep(wait)
        with self.lock:
            if self.phase != "playing":
                raise ToolError("NOT_YOUR_TURN", "The game is ending.", [NEXT_WAIT])
            arrival = self.current_tick()
            seat.last_accept = self.clock()
            seat.batch += 1
            batch = seat.batch
            now = self.clock()
            commands = []
            for c in checked:
                if c.status == "pending":
                    commands.extend(c.commands)
                    for cmd in c.commands:
                        bid = cmd.get("actor_id")
                        if c.cmd == "repair":
                            seat.repair_memory[bid] = (not (view.buildings.get(bid) or {}).get("repairing"), now)
                        elif c.cmd == "power_toggle":
                            seat.power_memory[bid] = (not (view.buildings.get(bid) or {}).get("powered_down"), now)
                        elif c.cmd == "sell":
                            seat.tracker.note_sell(bid, arrival)
            rows = [c.row() for c in checked]
            pending_any = any(c.status == "pending" for c in checked)
            self.rev += 1
            record = {"rev": self.rev, "pacing": "realtime", "state_version": arrival, "batch": batch, "carried": False,
                      "pending": pending_any, "applied_at_tick": None, "results": rows,
                      "accepted_orders": sum(1 for c in checked if c.status != "rejected"),
                      "rejected_orders": sum(1 for c in checked if c.status == "rejected"),
                      "next_batch_allowed_in_ms": int(MIN_BATCH_INTERVAL * 1000)}
            seat.last_orders = record
            if reasoning_summary:
                self.reasoning.append({"seat": seat.name, "batch": batch, "tick": arrival,
                                       "text": str(reasoning_summary)[:500]})
            pending = PendingBatch(batch, arrival, checked, record)
            if pending_any:
                seat.pending[batch] = pending
                engine = self.engine
            else:  # only no-op repair / power_toggle orders: settled at once, nothing carried over
                self._settle(seat, pending, final_tick=None, carried=False)
                engine = None
            answer = {"accepted": True, "session_id": self.session_id, "status": self._status(),
                      "state_version": arrival, "results": [dict(r) for r in rows],
                      "accepted_orders": record["accepted_orders"], "rejected_orders": record["rejected_orders"],
                      "applied_at_tick": None, "pending": pending_any,
                      "next_batch_allowed_in_ms": int(MIN_BATCH_INTERVAL * 1000),
                      "hint": f"Orders sent at tick {arrival}; the engine applies them in a moment. Read the new state and act again.",
                      "next_actions": [NEXT_AFTER_PLAY]}
        if engine is not None:
            engine.send(seat.position, batch, arrival + APPLY_DELAY_TICKS - engine.order_latency_ticks, commands)
            if HANDOVER_SECONDS:
                self.sleep(HANDOVER_SECONDS)
        return answer
