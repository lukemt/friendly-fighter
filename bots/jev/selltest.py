"""A mechanics test, not a bot to compete with (bots/jev/README.md, experiment jev-sell-mechanics).

v10 (bots/jev/v10) that, from ``mcv_at``, trains a second MCV, and once it stands sells every building
(``sell_all``) and keeps the MCV safe at home. It answers: does a sold building count as a loss in
``deaths_cost``? Does the game go on with only an MCV left? How long does the opponent take to find it?
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from altruagent import WAIT, WithReasoning

from .v10.bot import CouncilBot

MAX_ORDERS = 20


class SellTest(CouncilBot):
    def __init__(self, *, mcv_at: float = 360.0, sell_all: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.mcv_at, self.sell_all = mcv_at, sell_all
        self.mcv_queued = False
        self.sold_at: float | None = None
        self.trace: list[dict] = []
        self.last_trace = -1e9
        self.last_move = -1e9

    def choose_action(self, state, context):
        move = super().choose_action(state, context)
        raw = state.raw
        obs = raw.get("observation") or {}
        if not isinstance(obs, dict) or not obs:
            return move
        now = float((raw.get("time") or {}).get("game_seconds") or 0.0)
        units = [u for u in obs.get("units") or [] if not u.get("wreck")]
        buildings = obs.get("buildings") or []
        mcvs = [u for u in units if str(u.get("type")).lower() == "mcv"]
        mine: list[dict[str, Any]] = []
        if now >= self.mcv_at and not mcvs and not self.mcv_queued and "fix" in {str(b.get("type")).lower() for b in buildings}:
            mine.append({"cmd": "train", "item": "mcv", "count": 1})
            self.mcv_queued = True
        if mcvs and self.mcv_queued and self.sell_all and self.sold_at is None:
            self.sold_at = now
        if self.sold_at is not None:
            mine += [{"cmd": "sell", "building": b["id"]} for b in buildings][:MAX_ORDERS]
        if now - self.last_trace >= 10:
            self.last_trace = now
            mil = obs.get("military") or {}
            eco = obs.get("economy") or {}
            self.trace.append({"t": round(now), "cash": eco.get("cash"), "kills": mil.get("kills_cost"),
                               "deaths": mil.get("deaths_cost"), "assets": mil.get("assets_value"),
                               "buildings": len(buildings), "units": len(units), "mcvs": len(mcvs),
                               "production": [(p.get("item"), p.get("progress")) for p in obs.get("production") or []][:6]})
            path = os.environ.get("FF_LS_LOG")
            if path:
                with Path(path).open("a") as f:
                    f.write(json.dumps({"game": getattr(context, "session_id", None), **self.trace[-1]}) + "\n")
        inner = list(move.action.get("orders") or []) if isinstance(move, WithReasoning) else []
        if self.sold_at is not None:
            # (after the sale: no rebuilding, no deploying the MCV; it runs for the corner away from the enemy)
            inner = [o for o in inner if o.get("cmd") not in ("deploy", "build", "place", "train")]
            legal = raw.get("legal_actions") or {}
            enemy = legal.get("enemy_base")
            home = obs.get("base_center") or {}
            if mcvs and isinstance(enemy, dict) and isinstance(home, dict) and now - self.last_move >= 8:
                self.last_move = now
                hx, hy, ex, ey = home.get("x", 0), home.get("y", 0), enemy.get("x", 0), enemy.get("y", 0)
                dx, dy = hx - ex, hy - ey
                n = max(1.0, (dx * dx + dy * dy) ** 0.5)
                to = [int(hx + dx / n * 30), int(hy + dy / n * 30)]
                bounds = (raw.get("map") or {}).get("bounds")
                if isinstance(bounds, list) and len(bounds) == 4:
                    x, y, w, h = (int(v) for v in bounds)
                    to = [min(max(to[0], x + 1), x + w - 2), min(max(to[1], y + 1), y + h - 2)]
                mine.append({"cmd": "move", "units": [u["id"] for u in mcvs], "to": to})
        if not mine and inner == (list(move.action.get("orders") or []) if isinstance(move, WithReasoning) else []):
            return move
        orders = inner[:MAX_ORDERS - len(mine[:MAX_ORDERS])] + mine[:MAX_ORDERS]
        if not orders:
            return WAIT
        return WithReasoning({"type": "orders", "orders": orders}, (getattr(move, "reasoning_summary", "") or "")[:200] + " [selltest]")


def create_agent(**kwargs) -> SellTest:
    return SellTest(**kwargs)
