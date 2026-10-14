"""Which games to play: leagues (every pair of a set of bots, evenly), matchups, and gates.

- ``league``: the pairs that have the fewest games so far come first; maps rotate; each pair
  swaps seats every other game; seeds come from the run, so a league can be played again;
- ``matchup``: N games of A against B (maps rotating, seats swapped);
- ``gate``: is a candidate better than the champion? It plays the champion, and both play every
  baseline on the same seeds (paired: the same maps, seats and seeds for both), so the comparison
  against the baselines is as fair as it can be with few games.
"""

from __future__ import annotations

import itertools
import random

MAPS = ["trapped", "sidestep", "shadowfiend-ii"]


def _job(a: str, b: str, *, map_id: str, seed: int, minutes: int, label: str, swap: bool = False) -> dict:
    return {"bots": [b, a] if swap else [a, b], "map": map_id, "seed": seed, "minutes": minutes, "label": label}


def matchup(a: str, b: str, games: int, *, maps: list[str] = MAPS, seed: int = 1, minutes: int = 10,
            label: str = "") -> list[dict]:
    return [_job(a, b, map_id=maps[i % len(maps)], seed=seed * 10007 + i, minutes=minutes,
                 label=label or f"{a} vs {b}", swap=bool(i % 2)) for i in range(games)]


def league(bots: list[str], games: int, *, kinds: dict[str, str], maps: list[str] = MAPS, seed: int = 1,
           minutes: int = 10, played: dict[tuple[str, str], int] | None = None, label: str = "league") -> list[dict]:
    """``kinds``: each bot's kind (two OpenRA AIs can't play each other here: the lab steps the
    engine for agents). ``played``: games each pair already has (they are evened out)."""
    pairs = [tuple(sorted(p)) for p in itertools.combinations(sorted(set(bots)), 2)
             if not (kinds.get(p[0]) == "ai" and kinds.get(p[1]) == "ai")]
    if not pairs:
        raise ValueError("no pair of these bots can play (at least one of each pair must be an agent)")
    count = {p: (played or {}).get(p, 0) for p in pairs}
    index = {p: i for i, p in enumerate(pairs)}
    rng = random.Random(seed)
    jobs = []
    for i in range(games):
        low = min(count.values())
        pair = rng.choice([p for p in pairs if count[p] == low])
        k = count[pair]
        count[pair] += 1
        # Each pair goes round the maps, starting from a different one: every map gets its share.
        jobs.append(_job(pair[0], pair[1], map_id=maps[(k + index[pair]) % len(maps)], seed=seed * 10007 + i,
                         minutes=minutes, label=label, swap=bool(k % 2)))
    return jobs


def gate(candidate: str, champion: str, baselines: list[str], games: int, *, maps: list[str] = MAPS, seed: int = 1,
         minutes: int = 10) -> list[dict]:
    """``games`` against the champion, and ``games`` each for candidate and champion against every baseline
    (the same seeds for both)."""
    jobs = [_job(candidate, champion, map_id=maps[i % len(maps)], seed=seed * 10007 + i, minutes=minutes,
                 label=f"gate:{candidate}:h2h", swap=bool(i % 2)) for i in range(games)]
    for k, base in enumerate(baselines):
        for i in range(games):
            s = seed * 10007 + 1000 * (k + 1) + i
            m = maps[i % len(maps)]
            for who in (candidate, champion):
                jobs.append(_job(who, base, map_id=m, seed=s, minutes=minutes, label=f"gate:{candidate}:{base}",
                                 swap=bool(i % 2)))
    return jobs
