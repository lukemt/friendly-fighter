"""Print one simulated game as a transcript: .venv/bin/python -m bots.werewolf.harness.show SEED [--ours 3] [--mix rule]."""

from __future__ import annotations

import argparse

from .engine import Engine
from .run import layout, play


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--ours", type=int, default=2)
    ap.add_argument("--mix", default="rule")
    a = ap.parse_args()
    ours, kinds = layout(a.seed, a.ours, a.mix.split(","))
    eng, out, trace, errors = play(a.seed, ours, kinds, "ours", {}, __import__("bots.werewolf.harness.engine", fromlist=["Rules"]).Rules())
    print("roles:", {i: eng.roles[i] for i in range(7)}, "ours:", ours, "kinds:", kinds)
    for m in eng.messages:
        to = "all" if not m.recipients else f"->{m.recipients}"
        print(f"  d{m.day} P{m.sender} {to}: {m.content}")
    for d in eng.dead:
        print("  dead:", d)
    for h in eng.vote_history:
        print("  votes:", h)
    print("winner:", out.winner, out.reason, "day", out.days, "bot errors:", errors, "incidents:", trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
