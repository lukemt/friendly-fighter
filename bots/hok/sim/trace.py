"""One sim match with a look inside: where our heroes' time went, and what happened.

    python -m bots.hok.sim.trace --a ff-hok --b upstream --seed 2026 --mode lockstep
"""

from __future__ import annotations

import argparse
import json

from .baselines import make_agent
from .engine import Sim, SimConfig
from .match import play_match


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--a", default="ff-hok")
    ap.add_argument("--b", default="upstream")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--mode", default="realtime")
    ap.add_argument("--camp", type=int, default=1, help="A's camp")
    ap.add_argument("--max-steps", type=int, default=3000)
    ap.add_argument("--every", type=int, default=500, help="print hero positions every N steps")
    args = ap.parse_args(argv)
    specs = {args.camp: args.a, 3 - args.camp: args.b}
    snapshots: list[str] = []
    orig = Sim.step

    def step(self: Sim) -> None:
        orig(self)
        if self.step_no % args.every == 0:
            for camp in (1, 2):
                hs = [h for h in self.heroes if h.camp == camp]
                snapshots.append(f"  step {self.step_no} camp {camp} ({specs[camp]}): " + " ".join(
                    f"[{h.x / 1000:.0f},{h.z / 1000:.0f} {h.hp / h.max_hp:.0%} L{h.level} "
                    f"{(h.order or '-').replace('macro_', '')}]" for h in hs))
        if self.termination:
            snapshots.extend(f"  {t:6.1f}s {e}" for t, e in self.events if "destroyed" in e)
    Sim.step = step  # type: ignore[method-assign]
    try:
        r = play_match(lambda clock, sleep, camp: make_agent(specs[camp], clock, sleep), names=specs,
                       seed=args.seed, mode=args.mode, config=SimConfig(max_steps=args.max_steps))
    finally:
        Sim.step = orig  # type: ignore[method-assign]
    print(f"winner camp {r.winner} ({r.termination}); structure damage {r.structure_damage}; kills {r.kills}")
    for line in snapshots:
        print(line)
    for camp in (1, 2):
        agent = r.sides[camp].agent
        reasons = getattr(agent, "reasons", None)
        if reasons:
            total = sum(reasons.values())
            print(f"camp {camp} reasons: " + json.dumps({k: f"{v / total:.0%}" for k, v in reasons.most_common(12)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
