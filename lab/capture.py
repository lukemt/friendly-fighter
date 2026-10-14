"""Notes on one match as it plays (a ``Match`` watcher), written to the lab database when it ends.

- every ``SAMPLE_TICKS`` (5 game seconds) and at the end, each agent seat's state: a ``timeline`` row;
- every event of each seat's event log (``ffsim.observe.EventTracker``): an ``events`` row;
- every batch of orders: tick, orders, reasoning, and the verdicts once they settle (``decisions``).

An OpenRA AI seat has no view of its own: its kills and losses are the other seat's, mirrored.
"""

from __future__ import annotations

from ffsim.observe import NON_COMBAT, SHIPS

SAMPLE_TICKS = 125
TICKS_PER_SECOND = 25


def _timeline_row(game_id: str, position: int, obs: dict) -> tuple:
    eco, mil = obs.get("economy") or {}, obs.get("military") or {}
    units = [u for u in obs.get("units") or [] if not u.get("wreck")]
    combat = sum(1 for u in units if u["type"] not in NON_COMBAT and u["type"] not in SHIPS)
    return (game_id, position, obs["tick"], eco.get("cash"), eco.get("ore"), eco.get("power_provided"),
            eco.get("power_drained"), eco.get("harvesters"), len(units), combat, len(obs.get("buildings") or []),
            mil.get("army_value"), mil.get("kills_cost"), mil.get("deaths_cost"), mil.get("assets_value"),
            obs.get("explored_percent"), sum(1 for e in obs.get("visible_enemies") or [] if not e.get("wreck")),
            len(obs.get("visible_enemy_buildings") or []))


def summarize_orders(orders: list) -> str:
    """``build:powr train:e1x3 attack_move:12u>(40,52)``: a batch in one line."""
    parts = []
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        cmd = str(o.get("cmd"))
        if o.get("item"):
            part = f"{cmd}:{o['item']}" + (f"x{o['count']}" if (o.get("count") or 1) != 1 else "")
        elif isinstance(o.get("units"), list):
            part = f"{cmd}:{len(o['units'])}u"
        else:
            part = cmd
        if isinstance(o.get("to"), list):
            part += f">({o['to'][0]},{o['to'][1]})"
        elif o.get("target") is not None:
            part += f">#{o['target']}"
        parts.append(part)
    return " ".join(parts)


class Capture:
    def __init__(self, game_id: str) -> None:
        self.game_id = game_id
        self.timeline: list[tuple] = []
        self.events: list[tuple] = []
        self.decisions: dict[int, list] = {}
        self.batches: dict[tuple[int, int], dict] = {}
        self.next_sample: dict[int, int] = {}
        self.last_obs: dict[int, dict] = {}
        self.sampled: set[tuple[int, int]] = set()
        self.attached = False

    def attach(self, match) -> "Capture":
        match.watchers.append(self)
        for seat in match.seats:
            if seat.is_agent:
                pos = seat.position
                seat.tracker.sink = lambda ev, pos=pos: self._event(pos, ev)
        self.attached = True
        return self

    # -- the match's hooks (holding its lock) ----------------------------------------------

    def on_view(self, match, seat, view) -> None:
        obs = seat.obs
        if not obs:
            return
        self.last_obs[seat.position] = obs
        due = self.next_sample.get(seat.position)
        if due is None or obs["tick"] >= due:
            self._sample(seat.position, obs)
            self.next_sample[seat.position] = (obs["tick"] // SAMPLE_TICKS + 1) * SAMPLE_TICKS

    def _sample(self, position: int, obs: dict) -> None:
        if (position, obs["tick"]) not in self.sampled:
            self.sampled.add((position, obs["tick"]))
            self.timeline.append(_timeline_row(self.game_id, position, obs))

    def _event(self, position: int, ev: dict) -> None:
        self.events.append((self.game_id, position, ev.get("tick"), ev.get("kind"), ev.get("type"), ev.get("x"),
                            ev.get("y"), ev.get("count")))

    def on_batch(self, match, seat, batch, tick, orders, rows, reasoning) -> None:
        entry = {"batch": batch, "tick": tick, "orders": orders, "summary": summarize_orders(orders),
                 "reasoning": (str(reasoning)[:300] if reasoning else None),
                 "checked": [r.get("status") for r in rows]}
        self.decisions.setdefault(seat.position, []).append(entry)
        self.batches[(seat.position, batch)] = entry

    def on_settle(self, match, seat, batch, rows) -> None:
        entry = self.batches.pop((seat.position, batch), None)
        if entry is not None:
            entry["verdicts"] = [[r.get("status"), r.get("reason")] if r.get("status") != "ok" else "ok" for r in rows]

    def on_end(self, match) -> None:
        for position, obs in self.last_obs.items():
            self._sample(position, obs)  # (the end, wherever it falls)

    # -- for the database -------------------------------------------------------------------

    def final_military(self, position: int, other: int) -> dict:
        """The seat's military totals at the end (an OpenRA AI's: the other seat's, mirrored)."""
        obs = self.last_obs.get(position)
        if obs:
            return dict(obs.get("military") or {})
        o = (self.last_obs.get(other) or {}).get("military") or {}
        return {"kills_cost": o.get("deaths_cost"), "deaths_cost": o.get("kills_cost"), "assets_value": None,
                "army_value": None, "units_killed": o.get("units_lost"), "units_lost": o.get("units_killed"),
                "buildings_killed": o.get("buildings_lost"), "buildings_lost": o.get("buildings_killed")}

    def counts(self, position: int) -> tuple[int, int, int]:
        rows = self.decisions.get(position) or []
        orders = sum(len(r["orders"] or []) for r in rows)
        rejected = sum(1 for r in rows for v in (r.get("verdicts") or r.get("checked") or [])
                       if (v[0] if isinstance(v, list) else v) == "rejected")
        return len(rows), orders, rejected
