"""Baseline opponents for the harness, playing through the same GameState/template interface.

- ``random``: a random legal card, lineup and slot choice (any listed target).
- ``maxdamage``: drafts and brings the highest base-stat totals, then every turn uses the move with
  the most base power x STAB x type effectiveness on the best foe target; switches only when forced.
- ``smoke``: examples/smoke_agent.py (first legal option everywhere).
"""

from __future__ import annotations

import random
from typing import Any

from altruagent import GameState

from ..dex import effectiveness, move as move_data, species as species_data, to_id


def _template(state: GameState) -> dict | None:
    first = state.legal_actions[0]
    t = first.input.get("action") if isinstance(first.input, dict) else None
    return t if isinstance(t, dict) else None


def random_turn(template: dict, rng: random.Random) -> dict:
    slots = sorted(template["slots"], key=lambda s: s.get("slot", 0))
    only_pass = all(all(o["type"] == "pass" for o in s["options"]) for s in slots)
    for _ in range(50):
        out = {}
        for i, slot in enumerate(slots):
            o = rng.choice(slot["options"])
            if o["type"] == "move":
                pick = {"type": "move", "move_id": o["move_id"]}
                if o.get("targets"):
                    pick["target"] = rng.choice(o["targets"])
                out[f"slot_{i}"] = pick
            elif o["type"] == "switch":
                out[f"slot_{i}"] = {"type": "switch", "species": o["species"]}
            else:
                out[f"slot_{i}"] = {"type": "pass"}
        a, b = out["slot_0"], out["slot_1"]
        if a["type"] == b["type"] == "switch" and a["species"] == b["species"]:
            continue
        if a["type"] == b["type"] == "pass" and not only_pass:
            continue
        return {"type": "doubles_turn", **out}
    from examples import smoke_agent

    return smoke_agent._doubles_turn(template)  # noqa: SLF001


class RandomBaseline:
    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)

    def choose_action(self, state: GameState, context: Any = None) -> Any:
        t = _template(state)
        if t and t.get("type") == "select_lineup":
            roster = list(t["roster"])
            bring = self.rng.sample(roster, 4)
            return {"type": "select_lineup", "bring": bring, "leads": bring[:2]}
        if t and t.get("type") == "doubles_turn":
            return random_turn(t, self.rng)
        return self.rng.choice(state.legal_actions)


def _bst(name: object) -> int:
    sp = species_data(name)
    return sum(sp["bs"].values()) if sp else 0


class MaxDamageBaseline:
    def choose_action(self, state: GameState, context: Any = None) -> Any:
        t = _template(state)
        obs = state.raw.get("observation") or {}
        if t and t.get("type") == "select_lineup":
            roster = sorted(t["roster"], key=lambda s: -_bst(s))
            return {"type": "select_lineup", "bring": roster[:4], "leads": roster[:2]}
        if t and t.get("type") == "doubles_turn":
            return self._turn(t)
        cards = {c["card_id"]: c for c in obs.get("available_cards") or []}
        return max(state.legal_actions, key=lambda a: _bst(cards.get(a.action_id.split(":", 1)[1], {}).get("species")))

    def _turn(self, template: dict) -> dict:
        slots = sorted(template["slots"], key=lambda s: s.get("slot", 0))
        out, taken = {}, None
        for i, slot in enumerate(slots):
            me = species_data((slot.get("active") or {}).get("species"))
            best, best_v = None, -1.0
            for o in slot["options"]:
                if o["type"] != "move":
                    continue
                mv = move_data(o["move_id"])
                if not mv:
                    continue
                stab = 1.5 if me and mv["type"] in me["types"] else 1.0
                foes = {t["target"]: species_data(t.get("species")) for t in o.get("target_options") or []
                        if t.get("side") == "opponent"}
                if mv["cat"] == "Status" or not mv["bp"]:
                    v, target = 0.0, (o.get("targets") or [None])[0]
                    if target is not None and target < 0 and any(x > 0 for x in o["targets"]):
                        target = next(x for x in o["targets"] if x > 0)
                elif o.get("targets") and o["targets"] != [0]:
                    target, v = None, -1.0
                    for tgt in o["targets"]:
                        if tgt <= 0:
                            continue
                        sp = foes.get(tgt)
                        eff = effectiveness(mv["type"], sp["types"]) if sp else 1.0
                        if mv["bp"] * stab * eff > v:
                            target, v = tgt, mv["bp"] * stab * eff
                    if target is None:
                        target, v = o["targets"][0], 0.0
                else:
                    live = [sp for sp in foes.values() if sp] or [None]
                    effs = [effectiveness(mv["type"], sp["types"]) if sp else 1.0 for sp in live]
                    spread = mv["target"] in ("allAdjacentFoes", "allAdjacent")
                    v = mv["bp"] * stab * (0.75 * sum(effs) if spread else max(effs))
                    target = 0 if o.get("targets") else None
                if v > best_v:
                    best_v = v
                    best = {"type": "move", "move_id": o["move_id"]}
                    if target is not None:
                        best["target"] = target
            if best is None:
                for o in slot["options"]:
                    if o["type"] == "switch" and o["species"] != taken:
                        best = {"type": "switch", "species": o["species"]}
                        taken = o["species"]
                        break
            if best is None:
                best = {"type": "pass"}
            out[f"slot_{i}"] = best
        a, b = out["slot_0"], out["slot_1"]
        if a["type"] == b["type"] == "pass":
            only_pass = all(all(o["type"] == "pass" for o in s["options"]) for s in slots)
            if not only_pass:
                from examples import smoke_agent

                return smoke_agent._doubles_turn(template)  # noqa: SLF001
        return {"type": "doubles_turn", **out}


class SmokeBaseline:
    def choose_action(self, state: GameState, context: Any = None) -> Any:
        from examples import smoke_agent

        return smoke_agent.choose_action(state, context)


def make_baseline(name: str) -> Any:
    name = to_id(name)
    if name == "random":
        return RandomBaseline()
    if name == "maxdamage":
        return MaxDamageBaseline()
    if name == "smoke":
        return SmokeBaseline()
    raise ValueError(f"unknown baseline {name!r}")
