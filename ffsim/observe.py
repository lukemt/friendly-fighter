"""Engine view -> the platform's ``observation``, ``events`` and ``legal_actions``.

An engine view is a plain dict (``ffsim.engine_openra.view_from_pb`` makes one
from the bridge's protobuf; ``ffsim.engine_fake`` writes them directly)::

    {tick, player, faction, enemy_faction, done, result, winner, explored_percent,
     economy: {cash, ore, power_provided, power_drained, resource_capacity, harvesters},
     military: {kills_cost, deaths_cost, assets_value, army_value, units_killed, units_lost,
                buildings_killed, buildings_lost},
     units / visible_enemies: [{id, type, x, y, hp_percent, idle, activity, stance, is_husk}],
     buildings / visible_enemy_buildings: [{id, type, x, y, hp_percent, producing, powered_down,
                                           disabled, repairing, rally}],
     production: [{queue, item, progress, remaining_ticks, paused}],
     available_production: [...], command_results: [...]}

Field order and rounding follow recorded platform states.
"""

from __future__ import annotations

import math

STANCE_NAMES = ["hold_fire", "return_fire", "defend", "attack_anything"]
NON_COMBAT = {"mcv", "harv", "medi", "e6", "mech", "truk", "spy", "thf"}
SHIPS = {"ss", "msub", "dd", "ca", "pt", "lst"}
PRODUCERS = {"fact", "tent", "barr", "kenn", "weap", "afld", "afld.ukraine", "hpad", "spen", "syrd"}
BUILD_QUEUES = {"Building", "Defense"}
EVENT_WINDOW = 20
UNDER_ATTACK_TICKS = 250  # at most once per actor per 10 s
SOLD_WINDOW_TICKS = 750  # a building gone within 30 s of a sell order


def _hp(value) -> int:
    return int(round(float(value or 0) * 100))


def is_wreck(actor: dict) -> bool:
    return bool(actor.get("is_husk")) or "husk" in str(actor.get("type", ""))


def own_unit(u: dict) -> dict:
    out = {"id": u["id"], "type": u["type"], "x": u["x"], "y": u["y"], "hp": _hp(u.get("hp_percent")),
           "act": u.get("activity") or None, "idle": bool(u.get("idle")),
           "stance": STANCE_NAMES[int(u.get("stance") or 0) % 4]}
    if is_wreck(u):
        out["wreck"] = True
    return out


def enemy_unit(u: dict) -> dict:
    out = {"id": u["id"], "type": u["type"], "x": u["x"], "y": u["y"], "hp": _hp(u.get("hp_percent"))}
    if is_wreck(u):
        out["wreck"] = True
    return out


def own_building(b: dict) -> dict:
    rally = b.get("rally")
    return {"id": b["id"], "type": b["type"], "x": b["x"], "y": b["y"], "hp": _hp(b.get("hp_percent")),
            "powered": not b.get("disabled"), "powered_down": bool(b.get("powered_down")),
            "repairing": bool(b.get("repairing")), "producing": b.get("producing") or None,
            "rally": list(rally) if rally and rally[0] >= 0 else None}


def enemy_building(b: dict) -> dict:
    return {"id": b["id"], "type": b["type"], "x": b["x"], "y": b["y"], "hp": _hp(b.get("hp_percent"))}


def production_row(p: dict) -> dict:
    progress = float(p.get("progress") or 0)
    remaining = int(p.get("remaining_ticks") or 0)
    return {"queue": p["queue"], "item": p["item"], "progress": round(progress, 2),
            "remaining_s": round(remaining / 25, 1), "paused": bool(p.get("paused")),
            "ready": remaining <= 0 and progress >= 0.999}


def base_center(buildings: list[dict], units: list[dict]) -> list[int] | None:
    for b in buildings:
        if b["type"] == "fact":
            return [b["x"], b["y"]]
    group = buildings or [u for u in units if not u.get("wreck")]
    if not group:
        return None
    return [int(round(sum(a["x"] for a in group) / len(group))), int(round(sum(a["y"] for a in group) / len(group)))]


def observation(view: dict, map_id: str, size: tuple[int, int], events: list[dict]) -> dict:
    units = [own_unit(u) for u in view.get("units") or []]
    buildings = [own_building(b) for b in view.get("buildings") or []]
    available = []
    for item in view.get("available_production") or []:
        if item.lower() not in available:
            available.append(item.lower())
    eco = view.get("economy") or {}
    mil = view.get("military") or {}
    return {
        "tick": view["tick"],
        "faction": view.get("faction"),
        "enemy_faction": view.get("enemy_faction"),
        "economy": {k: int(eco.get(k) or 0) for k in
                    ("cash", "ore", "power_provided", "power_drained", "resource_capacity", "harvesters")},
        "military": {k: int(mil.get(k) or 0) for k in
                     ("kills_cost", "deaths_cost", "assets_value", "army_value", "units_killed", "units_lost",
                      "buildings_killed", "buildings_lost")},
        "units": units,
        "buildings": buildings,
        "production": [production_row(p) for p in view.get("production") or []],
        "available_production": available,
        "visible_enemies": [enemy_unit(u) for u in view.get("visible_enemies") or []],
        "visible_enemy_buildings": [enemy_building(b) for b in view.get("visible_enemy_buildings") or []],
        "map": {"id": map_id, "width": size[0], "height": size[1]},
        "base_center": base_center(buildings, units),
        "explored_percent": round(float(view.get("explored_percent") or 0), 1),
        "events": list(events[-EVENT_WINDOW:]),
    }


class EventTracker:
    """The seat's event log, built by comparing consecutive views."""

    def __init__(self) -> None:
        self.seq = 0
        self.events: list[dict] = []
        self.prev: dict | None = None
        self.spotted: set[int] = set()
        self.last_attacked: dict[int, int] = {}
        self.sold: dict[int, int] = {}  # building id -> tick of the sell order
        self.low_power = False
        self.ready_counts: dict[str, int] = {}
        self.sink = None  # called with every new event (the lab's game log); the seat sees the last 20

    def note_sell(self, building_id: int, tick: int) -> None:
        self.sold[building_id] = tick

    def _push(self, tick: int, kind: str, **fields) -> None:
        self.seq += 1
        event = {"seq": self.seq, "tick": tick, "kind": kind, **fields}
        self.events.append(event)
        del self.events[:-EVENT_WINDOW]
        if self.sink is not None:
            self.sink(event)

    def update(self, view: dict) -> None:
        tick = view["tick"]
        prev = self.prev
        self.prev = view
        units = {u["id"]: u for u in view.get("units") or []}
        buildings = {b["id"]: b for b in view.get("buildings") or []}
        enemies = list(view.get("visible_enemies") or []) + list(view.get("visible_enemy_buildings") or [])
        ready = {}
        for p in view.get("production") or []:
            if p.get("queue") in BUILD_QUEUES and production_row(p)["ready"]:
                ready[p["item"]] = ready.get(p["item"], 0) + 1
        if prev is None:
            # The first view seeds the trackers: the starting MCV and anything seen are not events.
            self.spotted.update(e["id"] for e in enemies if not is_wreck(e))
            self.ready_counts = ready
            eco = view.get("economy") or {}
            self.low_power = (eco.get("power_drained") or 0) > (eco.get("power_provided") or 0)
            return
        old_units = {u["id"]: u for u in prev.get("units") or []}
        old_buildings = {b["id"]: b for b in prev.get("buildings") or []}

        new_buildings = [b for bid, b in buildings.items() if bid not in old_buildings]
        for b in new_buildings:
            self._push(tick, "building_created", id=b["id"], type=b["type"], x=b["x"], y=b["y"])
        for uid, u in units.items():
            if uid not in old_units and not is_wreck(u):
                self._push(tick, "unit_created", id=uid, type=u["type"], x=u["x"], y=u["y"])
        new_wrecks = [u for uid, u in units.items() if uid not in old_units and is_wreck(u)]
        deployed_fact = any(b["type"] == "fact" for b in new_buildings)
        for uid, u in old_units.items():
            if uid in units or is_wreck(u):
                continue
            if u["type"] == "mcv" and deployed_fact:
                continue  # deploying: only building_created
            fields = {"id": uid, "type": u["type"], "x": u["x"], "y": u["y"]}
            wreck = next((w for w in new_wrecks if w["type"].split(".")[0] == u["type"]
                          and abs(w["x"] - u["x"]) <= 1 and abs(w["y"] - u["y"]) <= 1), None)
            if wreck is not None:
                fields["wreck_id"] = wreck["id"]
                new_wrecks.remove(wreck)
            self._push(tick, "unit_lost", **fields)
        for bid, b in old_buildings.items():
            if bid in buildings:
                continue
            sold_at = self.sold.pop(bid, None)
            kind = "building_sold" if sold_at is not None and tick - sold_at <= SOLD_WINDOW_TICKS else "building_lost"
            self._push(tick, kind, id=bid, type=b["type"], x=b["x"], y=b["y"])

        for item, count in ready.items():
            for _ in range(count - self.ready_counts.get(item, 0)):
                self._push(tick, "production_ready", type=item)
        self.ready_counts = ready

        for e in enemies:
            if e["id"] not in self.spotted and not is_wreck(e):
                self.spotted.add(e["id"])
                self._push(tick, "enemy_spotted", id=e["id"], type=e["type"], x=e["x"], y=e["y"])

        old_mil, mil = prev.get("military") or {}, view.get("military") or {}
        killed = ((mil.get("units_killed") or 0) + (mil.get("buildings_killed") or 0)
                  - (old_mil.get("units_killed") or 0) - (old_mil.get("buildings_killed") or 0))
        if killed > 0:
            self._push(tick, "enemy_killed", count=killed)

        for actor_id, actor in list(units.items()) + list(buildings.items()):
            old = old_units.get(actor_id) or old_buildings.get(actor_id)
            if old is None or is_wreck(actor):
                continue
            if (actor.get("hp_percent") or 0) < (old.get("hp_percent") or 0) - 1e-6:
                last = self.last_attacked.get(actor_id)
                if last is None or tick - last >= UNDER_ATTACK_TICKS:
                    self.last_attacked[actor_id] = tick
                    self._push(tick, "under_attack", id=actor_id, type=actor["type"], x=actor["x"], y=actor["y"])

        eco = view.get("economy") or {}
        low = (eco.get("power_drained") or 0) > (eco.get("power_provided") or 0)
        if low != self.low_power:
            self._push(tick, "low_power" if low else "power_restored")
            self.low_power = low


def enemy_base(base: list[int] | None, spawns: list[list[int]]) -> list[int] | None:
    if not spawns:
        return None
    if base is None:
        return list(spawns[-1])
    return list(max(spawns, key=lambda s: (s[0] - base[0]) ** 2 + (s[1] - base[1]) ** 2))


HOW_TO_SEND = ("Send 1-20 orders: play_action(session_id, state_version, action={\"type\":\"orders\",\"orders\":[...]}); "
               "see get_game_config('red_alert').order_schema.")


def legal_actions(session_id: str, state_version: int, obs: dict, spawns: list[list[int]],
                  building_items: set[str]) -> dict:
    units = [u for u in obs.get("units") or [] if not u.get("wreck")]
    buildings = obs.get("buildings") or []
    avail = obs.get("available_production") or []
    place = []
    for p in obs.get("production") or []:
        if p["queue"] in BUILD_QUEUES and p["ready"] and p["item"] not in place:
            place.append(p["item"])
    enemies = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck")]
    enemy_buildings = obs.get("visible_enemy_buildings") or []
    combat_all = [u for u in units if u["type"] not in NON_COMBAT and u["type"] not in SHIPS]
    combat_idle = [u["id"] for u in combat_all if u["idle"]]
    eb = enemy_base(obs.get("base_center"), spawns)
    attack_now = None
    if combat_all:
        group = [u for u in combat_all if u["idle"]] or combat_all
        group = group[:50]
        targets = enemies + enemy_buildings
        to = eb
        if targets:
            cx = sum(u["x"] for u in group) / len(group)
            cy = sum(u["y"] for u in group) / len(group)
            nearest = min(targets, key=lambda e: math.hypot(e["x"] - cx, e["y"] - cy))
            to = [nearest["x"], nearest["y"]]
        attack_now = {"cmd": "attack_move", "units": [u["id"] for u in group], "to": to}
    return {
        "session_id": session_id,
        "state_version": state_version,
        "build": [i for i in avail if i in building_items],
        "train": [i for i in avail if i not in building_items],
        "place": place,
        "deploy": [u["id"] for u in units if u["type"] == "mcv"],
        "repair": [b["id"] for b in sorted(buildings, key=lambda b: b["hp"]) if b["hp"] < 100 and not b["repairing"]],
        "attack": [e["id"] for e in enemies] + [b["id"] for b in enemy_buildings],
        "attack_now": attack_now,
        "enemy_base": eb,
        "combat_units": combat_idle,
        "idle_units": [u["id"] for u in units if u["idle"]],
        "harvesters": [u["id"] for u in units if u["type"] == "harv"],
        "production_buildings": [{"id": b["id"], "type": b["type"]} for b in buildings if b["type"] in PRODUCERS],
        "how_to_send": HOW_TO_SEND,
    }


def empty_legal_actions(session_id: str, state_version: int) -> dict:
    return {"session_id": session_id, "state_version": state_version, "build": [], "train": [], "place": [],
            "deploy": [], "repair": [], "attack": [], "attack_now": None, "enemy_base": None, "combat_units": [],
            "idle_units": [], "harvesters": [], "production_buildings": [], "how_to_send": HOW_TO_SEND}
