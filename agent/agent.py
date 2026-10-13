"""friendly-fighter: the agent the runtime loads (``python -m agent ...``).

``create_agent()`` is called once per match, in that match's own process,
and returns a fresh ``FriendlyFighter``:

- **Red Alert** (``red_alert``), the game this agent is built for, is played
  by the scripted ``RedAlertBot`` (agent/redalert_bot.py) on every view, with
  the optional OpenAI ``Commander`` (agent/commander.py) tuning its strategy
  in the background when ``OPENAI_API_KEY`` is set. The game never waits on
  the model.
- **Any other game** (in case this agent is ever seated in Werewolf or
  Pokémon) goes to the starter's LLM example when ``OPENAI_API_KEY`` is set,
  and otherwise plays the first legal action.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

from altruagent.console import print_line
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState

from .commander import Commander, commander_from_env
from .recorder import Recorder
from .remote import RemoteSeat, read_control
from .redalert_bot import RedAlertBot

RED_ALERT = "red_alert"


def _is_realtime(state: GameState) -> bool:
    pacing = state.raw.get("pacing")
    return isinstance(pacing, dict) and pacing.get("mode") == "realtime"


class FriendlyFighter:
    def __init__(self) -> None:
        self._bot: RedAlertBot | None = None
        self._commander: Commander | None = None
        self._fallback: Any = None
        self._fallback_ready = False
        self._recorder = Recorder()
        self._remote: RemoteSeat | None = None

    # -- moves ----------------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        self._recorder.state(state, context)
        move = self._choose(state, context)
        self._recorder.action(move, context)
        return move

    def _choose(self, state: GameState, context: DecisionContext):
        if context.game_type == RED_ALERT or (context.game_type is None and _is_realtime(state)):
            bot = self._red_alert()
            control = read_control()
            if control.get("enabled") and str(control.get("seat")) == str(context.seat_position):
                if self._remote is None:
                    self._remote = RemoteSeat(bot, context.seat_position)
                return self._remote.choose_action(state, context, autopilot=control.get("autopilot", True) is not False)
            return bot.choose_action(state, context)
        other = self._other()
        if other is not None:
            return other.choose_action(state, context)
        return state.legal_actions[0]

    def on_action_result(self, result: dict, context: DecisionContext) -> None:
        self._recorder.result(result, context)
        if self._remote is not None:
            self._remote.on_action_result(result, context)
        elif self._bot is not None:
            self._bot.on_action_result(result, context)
        elif self._fallback is not None and hasattr(self._fallback, "on_action_result"):
            self._fallback.on_action_result(result, context)

    def choose_message(self, state: GameState, context: DecisionContext):
        other = self._other()
        if other is not None and hasattr(other, "choose_message"):
            return other.choose_message(state, context)
        return TERMINATE_MESSAGING

    # -- players -------------------------------------------------------------------

    def _red_alert(self) -> RedAlertBot:
        if self._bot is None:
            self._bot = RedAlertBot()
            self._commander = commander_from_env(self._bot)
            if self._commander is not None:
                self._commander.start()
                print_line("[ff] red_alert: scripted controller + OpenAI commander")
            else:
                print_line("[ff] red_alert: scripted controller (no OPENAI_API_KEY: commander off)")
        return self._bot

    def _other(self) -> Any:
        if not self._fallback_ready:
            self._fallback_ready = True
            if os.environ.get("OPENAI_API_KEY"):
                from examples.llm_agent import create_agent as create_llm_agent

                self._fallback = create_llm_agent()
        return self._fallback


def create_agent() -> FriendlyFighter:
    load_dotenv()  # no-op if already loaded; never overrides real env vars
    return FriendlyFighter()
