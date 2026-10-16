"""Opponents for the sim, and a factory from short names.

- ``button0``: the placeholder ``agent/agent.py`` (first legal action: the first hero's
  do-nothing button, then the next pending hero's, so every hero idles).
- ``advance``: every hero on ``macro_advance`` for good.
- ``upstream``: the starter's built-in rules (``examples/llm/hok.py`` with no model:
  retreat below 25% HP, skills/attack on anything closer than 9000, else advance;
  re-planned every 3 s).
- ``ff-hok`` / ``ff-hok:{"knob": value}`` / ``ff-hok@0.2``: our bot (``bots.hok``), with knobs
  or another real-time pace.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from altruagent import WAIT, DecisionContext, GameState

from ..view import offers_by_seat, read_view


class Placeholder:
    """``agent/agent.py``'s fallback: ``state.legal_actions[0]``."""

    def choose_action(self, state: GameState, context: DecisionContext):
        return state.legal_actions[0] if state.legal_actions else WAIT


class AlwaysAdvance:
    def choose_action(self, state: GameState, context: DecisionContext):
        view = read_view(state)
        for seat in view.pending:
            offers = view.offers.get(seat)
            if offers is None:
                continue
            if "macro_advance" in offers.macros:
                if view.realtime and view.standing.get(seat) == "macro_advance":
                    continue
                return {"macro": "macro_advance", "seat": seat}
            if not view.realtime and offers.hold() is not None:
                return offers.hold().action
        return WAIT


def make_agent(spec: str, clock: Callable[[], float], sleep: Callable[[float], None]) -> Any:
    name, _, arg = spec.partition(":")
    if name == "button0":
        return Placeholder()
    if name == "advance":
        return AlwaysAdvance()
    if name == "upstream":
        from examples.llm.hok import HokPlayer

        return HokPlayer(None, log=lambda _l: None, clock=clock)
    if name.startswith("ff-hok"):
        from .. import create_agent

        knobs = json.loads(arg) if arg else {}
        extra = {}
        if "@" in name:  # ff-hok@0.2: another real-time pace
            extra["pace_s"] = float(name.split("@", 1)[1])
        return create_agent(knobs, clock=clock, sleep=sleep, log=lambda _l: None, reasoning=False, **extra)
    raise ValueError(f"unknown agent {spec!r} (button0, advance, upstream, ff-hok[@pace][:{{knobs}}])")


__all__ = ["AlwaysAdvance", "Placeholder", "make_agent", "offers_by_seat"]
