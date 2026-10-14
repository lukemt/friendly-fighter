"""Ratings from game results: a Bradley-Terry fit over every game (draws count half), on the Elo scale.

The fit uses all games at once, so it doesn't depend on their order, and one game of each bot
against a fixed 1500 anchor (half won) keeps a bot with only wins or only losses finite. ``sd`` is
the rating's standard error from the fit (wide while a bot has few games). The chance that A
beats B is ``1 / (1 + 10 ** ((Rb - Ra) / 400))``.
"""

from __future__ import annotations

import math
from collections import defaultdict

ANCHOR = "__anchor__"
SCALE = 400 / math.log(10)


def fit(results: list[tuple[str, str, float]], *, prior: float = 1.0, iterations: int = 500) -> dict[str, dict]:
    """``results``: (a, b, a's score: 1 win, 0.5 draw, 0 loss). Returns {bot: {rating, sd, games, score}}."""
    wins: dict[str, float] = defaultdict(float)
    n: dict[tuple[str, str], float] = defaultdict(float)
    games: dict[str, int] = defaultdict(int)
    score: dict[str, float] = defaultdict(float)
    for a, b, s in results:
        if a == b:
            continue
        wins[a] += s
        wins[b] += 1 - s
        n[(a, b)] += 1
        n[(b, a)] += 1
        games[a] += 1
        games[b] += 1
        score[a] += s
        score[b] += 1 - s
    players = sorted(games)
    for p in players:
        wins[p] += prior / 2
        n[(p, ANCHOR)] += prior
        n[(ANCHOR, p)] += prior
    opponents: dict[str, list[str]] = defaultdict(list)
    for (a, b) in n:
        opponents[a].append(b)
    strength = {p: 1.0 for p in players}
    strength[ANCHOR] = 1.0
    for _ in range(iterations):
        delta = 0.0
        new = {}
        for p in players:
            denom = sum(n[(p, q)] / (strength[p] + strength[q]) for q in opponents[p])
            new[p] = wins[p] / denom if denom > 0 else strength[p]
        for p in players:
            delta = max(delta, abs(math.log(new[p]) - math.log(strength[p])))
            strength[p] = new[p]
        if delta < 1e-9:
            break
    out = {}
    for p in players:
        info = sum(n[(p, q)] * strength[p] * strength[q] / (strength[p] + strength[q]) ** 2 for q in opponents[p])
        out[p] = {"rating": 1500 + SCALE * math.log(strength[p]), "sd": SCALE / math.sqrt(info) if info > 0 else float("inf"),
                  "games": games[p], "score": score[p]}
    return out


def expected(ra: float, rb: float) -> float:
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % interval of a rate (k of n)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)
