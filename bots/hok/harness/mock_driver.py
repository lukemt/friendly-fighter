"""Mock match driver: our agent plays whole sim matches through the real runner.

    python -m bots.hok.harness.mock_driver                    # both camps x both pacings x chaos on/off
    python -m bots.hok.harness.mock_driver --steps 3000 --opponent upstream --chaos 0.05

``altruagent.runner.run_game`` drives the agent exactly as in a live match;
``SimSession`` stands in for ``MCPGameSession`` and answers like the platform
(/skill/hok): states in the documented shape, legal actions for pending heroes,
standing orders, ``STALE_STATE`` (lockstep) and ``INVALID_ACTION``. The other
camp is played inside the session by a baseline (or our own bot).

Time is virtual: each call costs ``latency``, real time steps every 0.2 s of it
whatever the agent does, and the agent's pacing sleep advances it too.

``--chaos P`` makes the platform awkward with probability P per occasion:
fields missing or mistyped in the state, labels without distances, the
embedded ``legal_actions`` left out (the runner then calls
``get_legal_actions``), a macro gone between the read and the send
(``INVALID_ACTION``), the step moving on first in lockstep (``STALE_STATE``),
``RUNTIME_TEMPORARILY_UNAVAILABLE``, and a match that starts in
``waiting_for_players`` / ``queued`` / ``starting``.

It reports, per match: crashes (anything escaping the runner, and the agent's
own caught exceptions), **illegal answers** (an answer that was not legal in
the state the agent was shown: the safety layer's job is to make this 0),
refusals that were platform races (not the agent's fault), and the agent's
decision time.
"""

from __future__ import annotations

import argparse
import copy
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from altruagent import WAIT, DecisionContext, GameState
from altruagent.mcp_transport import MCPToolError
from altruagent.runner import run_game

from ..sim.baselines import make_agent
from ..sim.engine import STEP_SECONDS, Sim, SimConfig
from ..sim.match import GAME_TYPE, seats_of, server_check, state_for


def _tool_error(code: str, detail: str) -> MCPToolError:
    return MCPToolError(f"{code}: {detail}", status_code=None, error_code=code, detail=detail)


@dataclass
class Counts:
    reads: int = 0
    waits: int = 0
    plays: int = 0
    accepted: int = 0
    illegal: int = 0                     # not legal in the state the agent was shown
    races: int = 0                       # legal when read, gone when sent (injected or real)
    stale: int = 0
    unavailable: int = 0
    omitted_legal: int = 0
    illegal_detail: list[str] = field(default_factory=list)


class SimSession:
    """An ``MCPGameSession`` stand-in over the sim, for one camp."""

    def __init__(self, sim: Sim, camp: int, mode: str, opponent: Any, *, latency: float = 0.1,
                 chaos: float = 0.0, seed: int = 0, prelude: int = 0) -> None:
        self.sim = sim
        self.camp = camp
        self.mode = mode
        self.opponent = opponent
        self.latency = latency
        self.chaos = chaos
        self.rng = random.Random(seed * 7919 + camp)
        self.session_id = f"mock-{seed}-{camp}-{mode}"
        self.game_server_url = "http://mock"
        self.vt = 0.0                         # virtual wall clock
        self.acted: set[int] = set()          # our seats that acted this step
        self.counts = Counts()
        self.prelude = prelude                # reads answered before the match starts
        self._shown: dict[str, Any] | None = None   # the last state the agent was shown
        self._opp_ctx = DecisionContext(session_id=self.session_id, tournament_id=None, game_type=GAME_TYPE,
                                        agent_id="opponent", seat_position=2 - camp)
        self.opp_illegal = 0
        self.resigned = False

    # -- time --------------------------------------------------------------------------------

    def clock(self) -> float:
        return self.vt

    def sleep(self, seconds: float) -> None:
        self._advance(max(0.0, seconds))

    def _advance(self, dt: float) -> None:
        self.vt += dt
        if self.mode != "realtime":
            return
        while self.sim.termination is None and self.vt >= (self.sim.step_no + 1) * STEP_SECONDS + self._start_t:
            self._step()

    _start_t = 0.0

    def _step(self) -> None:
        self._opponent_moves()
        self.sim.step()
        self.acted = set()

    def _opponent_moves(self) -> None:
        camp = 3 - self.camp
        acted: set[int] = set()
        for _ in range(6):
            pending = [s for s in seats_of(camp) if s not in acted]
            raw = state_for(self.sim, camp, mode=self.mode, pending=pending, elapsed_s=self.vt)
            entries = (raw.get("legal_actions") or {}).get("actions") or []
            if not pending or not entries:
                return
            decision = self.opponent.choose_action(GameState.from_mcp_state(raw), self._opp_ctx)
            if decision is WAIT:
                return
            action_id, action = None, None
            inner = getattr(decision, "action", decision)
            if isinstance(inner, dict):
                action = inner
            else:
                action_id = getattr(inner, "action_id", inner)
            seat, payload, error = server_check(self.sim, camp, pending, entries, action_id, action)
            if error is None:
                error = self.sim.apply(seat, payload)
            if error is not None:
                self.opp_illegal += 1
                return
            acted.add(seat)
            hook = getattr(self.opponent, "on_action_result", None)
            if callable(hook):
                hook({"accepted": True}, self._opp_ctx)

    # -- rendering ----------------------------------------------------------------------------

    def _pending(self) -> list[int]:
        return [s for s in seats_of(self.camp) if s not in self.acted]

    def _render(self) -> dict[str, Any]:
        if self.prelude > 0:
            self.prelude -= 1
            phase = ["waiting_for_players", "queued", "starting"][self.prelude % 3]
            raw = {"session_id": self.session_id, "game_type": GAME_TYPE, "status": "in_progress", "phase": phase,
                   "is_current_actor": False, "is_terminal": False, "state_version": 0, "seats": seats_of(self.camp),
                   "camp": self.camp, "queue_position": 1 if phase == "queued" else None,
                   "pacing": {"mode": self.mode}, "observation": {}, "legal_actions": None}
            return raw
        raw = state_for(self.sim, self.camp, mode=self.mode, pending=self._pending(), session_id=self.session_id,
                        elapsed_s=self.vt)
        if self.chaos:
            raw = self._mess_up(raw)
        self._shown = raw
        return raw

    def _maybe(self) -> bool:
        return self.rng.random() < self.chaos

    def _mess_up(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = copy.deepcopy(raw)
        obs_seats = raw["observation"]["seats"]
        for key in list(obs_seats):
            summary = obs_seats[key]["summary"]
            me = summary["my_hero"]
            if self._maybe():
                me.pop(self.rng.choice(["hp", "max_hp", "x", "z", "level", "revive_time"]), None)
            if self._maybe():
                me["hp"] = str(me.get("hp"))           # a number as a string
            if self._maybe():
                me["max_hp"] = 0                       # a zero to divide by
            if self._maybe():
                summary["other_heroes"] = [dict(o, x=None) for o in summary["other_heroes"]]
            if self._maybe():
                summary.pop("structures", None)
            if self._maybe():
                summary["other_heroes"] = "garbage"
            if self._maybe() and self._maybe():
                del obs_seats[key]                     # a seat with no view at all
        if self._maybe():
            raw["simultaneous"].pop("standing_orders", None)
        if self._maybe() and self._maybe():
            raw["simultaneous"].pop("your_pending_seats", None)
        if self._maybe() and self._maybe():
            raw["observation"] = None
        if self._maybe():
            raw["max_steps"] = None
        actions = (raw.get("legal_actions") or {}).get("actions") or []
        for a in actions:
            if self._maybe():
                a["label"] = a["label"].split("(")[0].strip()   # a label without distance
        if actions and self._maybe():
            raw["legal_actions"] = None                         # the runner calls get_legal_actions
            self.counts.omitted_legal += 1
        return raw

    # -- the MCPGameSession interface ----------------------------------------------------------

    def get_state(self) -> GameState:
        self.counts.reads += 1
        self._advance(self.latency)
        return GameState.from_mcp_state(self._render())

    def wait_for_update(self, *, since_version, since_message_seq=None, since_is_current_actor=None,
                        since_phase=None, timeout_seconds=None) -> GameState:
        self.counts.waits += 1
        if self.prelude > 0:
            self._advance(1.0)
            return GameState.from_mcp_state(self._render())
        if self.mode == "realtime":
            target = (self.sim.step_no + 1) * STEP_SECONDS
            self._advance(max(self.latency, target - self.vt + 1e-9))
        else:
            # Lockstep: our heroes still pending play their standing orders after the timeout.
            self._advance(2.0)
            self._step()
        return GameState.from_mcp_state(self._render())

    def get_legal_actions(self) -> dict:
        self._advance(self.latency / 2)
        raw = state_for(self.sim, self.camp, mode=self.mode, pending=self._pending(), session_id=self.session_id,
                        elapsed_s=self.vt)
        if self._shown is not None:
            self._shown = dict(self._shown, legal_actions=raw["legal_actions"], state_version=raw["state_version"])
        return raw["legal_actions"] or {"session_id": self.session_id, "state_version": self.sim.step_no, "actions": []}

    def get_game_config(self, game_type: str) -> dict:
        return {"game_type": game_type, "note": "mock"}

    def play_action(self, *, action_id=None, action=None, state_version, reasoning_summary=None) -> dict:
        self.counts.plays += 1
        shown = self._shown or {}
        shown_entries = (shown.get("legal_actions") or {}).get("actions") or []
        shown_pending = (shown.get("simultaneous") or {}).get("your_pending_seats")
        if not isinstance(shown_pending, list):
            shown_pending = self._pending()
        # Was it legal in the state the agent was shown?
        _seat, _payload, error = server_check(self.sim, self.camp, shown_pending, shown_entries, action_id, action)
        if error is not None:
            self.counts.illegal += 1
            self.counts.illegal_detail.append(f"v{state_version}: {action_id or action}: {error}"[:200])
            self._advance(self.latency)
            raise _tool_error("INVALID_ACTION", error)
        if self.mode != "realtime":
            if state_version != self.sim.step_no:
                self.counts.stale += 1
                raise _tool_error("STALE_STATE", "state_version is out of date")
            if self.chaos and self._maybe():
                # The step moved on first (the other agent was done, the timeout ran out).
                self._step()
                self.counts.stale += 1
                raise _tool_error("STALE_STATE", "the step advanced")
        elif self.chaos and self._maybe():
            self.counts.unavailable += 1
            self._advance(self.latency)
            raise _tool_error("RUNTIME_TEMPORARILY_UNAVAILABLE", "the game server is busy")
        self._advance(self.latency)
        if self.mode == "realtime" and self.chaos and self._maybe():
            self.counts.races += 1
            raise _tool_error("INVALID_ACTION", "that macro is no longer available (its target died)")
        # Legal now? (In real time the world moved during the call.)
        seat, payload, error = server_check(self.sim, self.camp, self._pending(),
                                            [e for s in self._pending() for e in self.sim.legal_entries(s)],
                                            action_id, action)
        if error is None:
            error = self.sim.apply(seat, payload)
        if error is not None:
            self.counts.races += 1
            raise _tool_error("INVALID_ACTION", error)
        self.counts.accepted += 1
        self.acted.add(seat)
        answer: dict[str, Any] = {"accepted": True, "session_id": self.session_id,
                                  "state_version": self.sim.step_no, "status": "in_progress"}
        if self.mode != "realtime":
            if not self._pending():
                self._step()
            answer["state"] = self._render()
        return answer

    def get_result(self) -> dict:
        result = self.sim.result(self.camp)
        result["session_id"] = self.session_id
        return result

    def resign(self) -> dict:
        self.resigned = True
        self.sim.termination = "resignation"
        self.sim.winner = 3 - self.camp
        return {"session_id": self.session_id, "is_terminal": True, "status": "completed"}


@dataclass
class MockReport:
    camp: int
    mode: str
    chaos: float
    opponent: str
    steps: int
    termination: str
    winner: int | None
    crashed: str | None
    counts: Counts
    agent_stats: dict
    decide_ms_avg: float
    decide_ms_max: float
    wall_s: float


def run_mock(*, camp: int = 1, mode: str = "realtime", opponent: str = "upstream", chaos: float = 0.0,
             steps: int = 3000, seed: int = 0, latency: float = 0.1, knobs: dict | None = None) -> MockReport:
    from .. import create_agent

    sim = Sim(seed, SimConfig(max_steps=steps))
    holder: dict[str, SimSession] = {}
    opp = make_agent(opponent, lambda: holder["s"].vt, lambda d: None)
    session = SimSession(sim, camp, mode, opp, latency=latency, chaos=chaos, seed=seed,
                         prelude=3 if chaos else 0)
    holder["s"] = session
    lines: list[str] = []
    agent = create_agent(knobs, clock=session.clock, sleep=session.sleep, log=lines.append)
    context = DecisionContext(session_id=session.session_id, tournament_id=None, game_type=GAME_TYPE,
                              agent_id="ff", seat_position=camp - 1)
    crashed = None
    t0 = time.time()
    try:
        run_game(session, context, agent, sleep=session.sleep, now=session.clock, log=lines.append)
    except Exception as exc:  # noqa: BLE001 - the report counts it
        crashed = f"{type(exc).__name__}: {exc}"
    calls = max(1, agent.stats["decisions"])
    return MockReport(
        camp=camp, mode=mode, chaos=chaos, opponent=opponent, steps=sim.step_no, termination=sim.termination or "",
        winner=sim.winner, crashed=crashed, counts=session.counts, agent_stats=dict(agent.stats),
        decide_ms_avg=agent.decide_ms_total / calls, decide_ms_max=agent.decide_ms_max, wall_s=time.time() - t0,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--opponent", default="upstream")
    ap.add_argument("--chaos", type=float, default=None, help="one chaos level (default: 0 and 0.05)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--modes", default="realtime,lockstep")
    args = ap.parse_args(argv)
    chaos_levels = [args.chaos] if args.chaos is not None else [0.0, 0.05]
    reports = []
    for mode in args.modes.split(","):
        for chaos in chaos_levels:
            for camp in (1, 2):
                r = run_mock(camp=camp, mode=mode, opponent=args.opponent, chaos=chaos, steps=args.steps,
                             seed=args.seed + camp)
                reports.append(r)
                c = r.counts
                exceptions = sum(v for k, v in r.agent_stats.items() if k.endswith("exception"))
                print(f"{mode:9s} chaos={chaos:.2f} camp {camp}: {r.steps} steps, {r.termination or '-'} "
                      f"winner={r.winner} | crash={r.crashed or 'none'} agent_exceptions={exceptions} "
                      f"illegal={c.illegal} races={c.races} stale={c.stale} unavailable={c.unavailable} "
                      f"omitted_legal={c.omitted_legal} | plays={c.plays} accepted={c.accepted} reads={c.reads} "
                      f"waits={c.waits} | decide {r.decide_ms_avg:.2f} ms avg, {r.decide_ms_max:.1f} max "
                      f"| fallbacks={r.agent_stats.get('fallbacks', 0)} ({r.wall_s:.0f}s)")
                for line in c.illegal_detail[:5]:
                    print("    illegal:", line)
    total_steps = sum(r.steps for r in reports)
    crashes = sum(1 for r in reports if r.crashed) + sum(
        sum(v for k, v in r.agent_stats.items() if k.endswith("exception")) for r in reports)
    illegal = sum(r.counts.illegal for r in reports)
    print(f"TOTAL: {len(reports)} matches, {total_steps} steps, crashes={crashes}, illegal answers={illegal}, "
          f"decide max {max(r.decide_ms_max for r in reports):.1f} ms, "
          f"avg {statistics.mean(r.decide_ms_avg for r in reports):.2f} ms")
    return 0 if crashes == 0 and illegal == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
