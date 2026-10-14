"""Simple baseline bots for the lab (``lab/bots.json``): opponents that show whether ours can finish a
weak or absent player.

- ``create_idle``: never acts (as an agent whose model never answers would);
- ``create_builder``: our agent's economy only (deploys, builds, harvests, repairs), no army and no
  orders to units: a base to destroy.
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


def create_idle() -> Idle:
    return Idle()


def create_builder() -> Builder:
    return Builder()
