"""Two agents play a sim match through the platform's interface.

``state_for`` renders the sim as one camp's ``get_game_state`` payload in the
documented shape (/skill/hok section 3; upstream's ``tests/test_hok.py``).
``play_match`` drives two agents the way the runner and the server would:

- **real time**: the sim steps every 0.2 s of virtual time whatever the agents
  do. Each agent has its own virtual clock: a sent action costs ``latency``
  (live: ``play_action`` takes a median 0.204 s and returns the new state), a
  ``WAIT`` lasts until the next step
  (``wait_for_update``), and the agent's own ``sleep`` (its pacing) is
  virtual too. Heroes without a fresh action play their standing order.
- **lockstep**: each step, each agent answers its pending heroes one call at
  a time; a ``WAIT`` leaves the rest on their standing orders (the server's
  action timeout).

Every answer is checked like the server would: a macro must be on offer for
that pending hero now, an ``action_id`` must be listed, a raw button's fields
must come from its ``legal_*`` lists. Refused answers are reported to the
agent's ``on_action_result`` as ``INVALID_ACTION`` and counted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from altruagent import WAIT, DecisionContext, GameState
from altruagent.runner import DecisionError, _validate_decision

from .engine import STEP_SECONDS, Sim, SimConfig

GAME_TYPE = "honor_of_kings_5v5"


def seats_of(camp: int) -> list[int]:
    return list(range(5)) if camp == 1 else list(range(5, 10))


def state_for(sim: Sim, camp: int, *, mode: str, pending: list[int], session_id: str = "sim",
              time_limit_s: float = 1800.0, elapsed_s: float | None = None,
              action_timeout_s: float = 2.0) -> dict[str, Any]:
    seats = seats_of(camp)
    entries = [e for s in pending for e in sim.legal_entries(s)]
    standing = {str(s): sim.hero(s).order for s in seats if sim.hero(s).order}
    elapsed = sim.t if elapsed_s is None else elapsed_s
    terminal = sim.termination is not None
    pacing = {"mode": "realtime", "step_interval_s": STEP_SECONDS} if mode == "realtime" else \
        {"mode": "lockstep", "action_timeout_s": action_timeout_s}
    return {
        "session_id": session_id, "game_type": GAME_TYPE, "runtime_adapter": "hok",
        "status": "completed" if terminal else "in_progress",
        "phase": "finished" if terminal else "moving",
        "is_current_actor": bool(pending) and not terminal, "is_terminal": terminal,
        "state_version": sim.step_no, "pacing": pacing, "seats": seats, "camp": camp,
        "max_steps": sim.cfg.max_steps, "time_limit_s": time_limit_s,
        "seconds_left": max(0.0, round(time_limit_s - elapsed, 1)), "action_timeout_s": action_timeout_s,
        "observation": {"seats": {str(s): {"summary": sim.summary(s)} for s in seats}},
        "simultaneous": {"your_pending_seats": list(pending), "standing_orders": standing,
                         "waiting_for": [], "last_fallback_players": []},
        "legal_actions": {"session_id": session_id, "state_version": sim.step_no, "actions": entries}
        if pending and not terminal else None,
        "current_actor": None,
    }


def server_check(sim: Sim, camp: int, pending: list[int], entries: list[dict], action_id: str | None,
                 action: dict | None) -> tuple[int | None, dict | None, str | None]:
    """Like the server: (seat, action to apply, error)."""
    listed = {e["action_id"]: e for e in entries}
    if action is None:
        entry = listed.get(action_id or "")
        if entry is None:
            return None, None, f"{action_id!r} is not a legal action now"
        payload = dict(entry["input"]["action"])
        if any(k.startswith("legal_") for k in payload):
            return None, None, f"{action_id} needs its fields"
        seat = payload.get("seat")
        return seat, payload, None
    seat = action.get("seat")
    if not isinstance(seat, int) or isinstance(seat, bool) or seat not in seats_of(camp):
        return None, None, f"seat {seat!r} isn't yours"
    if seat not in pending:
        return None, None, f"seat {seat} already acted this step"
    if "macro" in action:
        if f"s{seat}:{action['macro']}" not in listed:
            return None, None, f"{action['macro']} is not available for seat {seat} now"
        return seat, {"macro": action["macro"], "seat": seat}, None
    button = action.get("which_button")
    entry = listed.get(f"s{seat}:button_{button}")
    if entry is None:
        return None, None, f"button {button!r} not listed for seat {seat}"
    payload = entry["input"]["action"]
    for key, legal in payload.items():
        if key.startswith("legal_") and action.get(key[len("legal_"):]) not in legal:
            return None, None, f"{key[len('legal_'):]}={action.get(key[len('legal_'):])!r} not in {key}"
    return seat, dict(action), None


@dataclass
class Side:
    agent: Any
    name: str
    vt: float = 0.0               # this agent's virtual clock
    calls: int = 0
    sends: int = 0
    waits: int = 0
    illegal: int = 0
    crashes: int = 0
    refusals: list[str] = field(default_factory=list)
    think_ms: float = 0.0
    think_max_ms: float = 0.0


@dataclass
class MatchResult:
    seed: int
    mode: str
    names: dict[int, str]
    winner: int | None
    termination: str
    steps: int
    structure_damage: dict[int, float]
    kills: dict[int, int]
    sides: dict[int, Side]

    def score(self, camp: int) -> float:
        return 0.5 if self.winner is None else (1.0 if self.winner == camp else 0.0)


def _decide(side: Side, state: GameState, context: DecisionContext):
    side.calls += 1
    t0 = time.perf_counter()
    try:
        decision = side.agent.choose_action(state, context)
    except Exception:  # noqa: BLE001 - counted: an agent crash ends its control in the real runner
        side.crashes += 1
        decision = WAIT
    ms = (time.perf_counter() - t0) * 1000
    side.think_ms += ms
    side.think_max_ms = max(side.think_max_ms, ms)
    return decision


def _submit(sim: Sim, camp: int, side: Side, decision, state: GameState, pending: list[int],
            entries: list[dict], context: DecisionContext) -> int | None:
    """Validate (runner, then server), apply, report. Returns the seat that acted."""
    try:
        checked = _validate_decision(decision, state.legal_actions)
    except DecisionError as exc:
        side.crashes += 1          # the runner would end the match for this agent
        side.refusals.append(f"runner: {exc}"[:200])
        return None
    if not hasattr(checked, "action_id"):  # RESIGN: never expected from a team bot
        side.crashes += 1
        side.refusals.append(f"resigned: {checked!r}")
        return None
    seat, payload, error = server_check(sim, camp, pending, entries, checked.action_id, checked.action)
    if error is None:
        error = sim.apply(seat, payload)
    hook = getattr(side.agent, "on_action_result", None)
    if error is not None:
        side.illegal += 1
        side.refusals.append(error[:200])
        if callable(hook):
            hook({"error": "INVALID_ACTION", "detail": error}, context)
        return None
    side.sends += 1
    if callable(hook):
        hook({"accepted": True, "session_id": "sim", "state_version": sim.step_no, "stepped": False}, context)
    return seat


def play_match(make_agents: Callable[[Callable[[], float], Callable[[float], None], int], Any] | None = None, *,
               agents: dict[int, Any] | None = None, names: dict[int, str] | None = None, seed: int = 0,
               mode: str = "realtime", config: SimConfig | None = None, latency: float = 0.2,
               max_calls_per_step: int = 12) -> MatchResult:
    """Play one match. ``make_agents(clock, sleep, camp)`` builds each camp's agent with its
    virtual clock and sleep (so pacing is virtual); or pass ready ``agents``."""
    sim = Sim(seed, config)
    sides: dict[int, Side] = {}
    for camp in (1, 2):
        holder: dict[str, Side] = {}

        def clock(h=holder) -> float:
            return h["side"].vt

        def sleep(d: float, h=holder) -> None:
            h["side"].vt += max(0.0, d)

        agent = agents[camp] if agents else make_agents(clock, sleep, camp)
        side = Side(agent=agent, name=(names or {}).get(camp, type(agent).__name__))
        holder["side"] = side
        sides[camp] = side
    contexts = {c: DecisionContext(session_id=f"sim-{seed}", tournament_id=None, game_type=GAME_TYPE,
                                   agent_id=f"camp{c}", seat_position=c - 1) for c in (1, 2)}
    while sim.termination is None:
        step_end = sim.t + STEP_SECONDS
        for camp in (1, 2):
            side = sides[camp]
            acted: set[int] = set()
            calls = 0
            if mode == "realtime" and side.vt >= step_end:
                continue
            while calls < max_calls_per_step:
                pending = [s for s in seats_of(camp) if s not in acted]
                raw = state_for(sim, camp, mode=mode, pending=pending, elapsed_s=sim.t)
                state = GameState.from_mcp_state(raw)
                entries = (raw.get("legal_actions") or {}).get("actions") or []
                if not pending or not entries:
                    break
                decision = _decide(side, state, contexts[camp])
                calls += 1
                if decision is WAIT:
                    side.waits += 1
                    if mode == "realtime":
                        side.vt = max(side.vt, step_end)
                    break
                seat = _submit(sim, camp, side, decision, state, pending, entries, contexts[camp])
                if seat is not None:
                    acted.add(seat)
                if mode == "realtime":
                    side.vt += latency
                    if side.vt >= step_end:
                        break
        sim.step()
    return MatchResult(seed=seed, mode=mode, names={c: sides[c].name for c in (1, 2)}, winner=sim.winner,
                       termination=sim.termination or "", steps=sim.step_no,
                       structure_damage=dict(sim.structure_damage), kills=dict(sim.kills), sides=sides)
