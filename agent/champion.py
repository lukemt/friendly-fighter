"""The champion on the live runtime: ``python -m agent --tournament --match --agent agent.champion``.

``jev-combo-v1`` (lab/bots.d/jev.json) since 2026-10-16, the user's go: v10's frozen rules council
with the jev family's army staging and regroup, the riflemen rule, henry's refinery schedule
(``macro``) and rush-wave-k's defend radius 22. No API, no network: ``oracle="rules"``.
Before it: ``ff-council-champ-v10`` (bots/frozen/ff_council_champ_v10, ``V10_KWARGS`` below), to switch
back to by swapping the two imports and settings.

It plays inside our usual agent, so platform games are still recorded to ``recordings/``; the
remote/console/probe control files are off for it (they drive the scripted bot only).
"""

from __future__ import annotations

from dotenv import load_dotenv

from altruagent.console import print_line
from bots.frozen.jev_combo_v1.bot import create_agent as create_champion

from .agent import FriendlyFighter

# The settings jev-combo-v1 is registered with in lab/bots.d/jev.json.
CHAMPION_KWARGS = {"oracle": "rules", "plan": "boom", "keep_first": 240, "early_refineries": 1, "stage_cells": 5,
                   "regroup": True, "blind_production": "rifles", "rifle_rule": True, "macro": 8, "defend_radius": 22}
# The previous live bot's (ff-council-champ-v10, bots.frozen.ff_council_champ_v10.bot).
V10_KWARGS = {"oracle": "rules", "plan": "boom", "keep_first": 240, "early_refineries": 1}


class ChampionFighter(FriendlyFighter):
    BOT = "jev-combo-v1"

    def _red_alert(self):
        if self._bot is None:
            self._bot = create_champion(**CHAMPION_KWARGS)
            print_line("[ff] red_alert: champion jev-combo-v1 (rules council, staging + regroup, riflemen rule, "
                       "refinery schedule 8, defend radius 22)")
        return self._bot


def create_agent() -> ChampionFighter:
    load_dotenv()
    return ChampionFighter(controls=False)
