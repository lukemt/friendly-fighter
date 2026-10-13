"""Remote control of one Red Alert seat, through files (for a human or an AI
playing from a terminal with scripts/ff_play.py).

Turned on by ``remote/control.json`` (read on every decision, so no restart
is needed)::

    {"enabled": true, "seat": 1, "autopilot": true}

For the controlled seat, on every decision the agent

- writes ``remote/seat<N>/state.json``: a compact summary of the view (and
  ``raw.json``, the full state) at most every ``STATE_EVERY_SECONDS``;
- sends any orders found in ``remote/seat<N>/inbox/*.json`` (each file is
  ``{"orders": [...]}`` and/or ``{"knobs": {...}}``), first and in file-name
  order, then moves the file to ``done/`` with the server's answer appended
  to ``remote/seat<N>/log.jsonl``;
- with ``autopilot`` true, fills the rest of the batch with the scripted
  bot's orders (it keeps building, harvesting and fighting); with it false,
  only the remote orders are sent.

Other seats play normally. ``remote/`` is gitignored.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from altruagent import WAIT, WithReasoning
from altruagent.console import print_line

from .redalert_bot import Knobs, RedAlertBot, _dict, _list, is_combat

# FF_REMOTE_DIR moves the switches (a local simulator run keeps its own, away from the live ones).
ROOT = Path(os.environ.get("FF_REMOTE_DIR") or Path(__file__).resolve().parent.parent / "remote")
STATE_EVERY_SECONDS = 1.0
MAX_ORDERS = 20


def read_control(root: Path | None = None) -> dict:
    try:
        return json.loads(((root or ROOT) / "control.json").read_text())
    except (OSError, ValueError):
        return {}


def _write_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str))
    os.replace(tmp, path)


def summarize(raw: dict) -> dict:
    """What a player needs to decide, in a few KB."""
    obs = _dict(raw.get("observation"))
    legal = _dict(raw.get("legal_actions"))
    units = [u for u in _list(obs.get("units")) if not u.get("wreck")]
    groups: dict[str, list] = {}
    for u in units:
        groups.setdefault(str(u.get("type")), []).append(
            [u.get("id"), u.get("x"), u.get("y"), u.get("hp"), "idle" if u.get("idle") else (u.get("act") or "busy")]
        )
    enemies = [
        [e.get("id"), e.get("type"), e.get("x"), e.get("y"), e.get("hp")]
        for e in _list(obs.get("visible_enemies")) if not e.get("wreck")
    ]
    enemy_buildings = [
        [e.get("id"), e.get("type"), e.get("x"), e.get("y"), e.get("hp")]
        for e in _list(obs.get("visible_enemy_buildings")) if not e.get("wreck")
    ]
    last = _dict(raw.get("last_orders"))
    return {
        "tick": obs.get("tick"),
        "time": _dict(raw.get("time")),
        "phase": raw.get("phase"),
        "faction": obs.get("faction"),
        "enemy_faction": obs.get("enemy_faction"),
        "base_center": obs.get("base_center"),
        "enemy_base": legal.get("enemy_base"),
        "map": {k: _dict(raw.get("map")).get(k) for k in ("id", "bounds")},
        "economy": _dict(obs.get("economy")),
        "military": _dict(obs.get("military")),
        "buildings": [[b.get("id"), b.get("type"), b.get("x"), b.get("y"), b.get("hp"),
                       b.get("producing")] for b in _list(obs.get("buildings"))],
        "units_by_type": groups,
        "combat_unit_ids": [u["id"] for u in units if is_combat(u)],
        "production": [[p.get("queue"), p.get("item"), round(float(p.get("progress") or 0), 2), p.get("ready")]
                       for p in _list(obs.get("production"))],
        "available_production": obs.get("available_production"),
        "enemy_units": enemies,
        "enemy_buildings": enemy_buildings,
        "events": [{k: v for k, v in e.items() if k != "seq"} for e in _list(obs.get("events"))[-10:]],
        "last_orders": [[r.get("cmd"), r.get("status"), r.get("reason"), r.get("detail")]
                        for r in _list(last.get("results"))],
        "recent_order_problems": _list(raw.get("recent_order_problems"))[-5:],
        "explored_percent": obs.get("explored_percent"),
        "result": raw.get("result"),
    }


class RemoteSeat:
    """Wraps the seat's RedAlertBot; see the module docstring."""

    def __init__(self, bot: RedAlertBot, seat: Any, root: Path = ROOT) -> None:
        self.bot = bot
        self.dir = root / f"seat{seat}"
        self.inbox = self.dir / "inbox"
        self.done = self.dir / "done"
        for d in (self.inbox, self.done):
            d.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._last_state_write = 0.0
        self._sent_files: list[str] = []
        self._remote_count = 0
        self._bot_count = 0
        print_line(f"[ff] remote control on for this seat: {self.dir}")

    def choose_action(self, state, context, *, autopilot: bool):
        raw = state.raw
        now = time.monotonic()
        if now - self._last_state_write >= STATE_EVERY_SECONDS and isinstance(raw.get("observation"), dict):
            self._last_state_write = now
            try:
                _write_atomic(self.dir / "state.json", summarize(raw))
                _write_atomic(self.dir / "raw.json", raw)
            except OSError:
                pass

        remote_orders, files = self._take_inbox()
        bot_answer = self.bot.choose_action(state, context) if autopilot else WAIT
        bot_orders = bot_answer.action["orders"] if isinstance(bot_answer, WithReasoning) else []
        orders = (remote_orders + bot_orders)[:MAX_ORDERS]
        self._sent_files = files
        self._remote_count = len(remote_orders)
        self._bot_count = max(0, len(orders) - len(remote_orders))
        if not orders:
            return WAIT
        note = f"remote: {len(remote_orders)} order(s)" if remote_orders else ""
        reasoning = (bot_answer.reasoning_summary or "") if isinstance(bot_answer, WithReasoning) else ""
        return WithReasoning({"type": "orders", "orders": orders}, "; ".join(x for x in (note, reasoning) if x)[:280])

    def on_action_result(self, result, context) -> None:
        if self._sent_files:
            entry = {"t": round(time.time(), 2), "files": self._sent_files,
                     "result": {k: result.get(k) for k in ("accepted", "error", "detail", "results")}
                     if isinstance(result, dict) else str(result)}
            if isinstance(entry["result"], dict) and isinstance(entry["result"].get("results"), list):
                entry["result"]["results"] = entry["result"]["results"][: self._remote_count]
            try:
                with (self.dir / "log.jsonl").open("a") as handle:
                    handle.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                pass
            self._sent_files = []
        # The bot only knows its own orders, which came after the remote ones.
        shift, bot_count = self._remote_count, self._bot_count
        self._remote_count = self._bot_count = 0
        if not bot_count:
            return  # nothing of the bot's was in this batch
        if shift and isinstance(result, dict) and isinstance(result.get("results"), list):
            rows = [dict(r, index=r["index"] - shift) for r in result["results"]
                    if isinstance(r, dict) and isinstance(r.get("index"), int) and r["index"] >= shift]
            result = {**result, "results": rows}
        elif shift and isinstance(result, dict) and result.get("error") == "INVALID_ACTION":
            result = {**result, "detail": None}  # can't attribute a whole-batch refusal: drop it
        self.bot.on_action_result(result, context)

    def _take_inbox(self) -> tuple[list[dict], list[str]]:
        orders: list[dict] = []
        names: list[str] = []
        try:
            files = sorted(p for p in self.inbox.iterdir() if p.suffix == ".json")
        except OSError:
            return orders, names
        for path in files:
            try:
                message = json.loads(path.read_text())
            except (OSError, ValueError):
                continue  # still being written, or broken: try again next view
            if isinstance(message.get("orders"), list):
                orders.extend(o for o in message["orders"] if isinstance(o, dict))
            knobs = message.get("knobs")
            if isinstance(knobs, dict):
                current = self.bot.knobs
                self.bot.set_knobs(Knobs(**{**current.__dict__, **{k: v for k, v in knobs.items()
                                                                     if k in current.__dict__}}))
            try:
                os.replace(path, self.done / path.name)
            except OSError:
                pass
            names.append(path.name)
        return orders, names

