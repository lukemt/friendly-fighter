"""Moment sets and probes: the funnel's first stage (docs/LAB_PLAN.md, Part 0 and L10).

A **moment** is a seat of a recorded lab game at a time: where an idea is tried. A **set** is a
named list of moments that share a situation (``late-rush``: the champion 2 minutes before the Rush
AI wiped it), collected by a finder or by hand:

- ``wiped``: the seat lost its base; the moment is ``--lead`` seconds (default 120) before the end;
- ``building-lost``: the seat's first lost building; ``--lead`` (default 30) before it;
- ``swing``: the seat's score (kills minus losses) fell by ``--drop`` (default 5000) within
  ``--window`` seconds (default 60); the moment is ``--lead`` (default 15) before the fall began;
- ``at``: every game at one time (``--at 13:00``), for situations that aren't events (standoffs).

A **probe** branches every moment of a set with each variant and with the control (the bot that
played there, which starts the branch with the same fresh memory as the variants), for
``--stop-after`` seconds, and compares each variant to the control moment by moment: the paired
difference in the score change since the moment is what decides. Branch games are ordinary lab
games (``games.branch_of``), labelled with their moment (``info.moment``) and set (``info.probe``).
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict

from .branch import TICKS_PER_SECOND, branch_jobs
from .views import clock

KINDS = ("wiped", "building-lost", "swing", "at")
DEFAULT_LEAD = {"wiped": 120.0, "building-lost": 30.0, "swing": 15.0, "at": 0.0}
BRANCHABLE = ("ai", "human", "platform")  # seat kinds a variant can't take over (the rest are our bots)


# -- finding moments -------------------------------------------------------------------------------


def _start(game) -> int:
    return (json.loads(game["info"] or "{}")).get("start_tick") or 0


def _candidates(db, f: dict) -> list:
    """(game, seat) rows of recorded, whole, traced games whose seat is one of our bots, by the filters."""
    conds = ["g.branch_of IS NULL", "g.trace IS NOT NULL", "g.outcome != 'no_result'",
             "g.termination != 'engine_crashed'", f"s.kind NOT IN ({','.join('?' * len(BRANCHABLE))})"]
    args: list = list(BRANCHABLE)
    if f.get("runs"):
        from .reuse import run_clause

        clause, more = run_clause("g", list(f["runs"]))
        conds.append(clause)
        args += more
    if f.get("bot"):
        conds.append("s.bot = ?")
        args.append(f["bot"])
    if f.get("vs"):
        conds.append("o.bot = ?")
        args.append(f["vs"])
    if f.get("map"):
        conds.append("g.map = ?")
        args.append(f["map"])
    return db.q("SELECT g.*, s.position AS seat, s.bot AS bot, s.result AS seat_result, o.bot AS opp "
                "FROM games g JOIN seats s ON s.game_id = g.id JOIN seats o ON o.game_id = g.id AND o.position != s.position "
                f"WHERE {' AND '.join(conds)} ORDER BY g.created", *args)


def _net(db, game_id: str, seat: int) -> list[tuple[float, int]]:
    rows = db.q("SELECT tick, kills_cost, deaths_cost FROM timeline WHERE game_id = ? AND position = ? ORDER BY tick",
                game_id, seat)
    return [(r["tick"], (r["kills_cost"] or 0) - (r["deaths_cost"] or 0)) for r in rows]


def find(db, kind: str, f: dict, *, lead: float | None = None, at: float | None = None, drop: int = 5000,
         window: float = 60.0, after: float | None = None, before: float | None = None) -> list[dict]:
    """Moments of this kind in the games matching ``f`` (``runs``, ``bot``, ``vs``, ``map``), one per
    game and seat at most; ``after``/``before`` keep moments in that span of game time (seconds)."""
    if kind not in KINDS:
        raise ValueError(f"no moment kind {kind!r} (kinds: {', '.join(KINDS)})")
    lead = DEFAULT_LEAD[kind] if lead is None else lead
    out = []
    for g in _candidates(db, f):
        start = _start(g)
        seconds, why = None, ""
        if kind == "wiped":
            if g["termination"] == "victory" and g["seat_result"] == "loss":
                seconds, why = g["game_seconds"] - lead, f"wiped at {clock(g['game_seconds'])}"
        elif kind == "building-lost":
            e = db.one("SELECT tick, type FROM events WHERE game_id = ? AND position = ? AND kind = 'building_lost' "
                       "AND tick >= ? ORDER BY tick LIMIT 1", g["id"], g["seat"],
                       start + int((after or 0) * TICKS_PER_SECOND))
            if e is not None:
                t = (e["tick"] - start) / TICKS_PER_SECOND
                seconds, why = t - lead, f"first {e['type']} lost at {clock(t)}"
        elif kind == "swing":
            series = _net(db, g["id"], g["seat"])
            for i, (tick, net) in enumerate(series):
                t = (tick - start) / TICKS_PER_SECOND
                if after is not None and t - lead < after:
                    continue
                span = [(k, n) for k, (tt, n) in enumerate(series[i:], i) if tt <= tick + window * TICKS_PER_SECOND]
                k_low, low = min(span, key=lambda x: x[1])
                if net - low >= drop:
                    # the fall begins at the last sample at the window's high before the low
                    top = max(n for k, n in span if k <= k_low)
                    k_top = max(k for k, n in span if k <= k_low and n == top)
                    t = (series[k_top][0] - start) / TICKS_PER_SECOND
                    seconds, why = t - lead, f"score fell {top - low:,} from {clock(t)} within {window:g} s"
                    break
        elif kind == "at":
            if at is None:
                raise ValueError("kind 'at' needs --at")
            if (g["game_seconds"] or 0) > at + 5:
                seconds, why = at - lead, f"at {clock(at)}"
        if seconds is None or seconds < 30:
            continue
        if (after is not None and seconds < after) or (before is not None and seconds > before):
            continue
        out.append({"game_id": g["id"], "seat": g["seat"], "seconds": round(seconds, 1), "kind": kind,
                    "why": f"{g['bot']} vs {g['opp']} on {g['map']}: {why}"})
    return out


# -- sets -----------------------------------------------------------------------------------------


def save(db, name: str, moments: list[dict]) -> int:
    """Add moments to set ``name`` (a moment already in it is kept once). Returns how many were new."""
    before = db.one("SELECT COUNT(*) AS n FROM moments WHERE set_name = ?", name)["n"]
    now = time.time()
    for m in moments:
        db.conn.execute("INSERT OR IGNORE INTO moments (set_name, game_id, seat, seconds, kind, why, created) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (name, m["game_id"], m["seat"], m["seconds"], m.get("kind") or "manual", m.get("why") or "", now))
    return db.one("SELECT COUNT(*) AS n FROM moments WHERE set_name = ?", name)["n"] - before


def moments(db, name: str) -> list:
    return db.q("SELECT * FROM moments WHERE set_name = ? ORDER BY id", name.lstrip("@"))


def sets(db) -> list:
    return db.q("SELECT set_name, COUNT(*) AS n, GROUP_CONCAT(DISTINCT kind) AS kinds, MIN(created) AS created "
                "FROM moments GROUP BY set_name ORDER BY created")


def drop(db, name: str, ids: list[int] | None = None) -> int:
    if ids:
        cur = db.conn.execute(f"DELETE FROM moments WHERE set_name = ? AND id IN ({','.join('?' * len(ids))})",
                              (name, *ids))
    else:
        cur = db.conn.execute("DELETE FROM moments WHERE set_name = ?", (name,))
    return cur.rowcount if cur is not None and cur.rowcount is not None else 0


# -- probes ---------------------------------------------------------------------------------------


def _versions() -> "callable":
    """A bot name's current version (lab/bots.json and its code), cached for one probe."""
    from . import bots as registry

    table, cache = registry.load(), {}

    def version(name: str) -> str | None:
        if name not in cache:
            try:
                cache[name] = registry.get(name, table).version()
            except Exception:  # noqa: BLE001 (an unregistered name never matches)
                cache[name] = None
        return cache[name]

    return version


def _engine() -> str:
    from . import bots as registry

    return registry._engine_hash()


def existing(db, job: dict, version, engine: str) -> str | None:
    """The game this branch job would play, if it was played already: the same moment, offset, bots
    (current versions), seed, stop rule and engine build. One moment with the same bots replays one
    way, so its result is the one recorded."""
    extra = job.get("extra") or {}
    rows = db.q("SELECT g.id FROM games g WHERE g.branch_of = ? AND g.branch_tick = ? AND g.seed = ? AND g.engine = ? "
                "AND g.outcome != 'no_result' AND g.branch_label = ? "
                "AND json_extract(g.info, '$.moment') = ? AND json_extract(g.info, '$.stop_after') IS ? "
                "AND json_extract(g.info, '$.stop_quiet') IS ? ORDER BY g.finished DESC",
                job["branch"]["of"], job["branch"]["tick"], job["seed"], engine, job["branch"]["label"],
                extra.get("moment"), extra.get("stop_after"), extra.get("stop_quiet"))
    for r in rows:
        seats = {s["position"]: s["version"] for s in db.q("SELECT position, version FROM seats WHERE game_id = ?", r["id"])}
        if all(seats.get(pos) == version(name) for pos, name in enumerate(job["bots"])):
            return r["id"]
    return None


def probe_jobs(db, name: str, variants: list[str], *, stop_after: float = 300.0, stop_quiet: float | None = None,
               spread: int = 1, seed: int = 1, control: bool = True, limit: int | None = None,
               moment_ids: list[int] | None = None, reuse: bool = True) -> tuple[list[dict], int]:
    """Branch jobs for the set's moments (``moment_ids``, else all, else the first ``limit``): the
    control and each variant, at ``spread`` moments 2 s apart (one moment with the same bots replays
    one way). With ``reuse``, a branch already played the same way is left out. Returns the jobs and
    how many were reused."""
    rows = moments(db, name)
    if moment_ids is not None:
        rows = [m for m in rows if m["id"] in set(moment_ids)]
    elif limit:
        rows = rows[:limit]
    if not rows:
        raise ValueError(f"no moments in set {name!r} (lab moments list)")
    version, engine = (_versions(), _engine()) if reuse else (None, None)
    jobs, reused = [], 0
    for m in rows:
        for k in range(max(1, spread)):
            for j in branch_jobs(db, m["game_id"], m["seconds"] + 2.0 * k, variants, seat=m["seat"], repeats=1,
                                 seed=seed + k, stop_after=stop_after, control=control):
                j["extra"] = {**j.get("extra", {}), "moment": m["id"], "probe": m["set_name"], "offset": 2.0 * k,
                              "stop_after": stop_after, "stop_quiet": stop_quiet}
                j["label"] = f"probe:{m['set_name']}:{j['branch']['label']}"
                if reuse and existing(db, j, version, engine):
                    reused += 1
                    continue
                jobs.append(j)
    return jobs, reused


def human_job(db, moment_id: int, who: str, *, stop_after: float = 300.0, stop_quiet: float | None = None,
              seed: int = 1) -> tuple[dict, dict]:
    """A moment played by hand (``lab human start --moment``): the branch a probe would play there,
    with the person (``human-<who>``) as the variant, the same seat, opponent, seed and stop rule, so
    the probe report pairs it with the control. Returns that job and the control's."""
    m = db.one("SELECT * FROM moments WHERE id = ?", moment_id)
    if m is None:
        raise ValueError(f"no moment {moment_id} (lab moments show SET)")
    jobs, _ = probe_jobs(db, m["set_name"], [f"human-{who}"], stop_after=stop_after, stop_quiet=stop_quiet,
                         seed=seed, moment_ids=[moment_id], reuse=False)
    control = next(j for j in jobs if j["branch"]["label"].startswith("control:"))
    mine = next(j for j in jobs if j is not control)
    return mine, control


def halving_order(db, name: str, seed: int = 1) -> list[int]:
    """The set's moments in a fixed shuffled order (so the first rounds are not one map or one bot)."""
    import random

    ids = [m["id"] for m in moments(db, name)]
    random.Random(seed).shuffle(ids)
    return ids


def survivors(stats: dict[str, dict], keep: float = 0.5) -> list[str]:
    """Successive halving: the better half by mean paired difference to the control (at least one),
    minus any variant already clearly WORSE."""
    ranked = sorted(stats, key=lambda v: -stats[v]["mean"])
    n = max(1, math.ceil(len(ranked) * keep))
    alive = [v for v in ranked[:n] if stats[v]["verdict"] != "WORSE"]
    return alive or ranked[:1]


MIN_PAIRS = 3  # fewer pairs give no verdict (two equal differences would make a zero-width interval)
T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18,
       15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04}


def _t95(df: int) -> float:
    if df <= 0:
        return float("inf")
    keys = [k for k in sorted(T95) if k <= df]
    return T95[keys[-1]] if df <= 30 else 1.96


def mean_ci(xs: list[float]) -> tuple[float, float, float]:
    """Mean and a 95% t-interval."""
    n = len(xs)
    if not n:
        return 0.0, float("-inf"), float("inf")
    mean = sum(xs) / n
    if n < 2:
        return mean, float("-inf"), float("inf")
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    half = _t95(n - 1) * sd / math.sqrt(n)
    return mean, mean - half, mean + half


def outcomes(db, name: str, runs: list[int] | None = None, *, stop_after: float | None = None,
             stop_quiet: float | None = None, moment_ids: list[int] | None = None, current: bool = True,
             counts: dict | None = None) -> dict:
    """Every branch played from the set's moments with this stop rule: ``{(moment, offset): {label:
    outcome}}``, an outcome being the branched seat's score change since the moment, its result and
    whether it was wiped. With ``current``, only branches whose bots are at their current versions
    count (a changed bot is another variant); the same branch played twice is one game (one moment
    replays one way): the latest is used. ``stop_after`` None: any stop (then the latest wins).
    ``counts`` (a dict) gets ``stale``: the branches left out for older versions."""
    name = name.lstrip("@")
    ids = {m["id"]: m for m in moments(db, name)}
    if moment_ids is not None:
        ids = {k: v for k, v in ids.items() if k in set(moment_ids)}
    conds = ["json_extract(g.info, '$.probe') = ?"]
    args: list = [name]
    if runs:
        conds.append(f"g.run_id IN ({','.join('?' * len(runs))})")
        args += runs
    if stop_after is not None:
        conds += ["json_extract(g.info, '$.stop_after') = ?", "json_extract(g.info, '$.stop_quiet') IS ?"]
        args += [stop_after, stop_quiet]
    version = _versions() if current else None
    out: dict = defaultdict(dict)
    stale = 0
    for g in db.q(f"SELECT g.* FROM games g WHERE {' AND '.join(conds)} ORDER BY g.finished", *args):
        info = json.loads(g["info"] or "{}")
        mid = info.get("moment")
        if mid not in ids:
            continue
        seat = info.get("branch_seat", 0)
        seats = {r["position"]: r for r in db.q("SELECT * FROM seats WHERE game_id = ?", g["id"])}
        s = seats.get(seat)
        series = _net(db, g["id"], seat)
        if s is None or not series:
            continue
        if version is not None and any(r["version"] != version(r["bot"]) for r in seats.values() if r["kind"] != "human"):
            stale += 1
            continue
        label = g["branch_label"] or ""
        out[(mid, info.get("offset", 0.0))][label.removeprefix("control:")] = {
            "control": label.startswith("control:"), "delta": series[-1][1] - series[0][1], "result": s["result"],
            "wiped": g["termination"] == "victory" and s["result"] == "loss", "game": g["id"],
            "seconds": (g["game_seconds"] or 0) - ((g["branch_tick"] or 0) - _start(g)) / TICKS_PER_SECOND}
    if counts is not None:
        counts["stale"] = stale
    return dict(out)


def variant_stats(data: dict) -> dict[str, dict]:
    """Per variant: its paired differences to the control at the same branch points, and the verdict."""
    ctl = {key: next(o for o in per.values() if o["control"]) for key, per in data.items()
           if any(o["control"] for o in per.values())}
    out: dict[str, dict] = {}
    for key, per in data.items():
        for label, o in per.items():
            if o["control"] or key not in ctl:
                continue
            out.setdefault(label, {"diffs": []})["diffs"].append(o["delta"] - ctl[key]["delta"])
    for st in out.values():
        m, lo, hi = mean_ci(st["diffs"])
        st.update(mean=m, lo=lo, hi=hi, n=len(st["diffs"]),
                  verdict=("too few" if len(st["diffs"]) < MIN_PAIRS else
                           "BETTER" if lo > 0 else "WORSE" if hi < 0 else "unclear"))
    return out


def probe_report(db, name: str, runs: list[int] | None = None, *, stop_after: float | None = None,
                 stop_quiet: float | None = None, moment_ids: list[int] | None = None, dropped: dict | None = None) -> str:
    """The probe's table: per variant the score change since the moment, the paired difference to the
    control with its verdict, in how many moments it did better, wipes, W-D-L, and how long branches
    lasted. ``dropped``: variants successive halving left out, and in which round."""
    from .report import _table

    name = name.lstrip("@")
    counts: dict = {}
    data = outcomes(db, name, runs, stop_after=stop_after, stop_quiet=stop_quiet, moment_ids=moment_ids, counts=counts)
    if not data:
        return f"no probe branches of set {name!r} yet" + (" with this stop rule" if stop_after is not None else "")
    stats = variant_stats(data)
    control_outs = [o for per in data.values() for o in per.values() if o["control"]]
    control_bots = sorted({lb for per in data.values() for lb, o in per.items() if o["control"]})

    def row(label: str, outs: list[dict], st: dict | None) -> list:
        w = sum(1 for o in outs if o["result"] == "win")
        d = sum(1 for o in outs if o["result"] == "draw")
        mean_d = round(sum(o["delta"] for o in outs) / len(outs)) if outs else "-"
        secs = round(sum(o["seconds"] for o in outs) / len(outs)) if outs else "-"
        if st is None:
            vs, better = "", ""
        elif st["n"] < MIN_PAIRS:
            vs, better = f"{st['mean']:+,.0f} (too few: {st['n']} < {MIN_PAIRS})", f"{sum(1 for x in st['diffs'] if x > 0)}/{st['n']}"
        else:
            vs = f"{st['mean']:+,.0f} ({st['lo']:+,.0f}..{st['hi']:+,.0f}) {st['verdict']}"
            better = f"{sum(1 for x in st['diffs'] if x > 0)}/{st['n']}"
        return [label[:40], len(outs), mean_d, vs, better, sum(1 for o in outs if o["wiped"]),
                f"{w}-{d}-{len(outs) - w - d}", secs]

    rows = [row("control", control_outs, None)] if control_outs else []
    for label in sorted(stats, key=lambda v: -stats[v]["mean"]):
        outs = [per[label] for per in data.values() if label in per and not per[label]["control"]]
        rows.append(row(label, outs, stats[label]))
    rule = (f", stop after {stop_after:g} s" if stop_after is not None else "") + \
           (f" or {stop_quiet:g} s quiet" if stop_quiet is not None else "")
    out = [f"probe {name!r}: {len({mid for mid, _ in data})} moments, {len(data)} branch points{rule}"
           + (f" (runs {', '.join(map(str, runs))})" if runs else ""),
           _table(rows, ["variant", "branches", "score change", "vs control (95% CI)", "better in", "wiped", "W-D-L",
                         "lasted s"]),
           f"control: the bot that played each moment ({', '.join(control_bots)})"]
    if dropped:
        out.append("dropped by halving: " + ", ".join(f"{v} (round {r})" for v, r in dropped.items()))
    if counts.get("stale"):
        out.append(f"({counts['stale']} branch(es) of older bot versions left out)")
    out.append("(score change: the branched seat's kills minus losses since the moment, mean; vs control: the paired\n"
               " difference at the same moments, a verdict from 3 pairs on; W-D-L: at the stop, on the tiebreak, or the\n"
               " end of the game)")
    return "\n".join(out)
