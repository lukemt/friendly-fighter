"""The platform's published Red Alert reference (``get_game_config('red_alert')``),
saved from a real match in ``ffsim/data/red_alert_config.json``, plus lookups."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# Map ids of the platform's pool -> the .oramap file shipped with OpenRA.
MAP_FILES = {"sidestep": "sidestep.oramap", "trapped": "trapped.oramap", "shadowfiend-ii": "shadowfiend-II.oramap"}


class Rules:
    def __init__(self, config: dict) -> None:
        self.config = config
        rules = config["rules"]
        self.units = rules["units"]
        self.buildings = rules["buildings"]
        self.maps = rules["maps"]
        self.building_items = set(self.buildings)

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> "Rules":
        return cls(json.loads((DATA / "red_alert_config.json").read_text()))

    def map(self, map_id: str) -> dict:
        if map_id not in self.maps:
            raise KeyError(f"unknown map {map_id!r}; one of {sorted(self.maps)}")
        return self.maps[map_id]

    def cost(self, item: str) -> int:
        entry = self.units.get(item) or self.buildings.get(item) or {}
        return int(entry.get("cost") or 0)


def tools_list() -> dict:
    return json.loads((DATA / "tools_list.json").read_text())


def list_games() -> dict:
    return json.loads((DATA / "list_games.json").read_text())
