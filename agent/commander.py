"""friendly-fighter's optional OpenAI commander for Red Alert.

The scripted ``RedAlertBot`` plays every view by itself. When ``OPENAI_API_KEY``
is set, this commander runs beside it in a daemon thread: every
``INTERVAL_SECONDS`` it shows the model a short summary of the match and gets
back strategy knobs (posture, wave size, unit mix, target), which the bot
picks up on its next view. The game never waits for the model: a slow or
failing call just leaves the previous knobs in place.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Callable

from altruagent.console import print_line
from examples.llm.base import object_schema
from examples.llm.providers import LLMProvider, ProviderError, provider_from_env
from examples.llm.redalert import backoff_seconds

from .redalert_bot import Knobs, RedAlertBot

INTERVAL_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_CALLS = 120  # a cost guard: about 30 minutes at one call per 15 s

SCHEMA = object_schema(
    {
        "posture": {"type": "string", "enum": ["eco", "defend", "attack"]},
        "attack_threshold": {"type": "integer", "minimum": 4, "maximum": 30},
        "unit_focus": {"type": "string", "enum": ["infantry", "vehicles", "mixed"]},
        "target": {"type": "string", "enum": ["nearest", "enemy_base", "harvesters"]},
    }
)

SYSTEM_PROMPT = (
    "You are the strategic commander of one side in a live 1v1 Command & Conquer: Red Alert "
    "match (OpenRA) against another AI. A fast scripted controller handles every order (building, "
    "harvesting, training, defending, attack waves); you only set its strategy knobs, about every "
    "15 seconds:\n"
    "- posture: 'eco' (extra refinery and harvesters, no defenses yet), 'defend' (hold the army at "
    "home, no attack waves), or 'attack' (launch waves when the army reaches attack_threshold).\n"
    "- attack_threshold: combat units needed before a wave (4-30). Smaller = earlier, riskier "
    "pressure; larger = stronger but later pushes.\n"
    "- unit_focus: 'infantry', 'vehicles' or 'mixed' production.\n"
    "- target: 'nearest' (fight what the army meets, then known enemy buildings), 'enemy_base' "
    "(push to buildings), or 'harvesters' (snipe enemy harvesters you can see).\n"
    "Victory = destroy every enemy building and the MCV. At the time limit the winner is decided by "
    "(kills_cost - deaths_cost), then assets_value, so avoid losing trades when ahead late. "
    "Answer only in the required JSON. reasoning_summary: one short sentence."
)


class Commander:
    """Background strategist for one match. ``start()`` is idempotent."""

    def __init__(
        self,
        bot: RedAlertBot,
        provider: LLMProvider,
        *,
        log: Callable[[str], None] = print_line,
        interval: float = INTERVAL_SECONDS,
        max_calls: int = MAX_CALLS,
    ) -> None:
        self._bot = bot
        self._provider = provider
        self._log = log
        self._interval = interval
        self._max_calls = max_calls
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.calls = 0
        self.errors = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ff-commander", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        errors_in_row = 0
        # Let the opening play out before the first call.
        if self._stop.wait(self._interval):
            return
        while not self._stop.is_set() and self.calls < self._max_calls:
            pause = self._interval
            try:
                self.step()
                errors_in_row = 0
            except ProviderError as exc:
                errors_in_row += 1
                self.errors += 1
                pause = max(self._interval, backoff_seconds(errors_in_row))
                self._log(f"[ff] commander: model call failed ({exc}); keeping current knobs")
            except Exception as exc:  # never let the thread take the match down
                self.errors += 1
                self._log(f"[ff] commander: {type(exc).__name__}: {exc}")
            if self._stop.wait(pause):
                return

    def step(self) -> Knobs | None:
        """One model call; applies and returns the new knobs (None with nothing to show)."""
        snapshot = self._bot.read_snapshot()
        if not snapshot:
            return None
        self.calls += 1
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(snapshot, separators=(",", ":"), default=str)},
        ]
        answer = self._provider.complete_structured(
            messages, "red_alert_strategy", SCHEMA, timeout=REQUEST_TIMEOUT_SECONDS
        )
        knobs = knobs_from_answer(answer, self._bot.knobs)
        self._bot.set_knobs(knobs)
        self._log(
            f"[ff] commander: posture={knobs.posture} threshold={knobs.attack_threshold} "
            f"focus={knobs.unit_focus} target={knobs.target} — {knobs.note}"
        )
        return knobs


def knobs_from_answer(answer: dict, current: Knobs) -> Knobs:
    """Validated knobs from a model answer; any bad field keeps its current value."""

    def pick(name: str, allowed: tuple[str, ...]) -> str:
        value = answer.get(name)
        return value if value in allowed else getattr(current, name)

    threshold = answer.get("attack_threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        threshold = current.attack_threshold
    return Knobs(
        posture=pick("posture", ("eco", "defend", "attack")),
        attack_threshold=min(30, max(4, threshold)),
        unit_focus=pick("unit_focus", ("infantry", "vehicles", "mixed")),
        target=pick("target", ("nearest", "enemy_base", "harvesters")),
        opening=current.opening,  # (not the model's to change)
        note=str(answer.get("reasoning_summary") or "")[:200],
    )


def commander_from_env(bot: RedAlertBot, *, log: Callable[[str], None] = print_line) -> Commander | None:
    """A commander when ``OPENAI_API_KEY`` is set and ``FF_COMMANDER`` isn't ``0``; else None."""
    if not os.environ.get("OPENAI_API_KEY") or os.environ.get("FF_COMMANDER", "1") == "0":
        return None
    try:
        provider = provider_from_env()
    except RuntimeError as exc:
        log(f"[ff] commander disabled: {exc}")
        return None
    return Commander(bot, provider, log=log)
