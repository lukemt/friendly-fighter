"""jev-ls: the champion v10 (bots/jev/v10) with a last stand, every part of it off by default.

The idea (bots/jev/README.md, experiment jev-laststand). v10 mostly loses by being wiped out late, and
most of those games it was ahead on kills minus losses when the base started to fall (run 116 and the
board: +9,400, +13,000, +17,000 ... on the net score). What the engine rules allow:

- a sold building is no loss: ``deaths_cost`` only counts what is destroyed, and a sale pays half the
  building's cost (by its health) and spawns infantry worth 40% more (run 148, ``selltest``);
- a player is defeated only when no building and no MCV is left (units don't count: run 148 again);
- the AI knows where everything stands (it picks the nearest enemy actor from ``World.Actors``, fog or
  not), so a hiding place only buys the time it needs to walk there.

So, in three opt-in parts:

- **insurance** (``insure_from``): from this game time an MCV is bought once, when the economy has
  money and the score is not lost, and sent to a hiding place chosen from the map's picture: far, by
  path, from our base and from the enemy's. It runs away from enemies that come near.
- **the last stand** (``sell_mode="overrun"``): when the base is overrun (enemy value at home much
  above ours, buildings falling) and the score is still ahead, with the MCV alive, every building is
  sold at once, nearest to the enemy first: no loss is booked, and the money and the infantry stay.
- **the dying** (``sell_dying``): a building that is under fire and below this health is sold before it dies.

Nothing here changes v10 unless a setting is on.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from altruagent import WAIT, WithReasoning

from .v10.bot import CouncilBot
from .v10.jev_bot import NONCOMBAT

MAX_ORDERS = 20
PASSABLE = set(".,=$*MS")
SOVIET = {"russia", "ukraine", "soviet"}
UNSEEN = 10 ** 6


def _cell(obj: Any) -> tuple[int, int] | None:
    if isinstance(obj, dict) and isinstance(obj.get("x"), (int, float)) and isinstance(obj.get("y"), (int, float)):
        return int(obj["x"]), int(obj["y"])
    if isinstance(obj, (list, tuple)) and len(obj) == 2 and all(isinstance(v, (int, float)) for v in obj):
        return int(obj[0]), int(obj[1])
    return None


def _dist(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Terrain:
    """The map's picture (``rules.maps.<id>.layout``) as a grid we can walk over."""

    def __init__(self, layout: dict) -> None:
        rows = layout.get("rows") or []
        ox, oy = (layout.get("origin") or [0, 0])[:2]
        self.ox, self.oy = int(ox), int(oy)
        self.h = len(rows)
        self.w = max((len(r) for r in rows), default=0)
        self.ok = [[(c in PASSABLE) for c in row.ljust(self.w, "^")] for row in rows]

    def passable(self, x: int, y: int) -> bool:
        i, j = y - self.oy, x - self.ox
        return 0 <= i < self.h and 0 <= j < self.w and self.ok[i][j]

    def roomy(self, x: int, y: int) -> bool:
        return all(self.passable(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    def distances(self, start: tuple[int, int]) -> dict[tuple[int, int], int]:
        """Steps from ``start`` to every cell it can walk to (8 neighbours, no corner cutting)."""
        best = None
        for r in range(0, 8):  # (the start may stand on a building: the nearest walkable cell)
            cells = [(start[0] + dx, start[1] + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                     if max(abs(dx), abs(dy)) == r and self.passable(start[0] + dx, start[1] + dy)]
            if cells:
                best = cells[0]
                break
        if best is None:
            return {}
        dist = {best: 0}
        queue = deque([best])
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                n = (x + dx, y + dy)
                if n in dist or not self.passable(*n):
                    continue
                if dx and dy and not (self.passable(x + dx, y) and self.passable(x, y + dy)):
                    continue
                dist[n] = dist[(x, y)] + 1
                queue.append(n)
        return dist

    def hiding_places(self, home: tuple[int, int], enemy: tuple[int, int], count: int = 3) -> list[tuple[int, int]]:
        """Roomy cells far, by path, from both bases, apart from each other (best first)."""
        dh, de = self.distances(home), self.distances(enemy)
        if not dh:
            return []
        scored = []
        for cell, d in dh.items():
            if d < 20 or not self.roomy(*cell):
                continue
            far = min(d, de.get(cell, UNSEEN))
            scored.append((far + 0.1 * d, cell))
        scored.sort(reverse=True)
        out: list[tuple[int, int]] = []
        for _, cell in scored:
            if all(_dist(cell, o) >= 25 for o in out):
                out.append(cell)
            if len(out) >= count:
                break
        return out


class LastStandBot(CouncilBot):
    def __init__(self, *, insure_from: float | None = None, insure_net_min: int = -500, insure_cash: int = 0,
                 sell_mode: str = "off", sell_net_min: int = -1500, overrun_value: int = 3000,
                 overrun_ratio: float = 1.3, overrun_seconds: float = 3.0, sell_dying: int = 0,
                 cashout_at: float | None = None, cashout_net: int = 2000, stand_army: str = "stay",
                 cashout_vs: str = "any", insure_count: int = 1, insure_gap: float = 120.0, **kwargs) -> None:
        """``insure_from``: game seconds from which one spare MCV is bought (None: never);
        ``insure_net_min``/``insure_cash``: only with the score at least this and this much money.
        ``sell_mode``: "overrun" sells the whole base when it is overrun (enemy value within 18 cells of
        home at least ``overrun_value`` and ``overrun_ratio`` times ours, and buildings falling or
        damaged under fire, for ``overrun_seconds``), while the score is at least ``sell_net_min`` and a
        spare MCV lives. ``sell_dying``: a building under fire below this health percent is sold.
        ``cashout_at``: from this game time, with a spare MCV alive and the score at least ``cashout_net``,
        the whole base is sold at once (the lead is banked: no building left to lose).
        ``stand_army``: after the sale the army and the harvesters "stay" where they are, go to the
        MCV's hiding place ("mcv"), or to places of their own, apart from the MCV and each other ("split").
        ``insure_count``: how many spare MCVs (``insure_gap`` seconds apart), each to a place of its own.
        ``cashout_vs``: "soviet" cashes out only against a Soviet opponent (the AIs' nukes and bombers)."""
        super().__init__(**kwargs)
        self.insure_from, self.insure_net_min, self.insure_cash = insure_from, insure_net_min, insure_cash
        self.sell_mode, self.sell_net_min = sell_mode, sell_net_min
        self.overrun_value, self.overrun_ratio, self.overrun_seconds = overrun_value, overrun_ratio, overrun_seconds
        self.sell_dying = sell_dying
        self.insure_count, self.insure_gap = insure_count, insure_gap
        self.cashout_at, self.cashout_net, self.stand_army = cashout_at, cashout_net, stand_army
        self.cashout_vs = cashout_vs
        self.army_moved_at = -1e9
        self.terrain: Terrain | None = None
        self.places: list[tuple[int, int]] = []
        self.spawn_home: tuple[int, int] | None = None
        self.mcv_bought = 0
        self.mcv_order_at = -1e9
        self.last_buildings: dict[int, tuple[str, int]] = {}
        self.sold_ids: set[int] = set()
        self.destroyed_at: list[float] = []
        self.overrun_since: float | None = None
        self.stand = False
        self.stand_at: float | None = None
        self.mcv_target: dict[int, tuple[int, int]] = {}
        self.mcv_moved_at: dict[int, float] = {}
        self.group_moved_at = -1e9
        self.tags: list[str] = []

    # -- the runner's hook ------------------------------------------------------------------------

    def choose_action(self, state, context):
        move = super().choose_action(state, context)
        raw = state.raw
        obs = raw.get("observation")
        if not isinstance(obs, dict):
            return move
        now = float((raw.get("time") or {}).get("game_seconds") or (obs.get("tick") or 0) / 25.0)
        inner = list(move.action.get("orders") or []) if isinstance(move, WithReasoning) else []
        mine, inner, tags = self._last_stand(raw, obs, now, context, inner)
        if not mine and inner == (list(move.action.get("orders") or []) if isinstance(move, WithReasoning) else []):
            return move
        orders = inner[:MAX_ORDERS - len(mine[:MAX_ORDERS])] + mine[:MAX_ORDERS]
        if not orders:
            return WAIT
        why = (getattr(move, "reasoning_summary", "") or "")
        return WithReasoning({"type": "orders", "orders": orders}, (" ".join(tags) + " " + why)[:280])

    # -- the three parts --------------------------------------------------------------------------

    def _last_stand(self, raw: dict, obs: dict, now: float, context, inner: list[dict]):
        mine: list[dict[str, Any]] = []
        tags: list[str] = []
        units = [u for u in obs.get("units") or [] if not u.get("wreck")]
        buildings = obs.get("buildings") or []
        costs = self.bot._costs
        military = obs.get("military") or {}
        net = int(military.get("kills_cost") or 0) - int(military.get("deaths_cost") or 0)
        economy = obs.get("economy") or {}
        cash = int(economy.get("cash") or 0) + int(economy.get("ore") or 0)
        types = {str(b.get("type") or "").lower() for b in buildings}
        mcvs = [u for u in units if str(u.get("type") or "").lower() == "mcv"] if now > 30 else []
        home = _cell(obs.get("base_center"))
        enemy_base = _cell((raw.get("legal_actions") or {}).get("enemy_base"))
        if self.spawn_home is None and home:
            self.spawn_home = home  # (a branch starts mid-game: our base then is as good as the spawn)
        self._track_buildings(buildings, now)
        if self.terrain is None and self.spawn_home and enemy_base:
            self._learn_terrain(context, raw)
        enemies = [e for e in obs.get("visible_enemies") or []
                   if not e.get("wreck") and str(e.get("type") or "").lower() not in NONCOMBAT | {"thf", "mech", "truk"}]

        # 1. insurance: one spare MCV, bought once there is money and the score isn't lost
        if self.insure_from is not None and now >= self.insure_from and self.mcv_bought < self.insure_count + 1:
            in_production = any(str(p.get("item") or "").lower() == "mcv" for p in obs.get("production") or [])
            if (len(mcvs) < self.insure_count and not in_production and "fix" in types and "weap" in types
                    and net >= self.insure_net_min and cash >= self.insure_cash
                    and now - self.mcv_order_at > max(20.0, self.insure_gap if mcvs else 20.0)):
                mine.append({"cmd": "train", "item": "mcv", "count": 1})
                self.mcv_order_at = now
                self.mcv_bought += 1
                tags.append("[ls insure]")
        if mcvs:
            inner = [o for o in inner if o.get("cmd") != "deploy"]  # (the inner bot would plant it as a yard)
            mine += self._hide(mcvs, enemies, now)

        # 2. the last stand: sell what is going to be lost anyway
        if self.sell_mode == "overrun" and not self.stand and mcvs and buildings and home:
            if self._overrun(units, enemies, buildings, home, now) and net >= self.sell_net_min:
                self.overrun_since = self.overrun_since if self.overrun_since is not None else now
                if now - self.overrun_since >= self.overrun_seconds:
                    self.stand, self.stand_at = True, now
                    tags.append("[ls STAND]")
            else:
                self.overrun_since = None
        enemy_faction = str(obs.get("enemy_faction") or "").lower()
        if (self.cashout_at is not None and not self.stand and mcvs and buildings and now >= self.cashout_at
                and net >= self.cashout_net and (self.cashout_vs != "soviet" or enemy_faction in SOVIET)):
            self.stand, self.stand_at = True, now
            tags.append("[ls CASHOUT]")
        if self.stand and self.stand_army != "stay" and self.places and now - self.group_moved_at >= 12:
            self.group_moved_at = now
            n = len(self.places)
            mcv_spot = self.places[0]
            if self.stand_army == "mcv":
                army_spot = harv_spot = mcv_spot
            else:  # (apart from the MCV and from each other)
                army_spot, harv_spot = self.places[1 % n], self.places[2 % n]
            for kind, spot in (("army", army_spot), ("harv", harv_spot)):
                ids = [u["id"] for u in units if (c := _cell(u)) and _dist(c, spot) > 10 and (
                    str(u.get("type") or "").lower() == "harv" if kind == "harv" else
                    str(u.get("type") or "").lower() not in {"mcv", "harv", "tecn", "tecn2", "c10", "e6"})]
                for i in range(0, min(len(ids), 100), 50):
                    mine.append({"cmd": "move", "units": ids[i:i + 50], "to": list(spot)})
            inner = [o for o in inner if o.get("cmd") != "harvest"]
        if self.stand and buildings:
            inner = [o for o in inner if o.get("cmd") not in ("build", "place", "train", "deploy")]
            near = enemies or [{"x": enemy_base[0], "y": enemy_base[1]}] if enemy_base else enemies

            def nearest(b: dict) -> float:
                c = _cell(b)
                return min((_dist(c, cell) for e in near if (cell := _cell(e))), default=0.0) if c else 0.0

            for b in sorted(buildings, key=nearest)[:MAX_ORDERS]:
                if b.get("id") not in self.sold_ids and mcvs:
                    mine.append({"cmd": "sell", "building": b["id"]})
                    self.sold_ids.add(b["id"])
                elif b.get("id") in self.sold_ids and now - (self.stand_at or now) > 6:
                    mine.append({"cmd": "sell", "building": b["id"]})  # (again: an order may have been lost)
        elif self.sell_dying and mcvs and buildings:
            for b in buildings:
                c, hp = _cell(b), int(b.get("hp") or 100)
                if (c and hp <= self.sell_dying and b.get("id") not in self.sold_ids
                        and any((e_c := _cell(e)) and _dist(c, e_c) <= 8 for e in enemies)):
                    mine.append({"cmd": "sell", "building": b["id"]})
                    self.sold_ids.add(b["id"])
                    tags.append("[ls dying]")
        return mine, inner, tags

    def _track_buildings(self, buildings: list[dict], now: float) -> None:
        current = {b["id"]: (str(b.get("type") or ""), int(b.get("hp") or 0)) for b in buildings if "id" in b}
        for bid in set(self.last_buildings) - set(current):
            if bid not in self.sold_ids:
                self.destroyed_at.append(now)
        self.destroyed_at = [t for t in self.destroyed_at if now - t <= 25]
        self.last_buildings = current

    def _learn_terrain(self, context, raw: dict) -> None:
        try:
            rules = (context.game_config or {}).get("rules") or {}
            map_id = (raw.get("map") or {}).get("id") or (raw.get("observation") or {}).get("map", {}).get("id")
            layout = ((rules.get("maps") or {}).get(map_id) or {}).get("layout")
            if not layout:
                return
            enemy = _cell((raw.get("legal_actions") or {}).get("enemy_base"))
            self.terrain = Terrain(layout)
            self.places = self.terrain.hiding_places(self.spawn_home, enemy) if enemy else []
        except Exception:  # noqa: BLE001  (no picture: the MCV stays where it is)
            self.terrain, self.places = Terrain({"rows": []}), []

    def _hide(self, mcvs: list[dict], enemies: list[dict], now: float) -> list[dict[str, Any]]:
        """Send each spare MCV to a hiding place of its own and leave it there (no running about: it was
        seen and caught crossing the map). A new order only for an MCV that is idle and not there yet."""
        out: list[dict[str, Any]] = []
        if not self.places:
            return out
        for u in sorted(mcvs, key=lambda m: m["id"]):
            here = _cell(u)
            if here is None:
                continue
            if u["id"] not in self.mcv_target:
                taken = set(self.mcv_target.values())
                self.mcv_target[u["id"]] = next((p for p in self.places if p not in taken), self.places[0])
                self.mcv_moved_at[u["id"]] = -1e9
            target = self.mcv_target[u["id"]]
            if _dist(here, target) > 4 and (now - self.mcv_moved_at[u["id"]] >= 20 or (u.get("idle") and now - self.mcv_moved_at[u["id"]] >= 6)):
                self.mcv_moved_at[u["id"]] = now
                out.append({"cmd": "move", "units": [u["id"]], "to": list(target)})
        return out

    def _overrun(self, units: list[dict], enemies: list[dict], buildings: list[dict], home: tuple[int, int], now: float) -> bool:
        costs = self.bot._costs
        e_value = sum(int(costs.get(str(e.get("type") or "").lower(), 300)) for e in enemies
                      if (c := _cell(e)) and _dist(c, home) <= 18)
        a_value = sum(int(costs.get(str(u.get("type") or "").lower(), 300)) for u in units
                      if (c := _cell(u)) and _dist(c, home) <= 25
                      and str(u.get("type") or "").lower() not in NONCOMBAT | {"mcv", "harv"})
        if e_value < self.overrun_value or e_value < self.overrun_ratio * max(a_value, 500):
            return False
        falling = len(self.destroyed_at) >= 2
        hurt = any(int(b.get("hp") or 100) < 45 and (c := _cell(b)) and any((ec := _cell(e)) and _dist(c, ec) <= 10 for e in enemies)
                   for b in buildings)
        return falling or hurt


def create_agent(**kwargs) -> LastStandBot:
    return LastStandBot(**kwargs)
