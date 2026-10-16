"""The champion on the live runtime: ``python -m agent --tournament --match --agent agent.champion``.

``ff-council-champ-v10`` (lab/bots.json): the frozen rules council with early refineries, no API.
It plays inside our usual agent, so platform games are still recorded to ``recordings/``; the
remote/console/probe control files are off for it (they drive the scripted bot only).
"""

from __future__ import annotations

from dotenv import load_dotenv

from altruagent.console import print_line
from bots.frozen.ff_council_champ_v10.bot import create_agent as create_champion

from .agent import FriendlyFighter

# The settings ff-council-champ-v10 is registered with in lab/bots.json.
CHAMPION_KWARGS = {"oracle": "rules", "plan": "boom", "keep_first": 240, "early_refineries": 1}


class ChampionFighter(FriendlyFighter):
    BOT = "ff-council-champ-v10"

    def _red_alert(self):
        if self._bot is None:
            self._bot = create_champion(**CHAMPION_KWARGS)
            print_line("[ff] red_alert: champion ff-council-champ-v10 (rules council, early refineries)")
        return self._bot


def create_agent() -> ChampionFighter:
    load_dotenv()
    return ChampionFighter(controls=False)
