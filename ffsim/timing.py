"""Timing profile of a game, in game ticks, from a seat recording (agent/recorder.py).

    python -m ffsim.timing recordings/<session>[/seat0.jsonl.gz] [more sessions...]

What it measures is what decides how a game plays out, and it is the same whether the game
ran on the platform, on the local platform in real time, or in virtual time:

- ``view_gap``: ticks between consecutive states the agent decided on (6 = it saw every view);
- ``lag``: a batch's arrival tick (``play_action``'s ``state_version``) minus the tick of the
  state it was decided on;
- ``batch_gap``: ticks between consecutive accepted batches;
- ``states_per_min`` / ``batches_per_min``: per game minute (1500 ticks);
- ``wall_*``: the same steps in wall seconds (only meaningful in real time).
"""

from __future__ import annotations

import gzip
import json
import statistics
import sys
from pathlib import Path


def _open(path: Path):
    return gzip.open(path, "rt") if path.suffix == ".gz" else open(path)


def read_records(path: Path) -> list[dict]:
    out = []
    try:
        with _open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except EOFError:  # a recording still being written
        pass
    return out


def profile(records: list[dict]) -> dict:
    svs, state_t = [], []
    lags, arrivals, wall_answer, wall_decide = [], [], [], []
    last_sv = last_state_t = last_action_t = None
    playing = False
    for r in records:
        kind = r.get("kind")
        if kind == "state":
            raw = r.get("raw") or {}
            if raw.get("phase") != "playing":
                playing = playing and raw.get("phase") != "finished"
                continue
            playing = True
            sv = raw.get("state_version")
            if sv is None:
                continue
            if last_sv is None or sv != last_sv:
                svs.append(sv)
                state_t.append(r["t"])
            last_sv, last_state_t = sv, r["t"]
        elif kind == "action" and playing:
            last_action_t = r["t"]
            if last_state_t is not None:
                wall_decide.append(r["t"] - last_state_t)
        elif kind == "result" and playing:
            res = r.get("result") or {}
            if res.get("accepted") and isinstance(res.get("state_version"), int) and last_sv is not None:
                arrivals.append(res["state_version"])
                lags.append(res["state_version"] - last_sv)
                if last_action_t is not None:
                    wall_answer.append(r["t"] - last_action_t)
    span = (svs[-1] - svs[0]) if len(svs) > 1 else 0
    minutes = span / 1500 if span else 0
    return {
        "ticks": span,
        "view_gap": _dist([b - a for a, b in zip(svs, svs[1:])]),
        "lag": _dist(lags),
        "lag_share": _shares(lags),
        "batch_gap": _dist([b - a for a, b in zip(arrivals, arrivals[1:])]),
        "states_per_min": round(len(svs) / minutes, 1) if minutes else None,
        "batches_per_min": round(len(arrivals) / minutes, 1) if minutes else None,
        "wall_view_gap": _dist([b - a for a, b in zip(state_t, state_t[1:])], 3),
        "wall_decide": _dist(wall_decide, 4),
        "wall_answer": _dist(wall_answer, 3),
    }


def _shares(xs: list) -> dict:
    """The share of each value (lags are 0, 6, 12... ticks)."""
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return {k: round(v / len(xs), 2) for k, v in sorted(out.items()) if v / len(xs) >= 0.01}


def _dist(xs: list, digits: int = 1) -> dict | None:
    if not xs:
        return None
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"n": len(xs), "mean": round(statistics.fmean(xs), digits), "p10": round(q(0.1), digits),
            "p50": round(q(0.5), digits), "p90": round(q(0.9), digits)}


def seat_files(arg: str) -> list[Path]:
    p = Path(arg)
    if p.is_dir():
        return sorted(p.glob("seat*.jsonl*"))
    return [p]


def main(argv: list[str]) -> int:
    for arg in argv:
        for f in seat_files(arg):
            prof = profile(read_records(f))
            print(f"{f.parent.name[:13]}/{f.name.split('.')[0]}: {prof['ticks']} ticks, "
                  f"{prof['states_per_min']} states/min, {prof['batches_per_min']} batches/min")
            for k in ("view_gap", "lag", "batch_gap", "wall_view_gap", "wall_decide", "wall_answer"):
                d = prof[k]
                if d:
                    print(f"    {k:14} n={d['n']:<5} mean={d['mean']:<8} p10={d['p10']:<7} p50={d['p50']:<7} p90={d['p90']}")
            print(f"    lag shares     {prof['lag_share']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
