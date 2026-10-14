"""Compare two batches of local games (``scripts/ffsim.py run`` result folders), e.g. the same
matchup in real time and in virtual time, or an A/B of two agent variants.

    python -m ffsim.compare_runs sim/results/<run A> sim/results/<run B>

Per run: outcomes of the agent seats (win/draw/loss for the first agent label), the
tiebreak values and military stats at the end, and from the runtimes' recordings the
timing in ticks (states and batches per game minute, lag shares). Means with their
standard error, so a difference can be judged against the noise.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from .timing import profile, read_records

ROOT = Path(__file__).resolve().parent.parent


def _games(run: Path) -> list[dict]:
    out = []
    for f in sorted(run.glob("sim-*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except ValueError:
            continue
    return out


def _agent_seats(game: dict) -> list[dict]:
    return [s for s in game.get("seats") or [] if s.get("kind") == "agent"]


def collect(run: Path) -> dict[str, list[float]]:
    m: dict[str, list[float]] = {}

    def add(key: str, value) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            m.setdefault(key, []).append(float(value))

    for g in _games(run):
        res = g.get("result") or {}
        winner = res.get("winner_seat")
        add("game_seconds", g.get("game_seconds"))
        add("wall_seconds", g.get("wall_seconds"))
        for s in _agent_seats(g):
            add("points", 1.0 if winner == s["seat"] else 0.5 if res.get("reason") == "draw" or (winner is None and res.get("outcome") != "no_result") else 0.0)
            mil = s.get("military") or {}
            for k in ("kills_cost", "deaths_cost", "assets_value", "army_value", "units_killed", "units_lost",
                      "buildings_killed", "buildings_lost"):
                add(k, mil.get(k))
            tb = s.get("tiebreak")
            if tb:
                add("tiebreak_net", tb[0])
            rec = ROOT / "recordings" / g["session_id"]
            seat_file = next(iter(sorted(rec.glob(f"seat{s['seat'][-1]}.jsonl*"))), None) if rec.exists() else None
            if seat_file is not None:
                p = profile(read_records(seat_file))
                add("states_per_min", p.get("states_per_min"))
                add("batches_per_min", p.get("batches_per_min"))
                for lag, share in (p.get("lag_share") or {}).items():
                    add(f"lag_{lag}_share", share)
                if p.get("lag"):
                    add("lag_mean", p["lag"]["mean"])
    return m


def _stat(xs: list[float]) -> tuple[float, float, int]:
    n = len(xs)
    mean = sum(xs) / n
    se = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1) / n) if n > 1 else float("nan")
    return mean, se, n


def main(argv: list[str]) -> int:
    a, b = (collect(Path(x)) for x in argv[:2])
    print(f"{'metric':18} {'A mean':>10} {'± se':>8} {'n':>3}   {'B mean':>10} {'± se':>8} {'n':>3}   {'B-A':>9}  {'|z|':>5}")
    for key in sorted(set(a) | set(b)):
        if key not in a or key not in b:
            continue
        (ma, sa, na), (mb, sb, nb) = _stat(a[key]), _stat(b[key])
        se = math.sqrt((sa if sa == sa else 0) ** 2 + (sb if sb == sb else 0) ** 2)
        z = abs(mb - ma) / se if se > 0 else float("nan")
        print(f"{key:18} {ma:10.2f} {sa:8.2f} {na:3d}   {mb:10.2f} {sb:8.2f} {nb:3d}   {mb - ma:9.2f}  {z:5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
