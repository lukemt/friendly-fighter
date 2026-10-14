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

import json
import os
import time
from dataclasses import fields
from typing import Any

from dotenv import load_dotenv

from altruagent.console import print_line
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState

from .commander import Commander, commander_from_env
from .recorder import Recorder
from .console import ConsoleSeat, read_console_control
from .probe import Probe, read_probe_control
from .remote import RemoteSeat, read_control
from .redalert_bot import Knobs, RedAlertBot

RED_ALERT = "red_alert"
# remote/control.json is looked at once a second at most, not on every view: on the shared
# workspace filesystem each look costs about a millisecond (a fifth of a worker's CPU).
CONTROL_EVERY_SECONDS = 1.0
# FF_THINK_MS: wait this long before every decision, like an agent that thinks slowly (an LLM's
# latency) -- for local test games of how such an agent fares; unset, there's no wait.
THINK_SECONDS = float(os.environ.get("FF_THINK_MS") or 0) / 1000.0


def knobs_from_env() -> Knobs:
    """``FF_KNOBS='{"posture": "eco", "attack_threshold": 14}'``: the bot starts with these knobs
    instead of the defaults (for local test games of other strategies; unknown keys are ignored)."""
    knobs = Knobs()
    try:
        wanted = json.loads(os.environ.get("FF_KNOBS") or "{}")
    except ValueError:
        return knobs
    for f in fields(Knobs):
        if f.name in wanted:
            setattr(knobs, f.name, type(getattr(knobs, f.name))(wanted[f.name]))
    return knobs


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
        self._probe: Probe | None = None
        self._probe_checked = False
        self._console: ConsoleSeat | None = None
        self._control: dict = {}
        self._control_read = float("-inf")

    # -- moves ----------------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        self._recorder.state(state, context)
        if THINK_SECONDS:
            time.sleep(THINK_SECONDS)  # FF_THINK_MS: play like a slower agent (local test games)
        move = self._choose(state, context)
        self._recorder.action(move, context)
        return move

    def _choose(self, state: GameState, context: DecisionContext):
        if context.game_type == RED_ALERT or (context.game_type is None and _is_realtime(state)):
            bot = self._red_alert()
            if not self._probe_checked:  # decided once per match, at its first view
                self._probe_checked = True
                mine = lambda c: c.get("enabled") and str(c.get("seat")) == str(context.seat_position)  # noqa: E731
                console, probe = read_console_control(), read_probe_control()
                if mine(console):
                    self._console = ConsoleSeat(bot, context, bot_on=console.get("bot", True) is not False)
                if mine(probe):
                    # With a console on this seat, the probe hands over instead of resigning.
                    resign = probe.get("resign_at_end", True) is not False and self._console is None
                    self._probe = Probe(bot, context, resign_at_end=resign)
            if self._probe is not None and not self._probe.finished:
                return self._probe.choose_action(state, context)
            if self._console is not None:
                return self._console.choose_action(state, context)
            now = time.monotonic()
            if now - self._control_read >= CONTROL_EVERY_SECONDS:
                self._control, self._control_read = read_control(), now
            control = self._control
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
        if self._probe is not None or self._console is not None:
            self.bot_result(result, context)
        elif self._remote is not None:
            self._remote.on_action_result(result, context)
        elif self._bot is not None:
            self._bot.on_action_result(result, context)
        elif self._fallback is not None and hasattr(self._fallback, "on_action_result"):
            self._fallback.on_action_result(result, context)

    def bot_result(self, result: dict, context: DecisionContext) -> None:
        if self._bot is not None:
            self._bot.on_action_result(result, context)

    def choose_message(self, state: GameState, context: DecisionContext):
        other = self._other()
        if other is not None and hasattr(other, "choose_message"):
            return other.choose_message(state, context)
        return TERMINATE_MESSAGING

    # -- players -------------------------------------------------------------------

    def _red_alert(self) -> RedAlertBot:
        if self._bot is None:
            self._bot = RedAlertBot(knobs=knobs_from_env())
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
