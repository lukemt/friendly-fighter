"""A template for a new bot: our scripted bot with one decision changed.

Register and play it:

    lab bots add example --kind module --factory bots.example:create_agent --desc "..." --tags ours
    lab play example ff-default --games 12

A bot is any object with ``choose_action(state, context)`` (and optionally
``on_action_result(result, context)``), returned fresh for each game by ``create_agent()``:

- ``state.raw`` is the platform's state: ``observation`` (units, buildings, economy, military,
  visible enemies, events...), ``legal_actions``, ``time``, ``last_orders``;
- ``context.game_config`` is ``get_game_config('red_alert')`` (rules: costs, prerequisites, maps);
- return ``altruagent.WAIT`` or ``WithReasoning({"type": "orders", "orders": [...]}, "why")``
  (up to 20 orders; the order formats are in the game config's ``order_schema``).

This one is ``RedAlertBot`` (agent/redalert_bot.py) that, once it has won a fight at home
(more kills than losses and no enemy near the base), sends its army after the enemy for one wave:
a turtle that scores. Change ``decide`` to try other ideas; the lab gives it a new version every
time the file changes.
"""

from __future__ import annotations

from agent.redalert_bot import Knobs, RedAlertBot
from altruagent import WAIT, WithReasoning


class CounterBot:
    def __init__(self, threshold: int = 12) -> None:
        self.bot = RedAlertBot(knobs=Knobs(posture="defend", attack_threshold=20), log=lambda line: None)
        self.threshold = threshold

    def choose_action(self, state, context):
        obs = state.raw.get("observation") or {}
        mil = obs.get("military") or {}
        ahead = (mil.get("kills_cost") or 0) > (mil.get("deaths_cost") or 0)
        army = sum(1 for u in obs.get("units") or [] if u.get("type") not in ("harv", "mcv") and not u.get("wreck"))
        # Defend until a won defense, then counterattack with a big army.
        posture = "attack" if ahead and army >= self.threshold else "defend"
        if self.bot.knobs.posture != posture:
            self.bot.set_knobs(Knobs(posture=posture, attack_threshold=self.threshold if posture == "attack" else 20))
        move = self.bot.choose_action(state, context)
        if isinstance(move, WithReasoning):
            return WithReasoning(move.action, f"[{posture}] {move.reasoning_summary or ''}"[:280])
        return move if move is not None else WAIT

    def on_action_result(self, result, context):
        self.bot.on_action_result(result, context)


def create_agent(threshold: int = 12) -> CounterBot:
    return CounterBot(threshold)
