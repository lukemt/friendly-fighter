"""Reports on the lab database, as text: what happened, who is better, where points are lost.

Every report reads the database only (``lab sql`` for anything else). Games that started from a
snapshot (branches) are left out of statistics and ratings unless asked for: they don't start at
the start.
"""

from __future__ import annotations

import math

import json
import time
from collections import Counter, defaultdict

from ffsim.rules import Rules

from . import bots as registry
from .ratings import fit, wilson
from .views import clock

TPS = 25


# -- filters ----------------------------------------------------------------------------------


def since_ts(text: str | None) -> float | None:
    """``2h`` / ``3d`` / ``30m`` ago, or a date ``2026-10-14``."""
    if not text:
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    if text[-1] in units and text[:-1].replace(".", "").isdigit():
        return time.time() - float(text[:-1]) * units[text[-1]]
    return time.mktime(time.strptime(text, "%Y-%m-%d"))


def where_games(f: dict, alias: str = "g") -> tuple[str, list]:
    conds, args = [], []
    if not f.get("branches"):
        conds.append(f"{alias}.branch_of IS NULL")
    if f.get("run") is not None:
        conds.append(f"{alias}.run_id = ?")
        args.append(int(f["run"]))
    if f.get("map"):
        conds.append(f"{alias}.map = ?")
        args.append(f["map"])
    ts = since_ts(f.get("since"))
    if ts:
        conds.append(f"{alias}.finished >= ?")
        args.append(ts)
    if f.get("label"):
        conds.append(f"json_extract({alias}.info, '$.label') LIKE ?")
        args.append(f["label"])
    if not f.get("failed"):
        conds.append(f"{alias}.outcome != 'no_result'")
    return (" AND ".join(conds) or "1=1"), args


def current_versions() -> dict[str, str]:
    try:
        return {name: b.version() for name, b in registry.load().items()}
    except Exception:  # noqa: BLE001
        return {}


def perspective(db, bot: str, f: dict) -> list[dict]:
    """The games of ``bot`` from its side: one row per seat it played (both, in self-play)."""
    w, args = where_games(f)
    sql = (f"SELECT g.*, s.position AS pos, s.bot, s.version, s.faction, s.result, s.net, s.kills_cost, s.deaths_cost, "
           f"s.assets_value, s.batches, s.rejected, o.bot AS opp, o.version AS opp_version, o.faction AS opp_faction, "
           f"o.assets_value AS opp_assets FROM seats s JOIN seats o ON o.game_id = s.game_id AND o.position != s.position "
           f"JOIN games g ON g.id = s.game_id WHERE s.bot = ? AND {w}")
    rows = [dict(r) for r in db.q(sql, bot, *args)]
    if f.get("vs"):
        rows = [r for r in rows if r["opp"] == f["vs"]]
    version = f.get("version")
    if version == "current":
        cur = current_versions()
        rows = [r for r in rows if r["version"] == cur.get(bot, r["version"])]
    elif version and version != "all":
        rows = [r for r in rows if r["version"] == version or r["version"].endswith("@" + version)]
    return rows


# -- tables -----------------------------------------------------------------------------------


def _table(rows: list[list], header: list[str]) -> str:
    cols = [header] + [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(c[i]) for c in cols) for i in range(len(header))]
    out = ["  ".join(h.ljust(w) for h, w in zip(header, widths)), "  ".join("-" * w for w in widths)]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in cols[1:]]
    return "\n".join(out)


def short(game_id: str) -> str:
    """A game id's first 8 characters after ``lab-`` / ``sim-``: enough for every command."""
    return game_id[4:12] if game_id[:4] in ("lab-", "sim-") else game_id[:8]


def _when(ts) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _res(result: str | None) -> str:
    return {"win": "W", "loss": "L", "draw": "D", "none": "-"}.get(result or "none", "?")


def games_table(db, f: dict, limit: int = 30) -> str:
    w, args = where_games(f)
    sql = f"SELECT g.* FROM games g WHERE {w}"
    if f.get("bot"):
        sql += " AND g.id IN (SELECT game_id FROM seats WHERE bot = ?)"
        args.append(f["bot"])
    if f.get("vs"):
        sql += " AND g.id IN (SELECT game_id FROM seats WHERE bot = ?)"
        args.append(f["vs"])
    sql += " ORDER BY g.finished DESC LIMIT ?"
    args.append(limit)
    rows = []
    for g in db.q(sql, *args):
        seats = {s["position"]: s for s in db.q("SELECT * FROM seats WHERE game_id = ? ORDER BY position", g["id"])}
        a, b = seats.get(0), seats.get(1)
        if not a or not b:
            continue
        rows.append([short(g["id"]), _when(g["finished"]), g["map"], f"{a['bot']} ({(a['faction'] or '?')[:3]})",
                     f"{_res(a['result'])}-{_res(b['result'])}", f"{b['bot']} ({(b['faction'] or '?')[:3]})",
                     a["net"], clock(g["game_seconds"]), g["termination"],
                     (f"branch of {short(g['branch_of'])}@{clock((g['branch_tick'] or 0) / TPS)} {g['branch_label']}"
                      if g["branch_of"] else (json.loads(g["info"] or "{}").get("label") or ""))])
    if not rows:
        return "(no games)"
    return _table(rows, ["game", "finished", "map", "seat 0", "result", "seat 1", "net0", "length", "ended by", "note"])


def _net_series(db, game_id: str, pos: int, other_pos: int, kind: str) -> list[tuple[int, int, dict]]:
    """(tick, net, row) for a seat; an OpenRA AI's mirrored from the other seat."""
    if kind != "ai":
        rows = db.q("SELECT * FROM timeline WHERE game_id = ? AND position = ? ORDER BY tick", game_id, pos)
        if rows:
            return [(r["tick"], (r["kills_cost"] or 0) - (r["deaths_cost"] or 0), dict(r)) for r in rows]
    # An OpenRA AI (or a platform opponent we didn't record): the other seat's, mirrored.
    rows = db.q("SELECT * FROM timeline WHERE game_id = ? AND position = ? ORDER BY tick", game_id, other_pos)
    return [(r["tick"], (r["deaths_cost"] or 0) - (r["kills_cost"] or 0), {}) for r in rows]


def spark(values: list[float], width: int = 60) -> str:
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    chars = " .:-=+*#%@"
    if hi == lo:
        return chars[4] * len(values)
    return "".join(chars[int((v - lo) / (hi - lo) * (len(chars) - 1))] for v in values)


def game_detail(db, ref: str, *, events: int = 40, decisions: int = 0) -> str:
    g = db.find_game(ref)
    if g is None:
        return f"no game {ref!r}"
    info = json.loads(g["info"] or "{}")
    seats = {s["position"]: dict(s) for s in db.q("SELECT * FROM seats WHERE game_id = ? ORDER BY position", g["id"])}
    run = db.one("SELECT * FROM runs WHERE id = ?", g["run_id"]) if g["run_id"] else None
    start = info.get("start_tick") or 0
    lines = [f"game {g['id']}  ({_when(g['finished'])}, run {g['run_id']}"
             + (f": {run['kind']} {run['note'] or ''}" if run else "") + ")",
             f"map {g['map']}, seed {g['seed']}, limit {g['minutes']} min; lasted {clock(g['game_seconds'])} "
             f"({g['wall_seconds']} s of wall time); ended by {g['termination']}"
             + (f", winner seat {g['winner']}" if g["winner"] is not None else f", {g['outcome']}")]
    if g["branch_of"]:
        lines.append(f"branch of {g['branch_of']} at {clock((g['branch_tick'] - start) / TPS)} ({g['branch_label']}); "
                     f"snapshot check: {info.get('snapshot_check')}")
    if info.get("errors"):
        lines.append(f"errors: {info['errors']}")
    lines.append("")
    rows = []
    for pos, s in seats.items():
        rows.append([pos, s["bot"], s["version"], s["faction"], s["result"], s["kills_cost"], s["deaths_cost"], s["net"],
                     s["assets_value"], f"{s['units_killed']}/{s['units_lost']}", f"{s['buildings_killed']}/{s['buildings_lost']}",
                     s["batches"], s["orders"], s["rejected"], s["think"]])
    lines.append(_table(rows, ["seat", "bot", "version", "faction", "result", "kills$", "losses$", "net", "assets$",
                               "units k/l", "bldgs k/l", "batches", "orders", "rejected", "think"]))
    # The score and economy every 30 s.
    lines.append("")
    series = {pos: _net_series(db, g["id"], pos, 1 - pos, s["kind"]) for pos, s in seats.items()}
    agent_rows = {pos: [r for _, _, r in series[pos] if r] for pos in seats}
    head = ["time"]
    for pos, s in seats.items():
        head += [f"net{pos}"] + ([f"cash{pos}", f"army${pos}", f"units{pos}", f"bldg{pos}"] if agent_rows[pos] else [])
    table = []
    ticks = sorted({t for pos in series for t, _, _ in series[pos]})
    marks = [m for m in (start + k * 30 * TPS for k in range(0, int((ticks[-1] - start) / (30 * TPS)) + 1))
             if m >= ticks[0] - 125] if ticks else []
    for t in marks + ticks[-1:]:
        row = [clock((t - start) / TPS)]
        for pos in seats:
            hit = [(tt, n, r) for tt, n, r in series[pos] if tt <= t]
            tt, n, r = hit[-1] if hit else (t, 0, {})
            row.append(n)
            if agent_rows[pos]:
                row += [r.get("cash"), r.get("army_value"), r.get("combat_units"), r.get("buildings")]
        if not table or table[-1] != row:
            table.append(row)
    lines.append(_table(table, head))
    for pos in seats:
        if series[pos]:
            lines.append(f"net{pos} over time: [{spark([n for _, n, _ in series[pos]])}] "
                         f"min {min(n for _, n, _ in series[pos])} max {max(n for _, n, _ in series[pos])}")
    # Events: what was lost and destroyed.
    lines.append("")
    lines.append("key events (buildings made/lost, kills, losses of 2+ units within 5 s):")
    for pos, s in seats.items():
        if s["kind"] == "ai":
            continue
        evs = db.q("SELECT * FROM events WHERE game_id = ? AND position = ? ORDER BY tick, rowid", g["id"], pos)
        picked = []
        fighting: list = []
        for e in evs:
            k = e["kind"]
            if k in ("building_created", "building_lost", "building_sold"):
                picked.append((e["tick"], f"{k} {e['type']} @{e['x']},{e['y']}"))
            elif k in ("enemy_killed", "unit_lost"):
                fighting.append(e)
        picked += [(t, text) for t, text in fights(fighting, start)]
        picked.sort()
        lines.append(f" seat {pos} ({s['bot']}), {len(picked)} events" + (f", last {events}:" if len(picked) > events else ":"))
        for t, text in picked[-events:]:
            lines.append(f"   {clock((t - start) / TPS)} {text}")
    if decisions:
        lines.append("")
        for pos, s in seats.items():
            rows = db.decisions(g["id"], pos)
            if not rows:
                continue
            lines.append(f"seat {pos} ({s['bot']}): {len(rows)} batches; reasoning changes:")
            last = None
            shown = 0
            for r in rows:
                if r.get("reasoning") != last:
                    last = r.get("reasoning")
                    lines.append(f"   {clock((r['tick'] - start) / TPS)} {r['summary'][:70]:70} | {last or ''}"[:200])
                    shown += 1
                    if shown >= decisions:
                        break
    notes = db.q("SELECT * FROM notes WHERE game_id = ? ORDER BY created", g["id"])
    if notes:
        lines.append("")
        lines.append("notes:")
        lines += [f"  [{n['author']}] {n['text']}" for n in notes]
    human = db.q("SELECT * FROM human_log WHERE game_id = ? ORDER BY wall", g["id"])
    if human:
        lines.append(f"\nhuman commands: {len(human)} (lab sql \"select * from human_log where game_id='{g['id']}'\")")
    lines.append(f"\ntrace: {g['trace']}")
    return "\n".join(lines)


def fights(events: list, start: int, gap_seconds: float = 10.0) -> list[tuple[int, str]]:
    """Unit losses and kills less than ``gap_seconds`` apart, as fights: when, how long, lost what, killed how many."""
    out = []
    group: list = []
    for e in list(events) + [None]:
        if group and (e is None or e["tick"] - group[-1]["tick"] > gap_seconds * TPS):
            lost = Counter(x["type"] for x in group if x["kind"] == "unit_lost")
            killed = sum(x["count"] or 0 for x in group if x["kind"] == "enemy_killed")
            span = (group[-1]["tick"] - group[0]["tick"]) / TPS
            out.append((group[0]["tick"], f"fight ({span:.0f} s): lost {sum(lost.values())}"
                        + (" (" + ", ".join(f"{k} x{v}" for k, v in lost.most_common(6)) + ")" if lost else "")
                        + f", killed {killed}"))
            group = []
        if e is not None:
            group.append(e)
    return out


def stats(db, bot: str, f: dict, by: str | None = None) -> str:
    rows = perspective(db, bot, f)
    if not rows:
        return f"no games of {bot} (with these filters)"
    keyf = {"opponent": lambda r: r["opp"], "map": lambda r: r["map"], "faction": lambda r: r["faction"],
            "seat": lambda r: r["pos"], "version": lambda r: r["version"], "opp_faction": lambda r: r["opp_faction"],
            None: lambda r: "all"}[by]
    groups: dict = defaultdict(list)
    for r in rows:
        groups[keyf(r)].append(r)
    out = []
    for key, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        w = sum(1 for r in rs if r["result"] == "win")
        d = sum(1 for r in rs if r["result"] == "draw")
        lo, hi = wilson(w + 0.5 * d, n)
        nets = sorted(r["net"] or 0 for r in rs)
        wipe_for = sum(1 for r in rs if r["termination"] == "victory" and r["result"] == "win")
        wipe_against = sum(1 for r in rs if r["termination"] == "victory" and r["result"] == "loss")
        out.append([key, n, w, d, n - w - d, f"{100 * (w + 0.5 * d) / n:.0f}%", f"{100 * lo:.0f}-{100 * hi:.0f}%",
                    nets[n // 2], round(sum(nets) / n), wipe_for, wipe_against])
    head = [by or "", "games", "W", "D", "L", "score", "95% CI", "net median", "net mean", "wiped them", "wiped us"]
    return f"{bot} ({f.get('version') or 'all versions'})\n" + _table(out, head)


def h2h(db, a: str, b: str, f: dict) -> str:
    f = {**f, "vs": b}
    text = stats(db, a, f, "map")
    return f"{a} vs {b}\n" + text


def ratings_table(db, f: dict, bots: list[str] | None = None, by_version: bool = False) -> str:
    w, args = where_games(f)
    rows = db.q(f"SELECT g.id, s.position, s.bot, s.version, s.points FROM games g JOIN seats s ON s.game_id = g.id "
                f"WHERE {w} AND g.outcome != 'no_result' ORDER BY g.id, s.position", *args)
    by_game: dict = defaultdict(dict)
    for r in rows:
        by_game[r["id"]][r["position"]] = r
    results = []
    cur = current_versions() if f.get("version") == "current" else None
    for gid, seats in by_game.items():
        if 0 not in seats or 1 not in seats or seats[0]["points"] is None:
            continue
        a, b = seats[0], seats[1]
        if cur is not None and (a["version"] != cur.get(a["bot"], a["version"]) or b["version"] != cur.get(b["bot"], b["version"])):
            continue
        ka, kb = (a["version"], b["version"]) if by_version else (a["bot"], b["bot"])
        if bots and not (a["bot"] in bots and b["bot"] in bots):
            continue
        results.append((ka, kb, float(a["points"])))
    if not results:
        return "(no games)"
    r = fit(results)
    table = []
    for i, (name, v) in enumerate(sorted(r.items(), key=lambda kv: -kv[1]["rating"]), 1):
        table.append([i, name, round(v["rating"]), f"±{round(v['sd'])}", v["games"], f"{100 * v['score'] / v['games']:.0f}%"])
    return (f"ratings from {len(results)} games (Bradley-Terry, Elo scale; ± one standard error)\n"
            + _table(table, ["#", "bot", "rating", "sd", "games", "score"]))


def pairs_matrix(db, f: dict, bots: list[str] | None = None) -> str:
    w, args = where_games(f)
    rows = db.q(f"SELECT g.id, s.position, s.bot, s.points FROM games g JOIN seats s ON s.game_id = g.id WHERE {w}", *args)
    by_game: dict = defaultdict(dict)
    for r in rows:
        by_game[r["id"]][r["position"]] = r
    score: dict = defaultdict(float)
    n: dict = defaultdict(int)
    names = set()
    for seats in by_game.values():
        if 0 not in seats or 1 not in seats or seats[0]["points"] is None:
            continue
        a, b = seats[0]["bot"], seats[1]["bot"]
        if bots and not (a in bots and b in bots):
            continue
        names |= {a, b}
        score[(a, b)] += seats[0]["points"]
        score[(b, a)] += seats[1]["points"]
        n[(a, b)] += 1
        n[(b, a)] += 1
    names = sorted(names)
    rows = [[a] + [f"{score[(a, b)]:g}/{n[(a, b)]}" if n[(a, b)] else "" for b in names] for a in names]
    return "score of the row's bot against the column's (points/games)\n" + _table(rows, ["vs"] + names)


def net_by_minute(db, game_ids: list[str], bot: str) -> list[float | None]:
    """Mean net score of ``bot`` at minutes 1..10 over these games."""
    sums, counts = [0.0] * 11, [0] * 11
    for gid in game_ids:
        g = db.one("SELECT info FROM games WHERE id = ?", gid)
        start = json.loads(g["info"] or "{}").get("start_tick") or 0
        seats = {s["position"]: s for s in db.q("SELECT * FROM seats WHERE game_id = ?", gid)}
        for pos, s in seats.items():
            if s["bot"] != bot:
                continue
            series = _net_series(db, gid, pos, 1 - pos, s["kind"])
            for m in range(1, 11):
                t = start + m * 60 * TPS
                hit = [n for tt, n, _ in series if tt <= t]
                if hit and series and series[-1][0] >= t - 125:
                    sums[m] += hit[-1]
                    counts[m] += 1
    return [round(sums[m] / counts[m]) if counts[m] else None for m in range(1, 11)]


def unit_costs() -> dict[str, int]:
    rules = Rules.load().config.get("rules") or {}
    out = {}
    for section in ("units", "buildings"):
        for name, info in (rules.get(section) or {}).items():
            if isinstance(info, dict) and isinstance(info.get("cost"), (int, float)):
                out[name.lower()] = int(info["cost"])
    return out


def observer(db, f: dict, focus: list[str] | None = None, worst: int = 5) -> str:
    """What a batch of games says: per bot, how it went, when it fell behind, what it lost, the worst games."""
    w, args = where_games(f)
    games = [dict(g) for g in db.q(f"SELECT * FROM games g WHERE {w} ORDER BY finished", *args)]
    if not games:
        return "(no games)"
    ids = [g["id"] for g in games]
    seats = defaultdict(list)
    for gid in ids:
        for s in db.q("SELECT * FROM seats WHERE game_id = ?", gid):
            seats[s["bot"]].append(dict(s))
    bots = focus or sorted(seats, key=lambda b: -len(seats[b]))
    costs = unit_costs()
    out = [f"{len(games)} games, {_when(games[0]['finished'])} to {_when(games[-1]['finished'])}; "
           f"ended by: " + ", ".join(f"{k} {v}" for k, v in Counter(g["termination"] for g in games).most_common())]
    nores = [g for g in games if g["outcome"] == "no_result"]
    if nores:
        out.append(f"!! {len(nores)} games without a result: " + ", ".join(f"{short(g['id'])} ({g['termination']})" for g in nores[:8]))
    errs = [g for g in games if (json.loads(g["info"] or "{}").get("errors"))]
    if errs:
        out.append(f"!! {len(errs)} games where a bot raised an error: " + ", ".join(short(g["id"]) for g in errs[:8]))
    for bot in bots:
        mine = seats.get(bot) or []
        if not mine:
            continue
        n = len(mine)
        wins = sum(1 for s in mine if s["result"] == "win")
        draws = sum(1 for s in mine if s["result"] == "draw")
        out.append("")
        out.append(f"== {bot}: {n} games, {wins} W {draws} D {n - wins - draws} L, mean net "
                   f"{round(sum(s['net'] or 0 for s in mine) / n)}")
        curve = net_by_minute(db, list({s["game_id"] for s in mine}), bot)
        out.append("   mean net at minute 1..10: " + " ".join("-" if v is None else f"{v:+d}" for v in curve))
        if mine[0]["kind"] != "ai":
            lost = Counter()
            killed_n = 0
            gids = [s["game_id"] for s in mine]
            for s in mine:
                for e in db.q("SELECT type, kind, count FROM events WHERE game_id = ? AND position = ? AND kind IN "
                              "('unit_lost', 'building_lost', 'enemy_killed')", s["game_id"], s["position"]):
                    if e["kind"] == "enemy_killed":
                        killed_n += e["count"] or 0
                    else:
                        lost[e["type"]] += 1
            spent = sorted(((costs.get(t, 0) * c, t, c) for t, c in lost.items()), reverse=True)
            out.append("   lost per game (by value): " + ", ".join(f"{t} {c / n:.1f} (${v / n:.0f})" for v, t, c in spent[:8]))
            out.append(f"   kills per game: {killed_n / n:.1f}; orders per game {sum(s['orders'] or 0 for s in mine) / n:.0f}, "
                       f"refused {sum(s['rejected'] or 0 for s in mine) / n:.1f}")
            few = [s for s in mine if (s["batches"] or 0) < 20]
            if few and bot not in ("idle",):
                out.append(f"   !! {len(few)} games with under 20 batches: " + ", ".join(short(s["game_id"]) for s in few[:6]))
            del gids
        by_opp = defaultdict(list)
        for s in mine:
            o = db.one("SELECT bot FROM seats WHERE game_id = ? AND position != ?", s["game_id"], s["position"])
            by_opp[o["bot"] if o else "?"].append(s)
        out.append("   by opponent: " + "; ".join(
            f"{o} {sum(1 for s in ss if s['result'] == 'win')}-{sum(1 for s in ss if s['result'] == 'draw')}-"
            f"{sum(1 for s in ss if s['result'] == 'loss')} (net {round(sum(s['net'] or 0 for s in ss) / len(ss)):+d})"
            for o, ss in sorted(by_opp.items(), key=lambda kv: -len(kv[1]))))
        worst_games = sorted(mine, key=lambda s: s["net"] or 0)[:worst]
        if worst_games and mine[0]["kind"] != "ai":
            out.append("   worst games and their biggest 30-second swing:")
            for s in worst_games:
                out.append("     " + _swing(db, s))
    return "\n".join(out)


def _swing(db, s: dict) -> str:
    g = db.one("SELECT * FROM games WHERE id = ?", s["game_id"])
    start = json.loads(g["info"] or "{}").get("start_tick") or 0
    series = _net_series(db, s["game_id"], s["position"], 1 - s["position"], s["kind"])
    best = (0, None, None)
    for i, (t, n, _) in enumerate(series):
        for t2, n2, _ in series[i + 1:]:
            if t2 - t > 30 * TPS:
                break
            if n2 - n < best[0]:
                best = (n2 - n, t, t2)
    text = f"{short(s['game_id'])} {g['map']} {s['result']} net {s['net']}"
    if best[1] is not None:
        lost = db.q("SELECT type FROM events WHERE game_id = ? AND position = ? AND kind IN ('unit_lost','building_lost') "
                    "AND tick BETWEEN ? AND ?", s["game_id"], s["position"], best[1], best[2])
        text += (f": {best[0]:+d} between {clock((best[1] - start) / TPS)} and {clock((best[2] - start) / TPS)}"
                 + (" (lost " + ", ".join(f"{k} x{v}" for k, v in Counter(r['type'] for r in lost).most_common(5)) + ")"
                    if lost else ""))
    return text


def branch_outcomes(db, origin_id: str, tick: int) -> dict[str, list[int]]:
    """Each branch label from this tick: the branched seat's final net in each repeat."""
    out: dict[str, list[int]] = defaultdict(list)
    for g in db.q("SELECT * FROM games WHERE branch_of = ? AND branch_tick = ?", origin_id, tick):
        seat = json.loads(g["info"] or "{}").get("branch_seat", 0)
        s = db.one("SELECT net FROM seats WHERE game_id = ? AND position = ?", g["id"], seat)
        if s is not None and s["net"] is not None:
            out[g["branch_label"]].append(s["net"])
    return dict(out)


def branch_report(db, ref: str, horizons: tuple[int, ...] = (30, 60, 120)) -> str:
    g = db.find_game(ref)
    if g is None:
        return f"no game {ref!r}"
    origin = db.one("SELECT * FROM games WHERE id = ?", g["branch_of"]) if g["branch_of"] else g
    rows = db.q("SELECT * FROM games WHERE branch_of = ? ORDER BY branch_tick, branch_label", origin["id"])
    if not rows:
        return f"no branches of {origin['id']}"
    start = json.loads(origin["info"] or "{}").get("start_tick") or 0
    out = [f"branches of {origin['id']} ({origin['map']})"]
    by: dict = defaultdict(list)
    for r in rows:
        by[(r["branch_tick"], r["branch_label"])].append(r)
    for tick in sorted({t for t, _ in by}):
        out.append(f"\nfrom {clock((tick - start) / TPS)} (tick {tick}):")
        table = []
        for (t, label), games in sorted(by.items()):
            if t != tick:
                continue
            info = json.loads(games[0]["info"] or "{}")
            seat = info.get("branch_seat", 0)
            deltas = {h: [] for h in horizons}
            finals, results = [], Counter()
            for gm in games:
                s = db.one("SELECT * FROM seats WHERE game_id = ? AND position = ?", gm["id"], seat)
                series = _net_series(db, gm["id"], seat, 1 - seat, s["kind"])
                if not series:
                    continue
                base = series[0][1]
                for h in horizons:
                    hit = [n for tt, n, _ in series if tt <= tick + h * TPS]
                    if series[-1][0] >= tick + h * TPS - 125 and hit:
                        deltas[h].append(hit[-1] - base)
                finals.append((s["net"] or 0) - base)
                results[s["result"]] += 1
            n = len(games)
            table.append([label, n] + [round(sum(v) / len(v)) if v else "-" for v in deltas.values()]
                         + [round(sum(finals) / len(finals)) if finals else "-",
                            f"{results['win']}-{results['draw']}-{results['loss']}"])
        out.append(_table(table, ["variant", "games"] + [f"net +{h}s" for h in horizons] + ["net to end", "W-D-L"]))
    out.append("\n(net: kills minus losses of the branched seat since the snapshot, mean over the games)")
    return "\n".join(out)


def gate_report(db, run_id: int, candidate: str, champion: str) -> str:
    f = {"run": run_id}
    out = [f"gate: {candidate} (candidate) vs {champion} (champion), run {run_id}"]
    h = [r for r in perspective(db, candidate, {**f, "vs": champion})]
    if h:
        w = sum(1 for r in h if r["result"] == "win") + 0.5 * sum(1 for r in h if r["result"] == "draw")
        lo, hi = wilson(w, len(h))
        out.append(f"head to head: {w:g}/{len(h)} ({100 * w / len(h):.0f}%, 95% CI {100 * lo:.0f}-{100 * hi:.0f}%), "
                   f"mean net {round(sum(r['net'] or 0 for r in h) / len(h)):+d}")
    baselines = sorted({r["opp"] for r in perspective(db, candidate, f)} - {champion})
    table = []
    worse = []
    for b in baselines:
        c = perspective(db, candidate, {**f, "vs": b})
        k = perspective(db, champion, {**f, "vs": b})
        sc = sum(1 for r in c if r["result"] == "win") + 0.5 * sum(1 for r in c if r["result"] == "draw")
        sk = sum(1 for r in k if r["result"] == "win") + 0.5 * sum(1 for r in k if r["result"] == "draw")
        nc = round(sum(r["net"] or 0 for r in c) / len(c)) if c else None
        nk = round(sum(r["net"] or 0 for r in k) / len(k)) if k else None
        table.append([b, f"{sc:g}/{len(c)}", f"{sk:g}/{len(k)}", nc, nk])
        if c and k and sc / len(c) + 0.15 < sk / len(k):
            worse.append(b)
    if table:
        out.append(_table(table, ["baseline", candidate, champion, f"net {candidate}", f"net {champion}"]))
    paired = paired_difference(db, run_id, candidate, champion, baselines)
    verdict, why = gate_verdict(paired, h)
    if paired["n"]:
        out.append(f"paired (same seeds, {paired['n']} pairs): candidate - champion = {paired['mean']:+.2f} points a game "
                   f"(95% CI {paired['lo']:+.2f} to {paired['hi']:+.2f}); net {paired['net']:+.0f} a game")
        for b, d in paired["by_baseline"].items():
            if d["hi"] < 0:
                out.append(f"  regression against {b}: {d['mean']:+.2f} (CI {d['lo']:+.2f} to {d['hi']:+.2f})")
    out.append(f"verdict: {verdict} ({why})")
    return "\n".join(out)


def _points(result: str | None) -> float:
    return 1.0 if result == "win" else 0.5 if result == "draw" else 0.0


def _mean_ci(xs: list[float]) -> tuple[float, float, float]:
    """Mean and a 95% interval (normal approximation; wide on purpose with few games)."""
    n = len(xs)
    if not n:
        return 0.0, -1.0, 1.0
    mean = sum(xs) / n
    if n < 2:
        return mean, -1.0, 1.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    half = 1.96 * math.sqrt(max(var, 0.05) / n)  # (a floor: 6 identical pairs are not certainty)
    return mean, mean - half, mean + half


def paired_difference(db, run_id: int, candidate: str, champion: str, baselines: list[str]) -> dict:
    """Candidate and champion played each baseline on the same seeds: the per-seed differences."""
    diffs, nets, by = [], [], {}
    for b in baselines:
        c = {(r["seed"], r["map"]): r for r in perspective(db, candidate, {"run": run_id, "vs": b})}
        k = {(r["seed"], r["map"]): r for r in perspective(db, champion, {"run": run_id, "vs": b})}
        d = [_points(c[key]["result"]) - _points(k[key]["result"]) for key in c if key in k]
        nets += [(c[key]["net"] or 0) - (k[key]["net"] or 0) for key in c if key in k]
        diffs += d
        mean, lo, hi = _mean_ci(d)
        by[b] = {"mean": mean, "lo": lo, "hi": hi, "n": len(d)}
    mean, lo, hi = _mean_ci(diffs)
    return {"n": len(diffs), "mean": mean, "lo": lo, "hi": hi, "net": sum(nets) / len(nets) if nets else 0.0,
            "by_baseline": by, "sd": (math.sqrt(sum((x - mean) ** 2 for x in diffs) / (len(diffs) - 1))
                                      if len(diffs) > 1 else 0.5)}


def gate_verdict(paired: dict, h2h: list[dict]) -> tuple[str, str]:
    """BETTER / WORSE / UNCLEAR from the pooled paired difference and the head-to-head games."""
    w = sum(_points(r["result"]) for r in h2h)
    hlo, hhi = wilson(w, len(h2h)) if h2h else (0.0, 1.0)
    if paired["n"] and paired["hi"] < 0 or (h2h and hhi < 0.5):
        return "WORSE", "the champion does better" + (" against the baselines" if paired["n"] and paired["hi"] < 0 else " head to head")
    regressions = [b for b, d in paired["by_baseline"].items() if d["hi"] < 0]
    if (paired["n"] and paired["lo"] > 0 or (h2h and hlo > 0.5 and paired["mean"] >= 0)) and not regressions:
        return "BETTER", ("better against the baselines" if paired["n"] and paired["lo"] > 0 else "wins head to head, no worse elsewhere")
    if paired["n"] > 1 and abs(paired["mean"]) > 0.01:
        need = math.ceil((1.96 * max(paired["sd"], 0.2) / abs(paired["mean"])) ** 2)
        return "UNCLEAR", f"a difference of {paired['mean']:+.2f} a game needs about {need} pairs to show; this run had {paired['n']}"
    return "UNCLEAR", "no difference to see: play more games, or call them equal"


def compare_report(db, run_id: int, candidates: list[str], baselines: list[str]) -> str:
    """Points per baseline for each candidate, and each one's paired difference from the first."""
    out = [f"compare, run {run_id}: {', '.join(candidates)} (reference: {candidates[0]})"]
    rows = []
    for b in baselines:
        row = [b]
        for c in candidates:
            games = perspective(db, c, {"run": run_id, "vs": b})
            pts = sum(_points(r["result"]) for r in games)
            net = round(sum(r["net"] or 0 for r in games) / len(games)) if games else 0
            row.append(f"{pts:g}/{len(games)} ({net:+d})")
        rows.append(row)
    total = ["all"]
    for c in candidates:
        games = [r for b in baselines for r in perspective(db, c, {"run": run_id, "vs": b})]
        pts = sum(_points(r["result"]) for r in games)
        total.append(f"{pts:g}/{len(games)} ({100 * pts / max(1, len(games)):.0f}%)")
    rows.append(total)
    out.append(_table(rows, ["baseline (points, mean net)"] + candidates))
    for c in candidates[1:]:
        d = paired_difference(db, run_id, c, candidates[0], baselines)
        flag = "better" if d["lo"] > 0 else "worse" if d["hi"] < 0 else "no clear difference"
        out.append(f"{c} - {candidates[0]}: {d['mean']:+.2f} points a game (95% CI {d['lo']:+.2f} to {d['hi']:+.2f}), "
                   f"net {d['net']:+.0f}: {flag}")
    return "\n".join(out)


def curves(db, runs: list[int] | None = None, bots: list[str] | None = None, minutes=(3, 6, 9)) -> str:
    """The hands, not the result: each bot's army value, harvesters and assets at these minutes,
    averaged over its games (from the 5 s timeline)."""
    where, args = ["g.branch_of IS NULL"], []
    if runs:
        where.append(f"g.run_id IN ({','.join('?' * len(runs))})")
        args += runs
    if bots:
        where.append(f"s.bot IN ({','.join('?' * len(bots))})")
        args += bots
    cols = []
    for m in minutes:
        lo, hi = m * 60 * TPS, m * 60 * TPS + 124
        cols += [f"ROUND(AVG(CASE WHEN t.tick BETWEEN {lo} AND {hi} THEN t.army_value END)) AS army{m}",
                 f"ROUND(AVG(CASE WHEN t.tick BETWEEN {lo} AND {hi} THEN t.harvesters END), 1) AS harv{m}",
                 f"ROUND(AVG(CASE WHEN t.tick BETWEEN {lo} AND {hi} THEN t.assets_value END)) AS assets{m}"]
    rows = db.q(f"SELECT s.bot, COUNT(DISTINCT t.game_id) AS games, {', '.join(cols)} FROM timeline t "
                f"JOIN seats s ON s.game_id = t.game_id AND s.position = t.position JOIN games g ON g.id = t.game_id "
                f"WHERE {' AND '.join(where)} GROUP BY s.bot ORDER BY army{minutes[len(minutes) // 2]} DESC", *args)
    head = ["bot", "games"] + [f"{k}@{m}:00" for m in minutes for k in ("army", "harv", "assets")]
    return _table([[r["bot"][:40], r["games"]] + [r[f"{k}{m}"] for m in minutes for k in ("army", "harv", "assets")]
                   for r in rows], head) if rows else "(no timeline rows)"


def runs_table(db, limit: int = 20) -> str:
    rows = [[r["id"], _when(r["started"]), r["kind"], r["games"], r["status"], r["git_commit"] + ("+" if r["git_dirty"] else ""),
             (r["note"] or "")[:40], r["experiment"] or "", (r["command"] or "")[:60]]
            for r in db.q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", limit)]
    return _table(rows, ["run", "started", "kind", "games", "status", "commit", "note", "experiment", "command"]) if rows else "(no runs)"


def bots_table(db) -> str:
    table = registry.load()
    rows = []
    for name, b in table.items():
        n = db.one("SELECT COUNT(*) AS n FROM seats s JOIN games g ON g.id = s.game_id WHERE s.bot = ? AND g.branch_of IS NULL", name)["n"]
        rows.append([name, b.kind, b.version().split("@")[1], b.think if b.is_agent else "", n, ",".join(b.tags), b.description[:60]])
    return _table(rows, ["bot", "kind", "version", "think", "games", "tags", "description"])
