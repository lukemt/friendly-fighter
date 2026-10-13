"""A deterministic stand-in for the engine, for fast tests of the GameAPI layer.

The test owns the clock: ``advance(ticks)`` moves the world on, applies the commands
whose ``apply_at_tick`` has come (verdict ``ok`` for an actor the seat owns,
``unknown_actor`` otherwise, or whatever ``verdict(seat, command)`` returns) and sends
every agent seat a view, as the bridge does every 6 ticks. Tests edit ``self.world[seat]``
(an engine view dict, see ``ffsim.observe``) to stage anything else.
"""

from __future__ import annotations

import copy


def default_world(seat: int) -> dict:
    mcv = 100 + seat
    spawn = [[66, 72], [26, 32]][seat % 2]
    return {
        "tick": 0, "player": f"Multi{seat}", "faction": ["england", "russia"][seat % 2],
        "enemy_faction": ["russia", "england"][seat % 2], "done": False, "result": "", "winner": "",
        "explored_percent": 3.0,
        "economy": {"cash": 5000, "ore": 0, "power_provided": 0, "power_drained": 0, "resource_capacity": 0,
                    "harvesters": 0},
        "military": {"kills_cost": 0, "deaths_cost": 0, "assets_value": 0, "army_value": 0, "units_killed": 0,
                     "units_lost": 0, "buildings_killed": 0, "buildings_lost": 0},
        "units": [{"id": mcv, "type": "mcv", "x": spawn[0], "y": spawn[1], "hp_percent": 1.0, "idle": True,
                   "activity": None, "stance": 0, "is_husk": False}],
        "buildings": [], "production": [], "available_production": [], "visible_enemies": [],
        "visible_enemy_buildings": [], "command_results": [],
    }


class FakeEngine:
    order_latency_ticks = 0

    def __init__(self, seat_kinds: list[str] | None = None, *, verdict=None) -> None:
        self.seat_kinds = seat_kinds or ["agent", "agent"]
        self.world = {i: default_world(i) for i, k in enumerate(self.seat_kinds) if k == "agent"}
        self.tick = 0
        self.sent: list[dict] = []
        self.results: dict[int, list[dict]] = {i: [] for i in self.world}
        self.verdict = verdict
        self.started = False
        self.stopped = False
        self.surrendered: list[int] = []
        self.on_view = None
        self.on_exit = None

    def start(self, on_view, on_exit) -> None:
        self.on_view, self.on_exit, self.started = on_view, on_exit, True

    def tick_now(self) -> int:
        return self.tick

    def send(self, position: int, batch: int, apply_at_tick: int, commands: list[dict]) -> None:
        self.sent.append({"seat": position, "batch": batch, "apply_at": apply_at_tick, "commands": commands,
                          "applied": False})

    def surrender(self, position: int) -> None:
        self.surrendered.append(position)

    def stop(self) -> None:
        self.stopped = True

    def _owns(self, seat: int, actor_id: int) -> bool:
        w = self.world[seat]
        return any(a["id"] == actor_id for a in w["units"] + w["buildings"])

    def _apply(self) -> None:
        for s in self.sent:
            if s["applied"] or s["apply_at"] > self.tick:
                continue
            s["applied"] = True
            for c in s["commands"]:
                if self.verdict is not None:
                    status, reason = self.verdict(s["seat"], c)
                elif c.get("actor_id") and not self._owns(s["seat"], c["actor_id"]):
                    status, reason = "rejected", "unknown_actor"
                else:
                    status, reason = "ok", "ok"
                self.results[s["seat"]].append({"batch": s["batch"], "index": c.get("index", 0),
                                                "actor_id": c.get("actor_id") or 0, "status": status,
                                                "reason": reason, "tick": self.tick, "detail": ""})

    def advance(self, ticks: int = 6, *, views: bool = True) -> None:
        self.tick += ticks
        self._apply()
        if views:
            self.emit()

    def emit(self) -> None:
        for seat, w in self.world.items():
            w["tick"] = self.tick
            self.results[seat] = [r for r in self.results[seat] if r["tick"] >= self.tick - 150]
            view = copy.deepcopy(w)
            view["command_results"] = list(self.results[seat])
            if self.on_view:
                self.on_view(seat, view)
