"""Games played on the real platform, into the lab database (``lab import``).

Our runtime records every match it plays (``agent/recorder.py``: ``recordings/<session>/seat<N>.jsonl.gz``,
every state it saw, every move and answer). This turns each one into a lab game: the seats (ours as
``ff-live``, an opponent by its platform name as ``platform:<name>``), the result, the score every
5 s, the events and every batch, so the same reports work on tournament and Testing games. Local
simulator recordings (``sim-*``) are skipped: lab games are recorded by the lab. Importing again
skips games already in the database, unless they had no result yet.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib
from pathlib import Path

from . import ROOT
from .capture import SAMPLE_TICKS, _timeline_row, summarize_orders

RECORDINGS = ROOT / "recordings"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _records(path: Path):
    try:
        with gzip.open(path, "rt") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except ValueError:
                    return
    except (EOFError, OSError, zlib.error):
        return


def _seat_file(game_id: str, path: Path) -> dict | None:
    pos = int(path.name[4:].split(".")[0]) if path.name[4:].split(".")[0].isdigit() else None
    states, timeline, events, decisions = [], [], [], []
    seen_seq: set = set()
    next_sample = None
    pending: dict | None = None
    last_raw = None
    times: list[float] = []
    for r in _records(path):
        kind = r.get("kind")
        if isinstance(r.get("t"), (int, float)):
            times.append(r["t"]) if not times else (times.append(r["t"]) if len(times) < 2 else times.__setitem__(1, r["t"]))
        if kind == "state":
            raw = r.get("raw") or {}
            last_raw = raw
            obs = raw.get("observation")
            if isinstance(obs, dict) and isinstance(obs.get("tick"), int):
                states.append(raw)
                if pos is None and raw.get("your_seat", "").startswith("Multi"):
                    pos = int(raw["your_seat"][5:])
                if next_sample is None or obs["tick"] >= next_sample:
                    timeline.append(_timeline_row(game_id, pos if pos is not None else 0, obs))
                    next_sample = (obs["tick"] // SAMPLE_TICKS + 1) * SAMPLE_TICKS
                for e in obs.get("events") or []:
                    if e.get("seq") not in seen_seq:
                        seen_seq.add(e.get("seq"))
                        events.append((game_id, pos, e.get("tick"), e.get("kind"), e.get("type"), e.get("x"), e.get("y"),
                                       e.get("count")))
        elif kind == "action":
            move = r.get("move")
            action = move.get("action") if isinstance(move, dict) else None
            if isinstance(action, dict) and action.get("type") == "orders":
                tick = ((last_raw or {}).get("observation") or {}).get("tick")
                pending = {"batch": len(decisions) + 1, "tick": tick, "orders": action.get("orders"),
                           "summary": summarize_orders(action.get("orders")), "reasoning": move.get("reasoning")}
                decisions.append(pending)
        elif kind == "result" and pending is not None:
            res = r.get("result") or {}
            if res.get("error"):
                pending["error"] = res.get("error")
            else:
                pending["verdicts"] = [[x.get("status"), x.get("reason")] if x.get("status") not in ("ok", "pending") else x.get("status")
                                       for x in res.get("results") or []]
            pending = None
    if not states:
        return None
    timeline = [(t[0], pos) + tuple(t[2:]) for t in timeline]
    events = [(e[0], pos) + tuple(e[2:]) for e in events]
    return {"pos": pos, "states": states, "first": states[0], "last": last_raw or states[-1], "timeline": timeline,
            "events": events, "decisions": decisions, "t0": times[0] if times else None, "t1": times[-1] if times else None}


def import_recordings(db, root: Path = RECORDINGS, *, run_id: int | None = None) -> list[str]:
    done = []
    for folder in sorted(root.iterdir()) if root.exists() else []:
        if not folder.is_dir() or not UUID.match(folder.name):
            continue
        gid = folder.name
        old = db.one("SELECT outcome FROM games WHERE id = ?", gid)
        if old is not None and old["outcome"] != "unknown":
            continue
        seats = [s for s in (_seat_file(gid, p) for p in sorted(folder.glob("seat*.jsonl.gz"))) if s]
        if not seats:
            continue
        if old is not None:
            for table in ("games", "seats", "timeline", "events", "decisions"):
                db.conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'games' else 'game_id'} = ?", (gid,))
        _save(db, gid, seats, run_id)
        done.append(gid)
    return done


def _save(db, gid: str, seats: list[dict], run_id: int | None) -> None:
    ours = {s["pos"]: s for s in seats}
    any_seat = seats[0]
    last = max((s["last"] for s in seats), key=lambda raw: ((raw.get("observation") or {}).get("tick") or 0))
    players = last.get("players") or any_seat["first"].get("players") or []
    result = next((s["last"].get("result") for s in seats if s["last"].get("result")), None) or {}
    time_info = last.get("time") or {}
    winner = None
    if isinstance(result.get("winner_seat"), str) and result["winner_seat"].startswith("Multi"):
        winner = int(result["winner_seat"][5:])
    outcome = "unknown" if not result else ("no_result" if result.get("outcome") == "no_result" else
                                            "draw" if winner is None else "decisive")
    first_tick = min((s["first"].get("observation") or {}).get("tick") or 0 for s in seats)
    start = first_tick - int(round(((any_seat["first"].get("time") or {}).get("game_seconds") or 0) * 25))
    t0 = min((s["t0"] for s in seats if s["t0"]), default=None)
    t1 = max((s["t1"] for s in seats if s["t1"]), default=None)
    game = {"id": gid, "run_id": run_id, "created": t0, "finished": t1, "map": (last.get("map") or {}).get("id"),
            "minutes": time_info.get("max_minutes"), "seed": None, "time_mode": "platform",
            "game_seconds": time_info.get("game_seconds"), "wall_seconds": None,
            "termination": result.get("reason") or ("unfinished" if not result else None), "winner": winner,
            "outcome": outcome, "branch_of": None, "branch_tick": None, "branch_label": None, "trace": None,
            "engine": "platform", "info": json.dumps({"start_tick": start, "label": "platform",
                                                     "players": [p.get("name") for p in players]})}
    rows = []
    for pos in range(max(2, len(players))):
        mine = ours.get(pos)
        player = next((p for p in players if p.get("seat") == f"Multi{pos}"), {})
        if mine is not None:
            mil = ((mine["last"].get("observation") or {}).get("military")) or {}
            bot, kind = "ff-live", "platform"
        else:
            other = next(iter(ours.values()))
            om = ((other["last"].get("observation") or {}).get("military")) or {}
            mil = {"kills_cost": om.get("deaths_cost"), "deaths_cost": om.get("kills_cost"), "units_killed": om.get("units_lost"),
                   "units_lost": om.get("units_killed"), "buildings_killed": om.get("buildings_lost"),
                   "buildings_lost": om.get("buildings_killed")}
            bot, kind = "platform:" + re.sub(r"\s*\(Player\d+.*\)$", "", str(player.get("name") or "?")), "platform"
        res = "none"
        if outcome in ("decisive", "draw"):
            res = "draw" if winner is None else "win" if winner == pos else "loss"
        k, d = mil.get("kills_cost"), mil.get("deaths_cost")
        rows.append({"game_id": gid, "position": pos, "bot": bot, "version": f"{bot}@platform", "kind": kind,
                     "faction": player.get("faction"), "result": res,
                     "points": {"win": 1.0, "loss": 0.0, "draw": 0.5}.get(res),
                     "kills_cost": k, "deaths_cost": d, "assets_value": mil.get("assets_value"),
                     "net": (k - d) if k is not None and d is not None else None, "army_value": mil.get("army_value"),
                     "units_killed": mil.get("units_killed"), "units_lost": mil.get("units_lost"),
                     "buildings_killed": mil.get("buildings_killed"), "buildings_lost": mil.get("buildings_lost"),
                     "batches": len(mine["decisions"]) if mine else None,
                     "orders": sum(len(x.get("orders") or []) for x in mine["decisions"]) if mine else None,
                     "rejected": None, "think": None})
    timeline = [row for s in seats for row in s["timeline"]]
    events = [row for s in seats for row in s["events"]]
    db.save_game(game, rows, timeline, events, {s["pos"]: s["decisions"] for s in seats})


def states_at(game_id: str, seconds: float, root: Path = RECORDINGS) -> dict[int, dict]:
    """Each recorded seat's last state at or before ``seconds`` of game time (``lab look`` on a platform game)."""
    out = {}
    for path in sorted((root / game_id).glob("seat*.jsonl.gz")):
        best = None
        for r in _records(path):
            if r.get("kind") != "state":
                continue
            raw = r.get("raw") or {}
            t = (raw.get("time") or {}).get("game_seconds")
            if isinstance(t, (int, float)) and isinstance(raw.get("observation"), dict):
                if t <= seconds or best is None:
                    best = raw
                if t > seconds:
                    break
        if best is not None:
            out[int((best.get("your_seat") or "Multi0")[5:])] = best
    return out
