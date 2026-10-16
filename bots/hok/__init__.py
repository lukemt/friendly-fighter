"""friendly-fighter's Honor of Kings 5v5 bot (``honor_of_kings_5v5``).

``create_agent()`` returns a fresh ``HokAgent`` for one match: a scripted team
policy (``policy.py``) behind a safety layer (``agent.py``) that only ever
answers with listed actions. ``agent/agent.py`` routes HoK here. See README.md.

Settings for local tests: ``FF_HOK_KNOBS='{"retreat_hp": 0.35}'`` (the policy's
``Knobs``), ``FF_HOK_PACE_S`` (real time: the idle look interval).
"""

from __future__ import annotations

import json
import os

from .agent import HokAgent
from .policy import Knobs, TeamPolicy, knobs_from_dict
from .view import GAME_TYPE, is_hok

__all__ = ["GAME_TYPE", "HokAgent", "Knobs", "TeamPolicy", "create_agent", "is_hok", "knobs_from_dict"]


def _env_knobs() -> dict:
    try:
        wanted = json.loads(os.environ.get("FF_HOK_KNOBS") or "{}")
    except ValueError:
        return {}
    return wanted if isinstance(wanted, dict) else {}


def create_agent(knobs: dict | None = None, **kwargs) -> HokAgent:
    """One match's agent. ``knobs`` override ``FF_HOK_KNOBS``; unknown keys are ignored."""
    wanted = {**_env_knobs(), **(knobs or {})}
    if "pace_s" not in kwargs and os.environ.get("FF_HOK_PACE_S"):
        try:
            kwargs["pace_s"] = float(os.environ["FF_HOK_PACE_S"])
        except ValueError:
            pass
    try:
        parsed = knobs_from_dict(wanted)
    except (TypeError, ValueError):
        parsed = Knobs()
    return HokAgent(parsed, **kwargs)
