"""Simple baseline bots for the lab (``lab/bots.json``): opponents that show whether ours can finish a
weak or absent player.

- ``create_idle``: never acts (as an agent whose model never answers would);
- ``create_builder``: our agent's economy only (deploys, builds, harvests, repairs), no army and no
  orders to units: a base to destroy.
- ``create_chaotic``: our agent's economy and production, but the army goes wherever a coin says,
  every 15-40 s: part of it to our base, part to a random spot, some of it nowhere (an erratic
  player, as an LLM agent with little sense of the game may be).
"""

from __future__ import annotations

from altruagent import WAIT, WithReasoning

ARMY_CMDS = {"attack", "attack_move", "move", "stop", "guard", "scatter"}


class Idle:
    def choose_action(self, state, context):
        return WAIT


class Builder:
    def __init__(self) -> None:
        from agent.agent import create_agent

        self.agent = create_agent(record=False, controls=False)

    def choose_action(self, state, context):
        move = self.agent.choose_action(state, context)
        if not isinstance(move, WithReasoning):
            return move
        orders = [o for o in move.action.get("orders", []) if o.get("cmd") not in ARMY_CMDS
                  and not (o.get("cmd") == "train" and o.get("item") != "harv")]
        self._sent = len(orders)
        if not orders:
            return WAIT
        return WithReasoning({"type": "orders", "orders": orders}, "builder: economy only")

    def on_action_result(self, result, context):
        pass  # (the inner bot's indexes no longer match: it only loses its refusal cooldowns)


class Chaotic:
    def __init__(self, seed: int | None = None) -> None:
        import random

        from agent.agent import create_agent

        self.agent = create_agent(knobs={"posture": "defend"}, record=False, controls=False)
        self.rng = random.Random(seed)
        self.next_at = 0.0

    def choose_action(self, state, context):
        move = self.agent.choose_action(state, context)
        orders = [o for o in (move.action.get("orders", []) if isinstance(move, WithReasoning) else [])
                  if o.get("cmd") not in ARMY_CMDS]
        raw = state.raw
        obs = raw.get("observation") or {}
        now = float((raw.get("time") or {}).get("game_seconds") or 0.0)
        army = [u["id"] for u in obs.get("units") or []
                if u.get("type") not in ("harv", "mcv") and not u.get("wreck") and isinstance(u.get("id"), int)]
        if army and now >= self.next_at:
            self.next_at = now + self.rng.uniform(15, 40)
            share = self.rng.sample(army, max(1, int(len(army) * self.rng.uniform(0.2, 1.0))))
            bounds = (raw.get("map") or {}).get("bounds") or [0, 0, 64, 64]
            enemy = (raw.get("legal_actions") or {}).get("enemy_base")
            spot = [self.rng.randint(bounds[0] + 2, bounds[0] + bounds[2] - 3),
                    self.rng.randint(bounds[1] + 2, bounds[1] + bounds[3] - 3)]
            to = enemy if enemy and self.rng.random() < 0.5 else spot
            orders = orders[:19] + [{"cmd": self.rng.choice(["attack_move", "move"]), "units": share[:50], "to": list(to)}]
        self._sent = len(orders)
        if not orders:
            return WAIT
        return WithReasoning({"type": "orders", "orders": orders[:20]}, "chaotic")

    def on_action_result(self, result, context):
        pass


def create_chaotic(seed: int = 7) -> Chaotic:
    return Chaotic(seed)


def create_idle() -> Idle:
    return Idle()


def create_builder() -> Builder:
    return Builder()
