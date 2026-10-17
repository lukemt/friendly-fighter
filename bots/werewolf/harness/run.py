"""Play many simulated games and report win rates per role.

    .venv/bin/python -m bots.werewolf.harness.run --ours 1 --mix rule --games 400 --seed0 1000
    .venv/bin/python -m bots.werewolf.harness.run --ours 2 --mix first,random,rule --games 400 --ref first

For each game seed the engine deals the roles, and ``--ours`` seats (random, from the seed) are played
by our bot; the other seats draw from ``--mix``. With ``--ref`` the same game (same roles, seats and
opponents) is played again with the reference player in our seats, so the table shows the win rate
with our bot, with the reference, and the paired difference with its 95% interval, per role of our
seat. "Win" is the team win of that seat's side.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from collections import defaultdict

from bots.werewolf import create_agent
from bots.werewolf.policy import Settings

from .engine import ROLE_DECK, SEER, VILLAGER, WOLF, Engine, Rules
from .players import make_player


def parse_settings(text: str) -> dict:
    out: dict = {}
    for part in filter(None, text.split(",")):
        k, v = part.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def layout(seed: int, n_ours: int, mix: list[str]) -> tuple[list[int], list[str]]:
    rng = random.Random(f"layout:{seed}")
    ours = sorted(rng.sample(range(7), n_ours))
    kinds = [rng.choice(mix) for _ in range(7)]
    return ours, kinds


def play(seed: int, ours: list[int], kinds: list[str], ours_kind: str, settings: dict, rules: Rules):
    agents: dict[int, object] = {}

    def factory(seat: int, role: str, rng: random.Random):
        if seat in ours:
            if ours_kind == "ours":
                agent = create_agent(settings=settings, log=lambda line: None)
            else:
                agent = make_player(ours_kind, seat, rng)
        else:
            agent = make_player(kinds[seat], seat, rng)
        agents[seat] = agent
        return agent

    trace: list = []
    eng = Engine(seed, factory, rules=rules, trace=trace)
    out = eng.play()
    errors = sum(len(getattr(agents[s], "errors", [])) for s in ours)
    return eng, out, trace, errors


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = wins / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ours", type=int, default=1, help="how many seats our bot plays (1-3)")
    ap.add_argument("--mix", default="rule", help="opponent kinds, comma separated: first,random,rule")
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--ref", default="", help="also play the same games with this player in our seats (first|random|rule)")
    ap.add_argument("--set", default="", help="bot settings, e.g. talk=false,preempt_claim=0.3")
    ap.add_argument("--night-resets", action="store_true", help="a night kill also resets the 3-day stall counter")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    mix = a.mix.split(",")
    rules = Rules(night_kill_resets_stall=a.night_resets)
    settings = parse_settings(a.set)
    Settings(**settings)  # fail early on a typo
    runs = {"ours": "ours"} | ({"ref": a.ref} if a.ref else {})
    # results[kind][role] -> list of (game, win)
    results: dict[str, dict[str, list[tuple[int, int]]]] = {k: defaultdict(list) for k in runs}
    reasons: dict[str, dict[str, int]] = {k: defaultdict(int) for k in runs}
    problems = defaultdict(int)
    days = defaultdict(list)
    started = time.time()
    for g in range(a.games):
        seed = a.seed0 + g
        ours, kinds = layout(seed, a.ours, mix)
        for name, kind in runs.items():
            eng, out, trace, errors = play(seed, ours, kinds, kind, settings, rules)
            for s in ours:
                role = eng.roles[s]
                results[name][role].append((g, int(out.winner == (WOLF if role == WOLF else VILLAGER))))
            reasons[name][f"{out.winner}:{out.reason}"] += 1
            days[name].append(out.days)
            problems[f"{name} bot errors"] += errors
            problems[f"{name} engine incidents"] += len(trace)
        if not a.quiet and (g + 1) % 100 == 0:
            print(f"  ... {g + 1}/{a.games} games, {time.time() - started:.0f}s", file=sys.stderr)

    print(f"\nours={a.ours} seats, opponents drawn from {mix}, {a.games} games (seeds {a.seed0}..{a.seed0 + a.games - 1}), "
          f"settings {settings or 'default'}, night kill resets stall: {a.night_resets}")
    header = f"{'role of our seat':<18}" + "".join(f"{n + ' win rate [95% CI] (seats)':<40}" for n in runs)
    if a.ref:
        header += "paired difference [95% CI]"
    print(header)
    for role in (WOLF, SEER, VILLAGER, "all"):
        row = f"{role:<18}"
        cells = {}
        for name in runs:
            rows = [r for k, v in results[name].items() if role in ("all", k) for r in v]
            n, w = len(rows), sum(x[1] for x in rows)
            lo, hi = wilson(w, n)
            cells[name] = rows
            row += f"{(100 * w / n if n else 0):5.1f}%  [{100 * lo:4.1f}, {100 * hi:4.1f}]  n={n:<5}".ljust(40)
        if a.ref:
            ours_rows, ref_rows = cells["ours"], cells["ref"]
            diffs = [x[1] - y[1] for x, y in zip(ours_rows, ref_rows)]
            if diffs:
                m = sum(diffs) / len(diffs)
                var = sum((d - m) ** 2 for d in diffs) / max(1, len(diffs) - 1)
                se = math.sqrt(var / len(diffs))
                row += f"{100 * m:+5.1f} pts  [{100 * (m - 1.96 * se):+5.1f}, {100 * (m + 1.96 * se):+5.1f}]"
        print(row)
    for name in runs:
        print(f"\n{name}: how games ended: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons[name].items())) +
              f"; mean day {sum(days[name]) / len(days[name]):.2f}")
    bad = {k: v for k, v in problems.items() if v}
    if bad:
        print("problems:", bad)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
