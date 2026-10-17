"""A scripted advisor for ext-ryan: the plan Claude played at the desk against v10 (runs 75-77), in code.

Claude advising ext-ryan beat ff-council-champ-v10 2 of 3 games (docs/STRATEGY.md, "How v10 was
beaten"); unadvised, ext-ryan lost 18 of 18. ``PlanDesk`` answers ext-ryan's own advisor questions
(the same JSON schemas Claude answered, via ``bots.external``: ``advisor: "bots.ryan_plan:PlanDesk"``)
with that plan, so it plays as many games as the lab likes:

- **Economy first:** two refineries before the war factory, harvesters to 6-9, up to 5 refineries,
  a defense with idle cash, then tanks with masses of riflemen, all kept at home.
- **One massed strike** at 5:40-6:40, once the army is big: toward the enemy's ore field (or its
  base edge), pressing while the local fight is won, back home when it turns or when well ahead.
- **Hold a lead at home; all-in when behind late.**
"""

from __future__ import annotations

import math
import re

TANKS = ("3tnk", "2tnk", "1tnk")
DEFENSES = ("pbox", "ftur", "gun", "hbox")
DEFENSE_TYPES = {"pbox", "ftur", "gun", "hbox", "tsla", "sam", "agun"}
TOWER_REACH = 9  # cells: beyond a tesla coil's 7 and a turret's 6.5
AHEAD = re.compile(r"\((\d+) vs (\d+) ahead\)")


def _cell(v):
    return (int(v[0]), int(v[1])) if isinstance(v, (list, tuple)) and len(v) >= 2 else None


def _toward(a, b, frac: float):
    return (round(a[0] + (b[0] - a[0]) * frac), round(a[1] + (b[1] - a[1]) * frac))


class PlanDesk:
    """Answers one seat's advisor questions (``Desk.ask``'s signature) with the plan above."""

    def __init__(self) -> None:
        self.strike: str = "none"   # none -> going -> done
        self.strike_from = 0.0
        self.defense_built = 0
        self.second_weap = False

    def attach(self, seat) -> None:
        pass

    def ask(self, *, system: str, payload, schema: dict, seat=None, model: str = "") -> dict:
        props = (schema.get("properties") or {})
        if "posture" in props:
            return self._strategy(payload)
        if "main_action" in props:
            return self._tactical(payload)
        return {k: ({"integer": -1, "array": []}.get(v.get("type"), "")) for k, v in props.items()}

    # -- the general -------------------------------------------------------------------------------

    def _strategy(self, p: dict) -> dict:
        m = p.get("match") or {}
        t = float((m.get("time") or {}).get("game_seconds") or 0)
        tank = self._tank((m.get("bot_status") or {}).get("catalog") or {})
        if t < 60:
            targets = [("proc", 2), ("harv", 4)]
        elif t < 120:
            targets = [("proc", 2), ("weap", 1), ("harv", 5)]
        elif t < 180:
            targets = [("weap", 1), ("harv", 6), ("proc", 3)]
        elif t < 240:
            targets = [("proc", 3), ("harv", 6), ("fix", 1)]
        elif t < 300:
            targets = [("proc", 4)] + ([(tank, 4)] if tank else [])
        else:
            targets = [(tank, 8)] if tank else []
        return {"posture": "economy" if t < 300 else "balanced", "unit_focus": "mixed", "defenses": 2,
                "max_refineries": 5, "build_targets": [{"item": i, "count": c} for i, c in targets], "avoid": [],
                "standing_raid": 0,
                "plan": "Economy first, army home; one massed strike on their ore-field guard when big; hold a lead "
                        "at home, all-in only when behind late."}

    # -- the field commander -----------------------------------------------------------------------

    def _tactical(self, p: dict) -> dict:
        m = p.get("match") or {}
        bot = p.get("bot") or {}
        t = float((m.get("time") or {}).get("game_seconds") or 0)
        left = float((m.get("time") or {}).get("remaining_seconds") or (600 - t))
        eco, mil = m.get("economy") or {}, m.get("military") or {}
        net = int(mil.get("kills_cost") or 0) - int(mil.get("deaths_cost") or 0)
        units, buildings = m.get("my_units") or {}, m.get("my_buildings") or {}
        catalog = bot.get("catalog") or {}
        tank = self._tank(catalog)
        army = (p.get("army") or {}).get("count") or 0
        harv = int(eco.get("harvesters") or 0)
        a = {"build": "auto", "defense": "", "infantry": "", "infantry_count": 1, "vehicle": "auto", "vehicle_count": 1,
             "main_action": "hold", "main_x": -1, "main_y": -1, "raid_size": 0, "raid_unit": "", "raid_x": -1,
             "raid_y": -1, "note": ""}
        barracks = buildings.get("tent", 0) + buildings.get("barr", 0)
        # Production.
        if t < 100:
            pass
        elif barracks and not buildings.get("weap"):
            a["build"] = "weap"
        if buildings.get("weap"):
            a["vehicle"] = "harv" if harv < 6 or (harv < 9 and buildings.get("proc", 0) >= 5 and t < 360) else (tank or "auto")
        if t >= 120 and self.defense_built < 1 and buildings.get("proc", 0) >= 2:
            a["defense"] = next((d for d in DEFENSES if d in catalog), "")
            self.defense_built += 1 if a["defense"] else 0
        if barracks and (t < 150 or harv >= 6):
            a["infantry"], a["infantry_count"] = "e1", 2
        if (not self.second_weap and buildings.get("weap") == 1 and int(eco.get("ore") or 0) > 1000 and t > 280):
            a["build"], self.second_weap = "weap", True
        # The army.
        base, enemy = _cell(m.get("base_center")), _cell(m.get("enemy_start"))
        local = AHEAD.search(str(bot.get("main_state") or ""))
        ours, theirs = (int(local.group(1)), int(local.group(2))) if local else (0, 0)
        if left < 80 and net < 0:  # behind at the end: everything at whatever is nearest
            target = self._nearest_enemy(p, base) or self._edge(base, enemy)
            a.update(main_action="attack", main_x=target[0], main_y=target[1], note="behind at the end: all in")
        elif self.strike == "none" and ((t >= 340 and army >= 35) or (t >= 400 and army >= 25)) and left > 120:
            self.strike, self.strike_from = "going", t
        if a["main_action"] == "hold" and self.strike == "going":
            turned = theirs and ours < 0.8 * theirs
            if turned or net >= 2500 or t - self.strike_from > 120 or left < 150:
                self.strike = "done"
                a.update(main_action="retreat", note="strike over: home" + (" with the lead" if net > 0 else ""))
            else:
                target = self._target(p, bot, base, enemy)
                a.update(main_action="attack", main_x=target[0], main_y=target[1], note="massed strike")
        elif a["main_action"] == "hold" and self.strike == "none" and t >= 300 and base and enemy:
            spot = _toward(base, enemy, 0.3)  # (forming up forward cuts the walk)
            a.update(main_x=spot[0], main_y=spot[1], note="forming up")
        return a

    @staticmethod
    def _defenses(p: dict) -> list[tuple[int, int]]:
        known = p.get("known_enemy_buildings") or []
        return [(b["x"], b["y"]) for b in known if isinstance(b, dict) and b.get("type") in DEFENSE_TYPES
                and isinstance(b.get("x"), int)]

    def _target(self, p: dict, bot: dict, base, enemy):
        """Where the strike goes: their units in the open (harvesters and the guard), else their ore
        field, else short of their base; never within reach of a known defense."""
        towers = self._defenses(p)
        def safe(c):
            return c and all(math.dist(c, d) > TOWER_REACH for d in towers)
        seen = [(e.get("x"), e.get("y")) for e in p.get("visible_enemies") or [] if isinstance(e.get("x"), int)]
        seen = [c for c in seen if safe(c)]
        if seen and base:
            return min(seen, key=lambda c: math.dist(c, base))
        field = _cell(bot.get("enemy_ore_field"))
        if safe(field):
            return field
        if not base or not enemy:
            return (-1, -1)
        d = math.dist(base, enemy) or 1.0
        for short in (16, 20, 24, 28):
            c = _toward(base, enemy, max(0.0, 1.0 - short / d))
            if safe(c):
                return c
        return _toward(base, enemy, 0.5)

    @staticmethod
    def _tank(catalog: dict) -> str:
        return next((t for t in TANKS if t in catalog), "")

    @staticmethod
    def _edge(base, enemy):
        if not base or not enemy:
            return (-1, -1)
        d = math.dist(base, enemy) or 1.0
        return _toward(base, enemy, max(0.0, 1.0 - 12.0 / d))  # 12 cells short of their base

    @staticmethod
    def _nearest_enemy(p: dict, base):
        cells = [(e.get("x"), e.get("y")) for e in p.get("visible_enemies") or [] if isinstance(e.get("x"), int)]
        if not cells or not base:
            return None
        return min(cells, key=lambda c: math.dist(c, base))
