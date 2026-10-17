"""Showdown's gen 9 data (data/dex.json, made by tools/extract_data.js) and the stat model.

A ``Mon`` is one Pokémon as the bot knows it: a draft card's full set when we saw the card
(both rosters are public during the draft), else the species with a guessed spread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "dex.json"
STATS = ("hp", "atk", "def", "spa", "spd", "spe")
LEVEL = 50


def to_id(name: object) -> str:
    """Showdown's id: lowercase letters and digits only (``"Urshifu-Rapid-Strike"`` -> ``urshifurapidstrike``)."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


@lru_cache(maxsize=1)
def dex() -> dict:
    with DATA.open() as f:
        return json.load(f)


def species(name: object) -> dict | None:
    data = dex()["species"]
    sid = to_id(name)
    if sid in data:
        return data[sid]
    # "p1: Garchomp", "Garchomp, L50, F": take the species part.
    text = str(name or "")
    if ": " in text:
        text = text.split(": ", 1)[1]
    return data.get(to_id(text.split(",")[0]))


def move(name: object) -> dict | None:
    return dex()["moves"].get(to_id(name))


def type_name(name: object) -> str:
    """``"FIRE"``/``"fire"`` -> ``"Fire"``."""
    text = str(name or "").strip()
    return text[:1].upper() + text[1:].lower()


def effectiveness(move_type: str, defender_types: list[str]) -> float:
    chart = dex()["types"]
    mult = 1.0
    for t in defender_types:
        mult *= chart.get(type_name(t), {}).get(type_name(move_type), 1.0)
    return mult


def nature_mult(nature: object, stat: str) -> float:
    n = dex()["natures"].get(to_id(nature))
    if not n:
        return 1.0
    if n["plus"] == stat and n["minus"] != stat:
        return 1.1
    if n["minus"] == stat and n["plus"] != stat:
        return 0.9
    return 1.0


def calc_stat(stat: str, base: int, ev: int = 0, iv: int = 31, nature: object = None, level: int = LEVEL) -> int:
    core = (2 * base + iv + ev // 4) * level // 100
    if stat == "hp":
        return 1 if base == 1 else core + level + 10
    return int((core + 5) * nature_mult(nature, stat))


@dataclass(eq=False)  # compared and hashed by identity (team.py caches matchups per pair)
class Mon:
    """One Pokémon: its set and the stats that set gives it."""

    species_id: str
    name: str
    types: list[str]
    base: dict[str, int]
    ability: str = ""
    item: str = ""
    nature: str = ""
    moves: list[str] = field(default_factory=list)  # move ids
    evs: dict[str, int] = field(default_factory=dict)
    ivs: dict[str, int] = field(default_factory=dict)
    weight: float = 50.0
    known_set: bool = False  # from a draft card (exact), not guessed
    stats: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stats:
            self.stats = {
                s: calc_stat(s, int(self.base.get(s, 80)), int(self.evs.get(s, 0)), int(self.ivs.get(s, 31)), self.nature)
                for s in STATS
            }

    @property
    def ability_id(self) -> str:
        return to_id(self.ability)

    @property
    def item_id(self) -> str:
        return to_id(self.item)

    def has_move(self, *ids: str) -> bool:
        return any(m in self.moves for m in ids)


def mon_from_card(card: dict) -> Mon | None:
    """A draft card (``{species, item, ability, nature, evs, ivs, moves}``) as a ``Mon``."""
    sp = species(card.get("species") or card.get("name"))
    if sp is None:
        return None
    evs = {k: int(v) for k, v in (card.get("evs") or {}).items() if k in STATS}
    ivs = {k: int(v) for k, v in (card.get("ivs") or {}).items() if k in STATS}
    return Mon(
        species_id=to_id(sp["name"]), name=sp["name"], types=list(sp["types"]), base=dict(sp["bs"]),
        ability=str(card.get("ability") or (sp["abilities"][0] if sp["abilities"] else "")),
        item=str(card.get("item") or ""), nature=str(card.get("nature") or ""),
        moves=[to_id(m) for m in card.get("moves") or [] if to_id(m)],
        evs=evs, ivs=ivs, weight=float(sp.get("weight") or 50), known_set=True,
    )


def guess_mon(name: object, moves: list[str] | None = None, ability: str = "", item: str = "") -> Mon | None:
    """A species we have no card for: a common spread (252 in its better attack and speed)."""
    sp = species(name)
    if sp is None:
        return None
    bs = sp["bs"]
    physical = bs["atk"] >= bs["spa"]
    evs = {"hp": 4, ("atk" if physical else "spa"): 252, "spe": 252}
    return Mon(
        species_id=to_id(sp["name"]), name=sp["name"], types=list(sp["types"]), base=dict(bs),
        ability=ability or (sp["abilities"][0] if sp["abilities"] else ""), item=item,
        nature="Jolly" if physical else "Timid", moves=[to_id(m) for m in moves or [] if to_id(m)],
        evs=evs, weight=float(sp.get("weight") or 50), known_set=False,
    )
