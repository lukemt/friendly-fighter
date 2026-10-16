"""Compare HoK team policies on the abstract sim (relative results only).

    python -m bots.hok.sim.compare --a ff-hok --b upstream,advance,button0 --games 20 --seed 2026
    python -m bots.hok.sim.compare --a 'ff-hok:{"min_pushers": 2}' --b ff-hok --games 40 --mode both

Each game is played twice per seed with the sides swapped (A as camp 1, then
as camp 2), so map side never favours one policy. A's score per game: win 1,
draw 0.5, loss 0. Reported with a 95% interval (normal approximation on the
per-seed pair means, which keeps the side swap paired).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from .baselines import make_agent
from .engine import SimConfig
from .match import play_match


def _one(args: tuple[str, str, int, str, int, dict]) -> dict[str, Any]:
    a, b, seed, mode, a_camp, cfg = args
    specs = {a_camp: a, 3 - a_camp: b}
    config = SimConfig(**cfg)
    t0 = time.time()
    r = play_match(lambda clock, sleep, camp: make_agent(specs[camp], clock, sleep), names=specs, seed=seed,
                   mode=mode, config=config)
    sa, sb = r.sides[a_camp], r.sides[3 - a_camp]
    return {
        "a": a, "b": b, "seed": seed, "mode": mode, "a_camp": a_camp, "score": r.score(a_camp),
        "termination": r.termination, "steps": r.steps,
        "dmg_a": r.structure_damage[a_camp], "dmg_b": r.structure_damage[3 - a_camp],
        "kills_a": r.kills[a_camp], "kills_b": r.kills[3 - a_camp],
        "illegal_a": sa.illegal, "crashes_a": sa.crashes, "illegal_b": sb.illegal, "crashes_b": sb.crashes,
        "sends_a": sa.sends, "think_ms_a": sa.think_ms / max(1, sa.calls), "think_max_ms_a": sa.think_max_ms,
        "secs": time.time() - t0,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_seed: dict[tuple, list[float]] = {}
    for r in rows:
        by_seed.setdefault((r["seed"], r["mode"]), []).append(r["score"])
    pair_means = [statistics.mean(v) for v in by_seed.values()]
    mean = statistics.mean(r["score"] for r in rows)
    half = 1.96 * statistics.stdev(pair_means) / math.sqrt(len(pair_means)) if len(pair_means) > 1 else float("nan")
    return {
        "games": len(rows), "wins": sum(r["score"] == 1.0 for r in rows), "draws": sum(r["score"] == 0.5 for r in rows),
        "losses": sum(r["score"] == 0.0 for r in rows), "score": mean, "ci": (mean - half, mean + half),
        "crystal": sum(r["termination"] == "crystal_destroyed" for r in rows),
        "dmg_a": statistics.mean(r["dmg_a"] for r in rows), "dmg_b": statistics.mean(r["dmg_b"] for r in rows),
        "kills_a": statistics.mean(r["kills_a"] for r in rows), "kills_b": statistics.mean(r["kills_b"] for r in rows),
        "illegal_a": sum(r["illegal_a"] for r in rows), "crashes_a": sum(r["crashes_a"] for r in rows),
        "illegal_b": sum(r["illegal_b"] for r in rows), "crashes_b": sum(r["crashes_b"] for r in rows),
        "think_ms_a": statistics.mean(r["think_ms_a"] for r in rows),
        "think_max_ms_a": max(r["think_max_ms_a"] for r in rows),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", default="ff-hok", help="the policy under test")
    ap.add_argument("--b", default="upstream,advance,button0", help="comma-separated opponents")
    ap.add_argument("--games", type=int, default=10, help="seeds per opponent (each played from both sides)")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--mode", default="realtime", choices=["realtime", "lockstep", "both"])
    ap.add_argument("--config", default="{}", help="SimConfig overrides as JSON")
    ap.add_argument("--workers", type=int, default=max(1, min(2, os.cpu_count() or 1)))
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--json", default=None, help="also write every game's row to this file")
    args = ap.parse_args(argv)
    cfg = json.loads(args.config)
    if args.max_steps:
        cfg["max_steps"] = args.max_steps
    modes = ["realtime", "lockstep"] if args.mode == "both" else [args.mode]
    opponents = [b for b in _split(args.b) if b]
    jobs = [(args.a, b, args.seed + i, mode, a_camp, cfg)
            for b in opponents for mode in modes for i in range(args.games) for a_camp in (1, 2)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(_one, jobs))
    print(f"A = {args.a}   seeds {args.seed}..{args.seed + args.games - 1}, both sides, config {cfg or 'default'}"
          f"   ({len(rows)} games, {time.time() - t0:.0f}s)")
    print(f"{'opponent':28s} {'mode':9s} {'games':>5s} {'W-D-L':>9s} {'score':>6s} {'95% CI':>15s} "
          f"{'crystal':>7s} {'struct dmg A/B':>15s} {'kills A/B':>11s} {'illegal/crash A':>15s} {'B':>7s} {'ms avg/max':>11s}")
    for b in opponents:
        for mode in modes:
            sel = [r for r in rows if r["b"] == b and r["mode"] == mode]
            s = summarize(sel)
            print(f"{b:28.28s} {mode:9s} {s['games']:5d} {s['wins']:3d}-{s['draws']:d}-{s['losses']:<3d} "
                  f"{s['score']:6.2f} [{s['ci'][0]:5.2f},{s['ci'][1]:5.2f}] {s['crystal']:7d} "
                  f"{s['dmg_a']:7.0f}/{s['dmg_b']:<7.0f} {s['kills_a']:5.1f}/{s['kills_b']:<5.1f} "
                  f"{s['illegal_a']:7d}/{s['crashes_a']:<7d} {s['illegal_b']:3d}/{s['crashes_b']:<3d} "
                  f"{s['think_ms_a']:5.2f}/{s['think_max_ms_a']:<5.1f}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f)
    return 0


def _split(text: str) -> list[str]:
    """Split on commas outside braces (knob JSON has commas)."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


if __name__ == "__main__":
    raise SystemExit(main())
