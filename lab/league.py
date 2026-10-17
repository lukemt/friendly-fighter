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


def matchup(a: str, b: str, games: int, *, maps: list[str] = MAPS, seed: int = 1, minutes: int = 20,
            label: str = "") -> list[dict]:
    return [_job(a, b, map_id=maps[i % len(maps)], seed=game_seed(seed, i), minutes=minutes,
                 label=label or f"{a} vs {b}", swap=bool(i % 2)) for i in range(games)]


def game_seed(seed: int, i: int) -> int:
    """The ``i``-th game's seed of a run on ``seed``: the same for every opponent and every command
    (matchup, compare, gate, the board), so the champion's game against an opponent on a seed is
    played once and reused by every later run (lab/reuse.py). Until 2026-10-16 compare and gate added
    1000 x the baseline's place in --baselines, which made each list of baselines its own games."""
    return seed * 10007 + i


def compare(candidates: list[str], baselines: list[str], games: int, *, maps: list[str] = MAPS, seed: int = 1,
            minutes: int = 20, interleaved: bool = False, h2h: bool = False) -> list[dict]:
    """Every candidate against every baseline, ``games`` each, on the same seeds (paired).
    ``interleaved``: game 1 against every baseline, then game 2, ... (a sequential test's order:
    a stop at any point has seen every baseline about as often). ``h2h``: and ``games`` of each
    candidate against the reference (the first), seats swapped every other game."""
    order = ([(k, i) for i in range(games) for k in range(len(baselines))] if interleaved
             else [(k, i) for k in range(len(baselines)) for i in range(games)])
    jobs = []
    for n, (k, i) in enumerate(order):
        if h2h and (n % len(baselines) == 0 if interleaved else n < games):
            g = i if interleaved else n
            for who in candidates[1:]:
                jobs.append(_job(who, candidates[0], map_id=maps[g % len(maps)], seed=game_seed(seed, g),
                                 minutes=minutes, label=f"compare:h2h", swap=bool(g % 2)))
        for who in candidates:
            jobs.append(_job(who, baselines[k], map_id=maps[i % len(maps)], seed=game_seed(seed, i), minutes=minutes,
                             label=f"compare:{baselines[k]}", swap=bool(i % 2)))
    return jobs


def league(bots: list[str], games: int, *, kinds: dict[str, str], maps: list[str] = MAPS, seed: int = 1,
           minutes: int = 20, played: dict[tuple[str, str], int] | None = None, label: str = "league") -> list[dict]:
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
         minutes: int = 20, interleaved: bool = False) -> list[dict]:
    """``games`` against the champion, and ``games`` each for candidate and champion against every baseline
    (the same seeds for both). ``interleaved``: game 1 of each (head to head, then every baseline), then
    game 2, ... (a sequential test's order)."""
    def h2h(i):
        return [_job(candidate, champion, map_id=maps[i % len(maps)], seed=game_seed(seed, i), minutes=minutes,
                     label=f"gate:{candidate}:h2h", swap=bool(i % 2))]

    def against(base, i):
        return [_job(who, base, map_id=maps[i % len(maps)], seed=game_seed(seed, i), minutes=minutes,
                     label=f"gate:{candidate}:{base}", swap=bool(i % 2)) for who in (candidate, champion)]

    if interleaved:
        return [j for i in range(games) for j in h2h(i) + [x for base in baselines for x in against(base, i)]]
    return [j for i in range(games) for j in h2h(i)] + [x for base in baselines for i in range(games)
                                                         for x in against(base, i)]
