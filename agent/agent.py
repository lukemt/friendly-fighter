"""friendly-fighter: the agent the runtime loads (``python -m agent ...``).

``create_agent()`` is called once per match, in that match's own process,
and returns a fresh ``FriendlyFighter``:

- **Red Alert** (``red_alert``), the game this agent is built for, is played
  by the scripted ``RedAlertBot`` (agent/redalert_bot.py) on every view, with
  the optional OpenAI ``Commander`` (agent/commander.py) tuning its strategy
  in the background when ``OPENAI_API_KEY`` is set. The game never waits on
  the model.
- **Pokémon** (``pokemon_vgc_doubles_draft``) is played by the scripted
  ``bots.pokemon`` agent: draft, Team Preview and doubles turns, no model calls.
- **Any other game** (in case this agent is ever seated in Werewolf) goes to
  the starter's LLM example when ``OPENAI_API_KEY`` is set, and otherwise
  plays the first legal action.
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
POKEMON = "pokemon_vgc_doubles_draft"
# remote/control.json is looked at once a second at most, not on every view: on the shared
# workspace filesystem each look costs about a millisecond (a fifth of a worker's CPU).
CONTROL_EVERY_SECONDS = 1.0
# FF_THINK_MS: wait this long before every decision, like an agent that thinks slowly (an LLM's
# latency) -- for local test games of how such an agent fares; unset, there's no wait.
THINK_SECONDS = float(os.environ.get("FF_THINK_MS") or 0) / 1000.0


def knobs_from_env() -> Knobs:
    """``FF_KNOBS='{"posture": "eco", "attack_threshold": 14}'``: the bot starts with these knobs
    instead of the defaults (for local test games of other strategies; unknown keys are ignored)."""
    try:
        wanted = json.loads(os.environ.get("FF_KNOBS") or "{}")
    except ValueError:
        return Knobs()
    return knobs_from_dict(wanted if isinstance(wanted, dict) else {})


def _is_realtime(state: GameState) -> bool:
    pacing = state.raw.get("pacing")
    return isinstance(pacing, dict) and pacing.get("mode") == "realtime"


def knobs_from_dict(wanted: dict | None) -> Knobs:
    """Knobs with these values over the defaults (unknown keys are ignored)."""
    knobs = Knobs()
    for f in fields(Knobs):
        if wanted and f.name in wanted:
            setattr(knobs, f.name, type(getattr(knobs, f.name))(wanted[f.name]))
    return knobs


class FriendlyFighter:
    #: The bot's name in the recordings (and so in ``lab import``); ``ff-live`` when knobs change it.
    BOT = "ff-default"

    def __init__(self, *, knobs: Knobs | None = None, think_seconds: float | None = None,
                 record: bool | None = None, controls: bool = True) -> None:
        """``knobs``/``think_seconds``: as ``FF_KNOBS``/``FF_THINK_MS`` (which they override) for one agent
        object (the lab plays several in one process); ``record``: as ``FF_RECORD``; ``controls``: whether
        the remote-control, console and probe files may take this seat over."""
        self._knobs = knobs
        self._think_seconds = THINK_SECONDS if think_seconds is None else think_seconds
        self._controls = controls
        self._bot: RedAlertBot | None = None
        self._commander: Commander | None = None
        self._fallback: Any = None
        self._pokemon_bot: Any = None
        self._fallback_ready = False
        custom = knobs is not None or bool(os.environ.get("FF_KNOBS"))
        self._recorder = Recorder(enabled=record, bot="ff-live" if custom and self.BOT == "ff-default" else self.BOT)
        self._remote: RemoteSeat | None = None
        self._probe: Probe | None = None
        self._probe_checked = False
        self._console: ConsoleSeat | None = None
        self._control: dict = {}
        self._control_read = float("-inf")

    # -- moves ----------------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        self._game = context.game_type  # _other() routes Werewolf by it
        self._recorder.state(state, context)
        if self._think_seconds:
            time.sleep(self._think_seconds)  # FF_THINK_MS: play like a slower agent (local test games)
        move = self._choose(state, context)
        self._recorder.action(move, context)
        return move

    def _choose(self, state: GameState, context: DecisionContext):
        if context.game_type == RED_ALERT or (context.game_type is None and _is_realtime(state)):
            bot = self._red_alert()
            if not self._controls:
                return bot.choose_action(state, context)
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
        if context.game_type == POKEMON:
            return self._pokemon().choose_action(state, context)
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
        elif self._pokemon_bot is not None:
            self._pokemon_bot.on_action_result(result, context)
        elif self._fallback is not None and hasattr(self._fallback, "on_action_result"):
            self._fallback.on_action_result(result, context)

    def bot_result(self, result: dict, context: DecisionContext) -> None:
        if self._bot is not None:
            self._bot.on_action_result(result, context)

    def choose_message(self, state: GameState, context: DecisionContext):
        self._game = context.game_type
        other = self._other()
        if other is not None and hasattr(other, "choose_message"):
            return other.choose_message(state, context)
        return TERMINATE_MESSAGING

    # -- players -------------------------------------------------------------------

    def _red_alert(self) -> RedAlertBot:
        if self._bot is None:
            self._bot = RedAlertBot(knobs=self._knobs if self._knobs is not None else knobs_from_env())
            self._commander = commander_from_env(self._bot)
            if self._commander is not None:
                self._commander.start()
                print_line("[ff] red_alert: scripted controller + OpenAI commander")
            else:
                print_line("[ff] red_alert: scripted controller (no OPENAI_API_KEY: commander off)")
        return self._bot

    def _pokemon(self) -> Any:
        if self._pokemon_bot is None:
            from bots.pokemon import create_agent as create_pokemon_agent

            self._pokemon_bot = create_pokemon_agent()
            print_line("[ff] pokemon_vgc_doubles_draft: scripted bot (bots.pokemon)")
        return self._pokemon_bot

    def _other(self) -> Any:
        if getattr(self, "_game", None) == "werewolf":
            return self._werewolf()
        if not self._fallback_ready:
            self._fallback_ready = True
            if os.environ.get("OPENAI_API_KEY"):
                from examples.llm_agent import create_agent as create_llm_agent

                self._fallback = create_llm_agent()
        return self._fallback

    def _werewolf(self) -> Any:
        """The scripted Werewolf bot (bots.werewolf); kept in ``_fallback`` so on_action_result reaches it."""
        if self._fallback is None:
            from bots.werewolf import create_agent as create_werewolf_agent

            self._fallback = create_werewolf_agent(record=self._recorder._write)
            print_line("[ff] werewolf: scripted bot (bots.werewolf)")
        return self._fallback


def create_agent(knobs: dict | None = None, *, think_ms: float | None = None, record: bool | None = None,
                 controls: bool = True) -> FriendlyFighter:
    """The runtime calls this with no arguments; the lab (lab/bots.py) passes a bot's settings."""
    load_dotenv()  # no-op if already loaded; never overrides real env vars
    return FriendlyFighter(knobs=knobs_from_dict(knobs) if knobs is not None else None,
                           think_seconds=None if think_ms is None else think_ms / 1000.0, record=record,
                           controls=controls)
