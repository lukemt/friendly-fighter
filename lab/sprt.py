"""Stop a comparison when it is clear: the sequential probability ratio test (lab compare --sprt).

As chess engines are tested (Stockfish's Fishtest, OpenBench): each candidate's paired difference
from the reference (its points minus the reference's on the same game: the same opponent, map,
seed and seat) is tested after every game, H0 "better by ``t0`` points a game" against H1 "better
by ``t1``", with the normal approximation of the log-likelihood ratio (the GSPRT):

    LLR = n (t1 - t0) (2 mean - t0 - t1) / (2 var)

and the run stops playing a candidate's games when the LLR leaves ``[log(beta / (1 - alpha)),
log((1 - beta) / alpha)]``: H1 accepted ("better"), or H0 ("not better"). The variance is the
pairs' own, shrunk towards the lab's measured 0.27 (sd 0.52 over 994 pairs, 2026-10-16) by
``PRIOR`` pairs: early on most pairs are equal (73%), and a variance near 0 would stop at once.

Simulated on the measured pair outcomes (73% equal), bounds [0, 0.1], alpha = beta = 0.05, at most
216 pairs: a candidate 0.1 better is called better 78% of the time (a fixed 56-pair compare: 31%),
one no better 4%; one 0.3 worse stops after ~21 pairs, 0.1 worse ~50, 0.2 better ~48.
"""

from __future__ import annotations

import math

PAIR_VARIANCE = 0.27  # measured: sd 0.52 of the points difference of a pair (994 pairs, 2026-10-16)
PRIOR = 10            # pairs' worth of that variance mixed into a run's own
MIN_PAIRS = 12        # no verdict before this many pairs


def bounds(alpha: float = 0.05, beta: float = 0.05) -> tuple[float, float]:
    return math.log(beta / (1 - alpha)), math.log((1 - beta) / alpha)


def llr(diffs: list[float], t0: float, t1: float) -> float:
    n = len(diffs)
    if n == 0:
        return 0.0
    mean = sum(diffs) / n
    s2 = sum((x - mean) ** 2 for x in diffs) / (n - 1) if n > 1 else PAIR_VARIANCE
    var = (n * s2 + PRIOR * PAIR_VARIANCE) / (n + PRIOR)
    return n * (t1 - t0) * (2 * mean - t0 - t1) / (2 * var)


def decide(diffs: list[float], t0: float = 0.0, t1: float = 0.1, alpha: float = 0.05, beta: float = 0.05,
           min_pairs: int = MIN_PAIRS) -> str | None:
    """"better" (H1), "not better" (H0) or None (play on)."""
    if len(diffs) < min_pairs:
        return None
    lo, hi = bounds(alpha, beta)
    value = llr(diffs, t0, t1)
    return "better" if value >= hi else "not better" if value <= lo else None


def parse_bounds(text: str) -> tuple[float, float]:
    t0, t1 = (float(x) for x in text.split(","))
    if not t1 > t0:
        raise ValueError(f"--bounds {text!r}: the second must be above the first (e.g. 0,0.1)")
    return t0, t1


class Sequential:
    """A compare's candidates against its reference, tested after every game; ``keep`` says which
    jobs still need playing (batch.run)."""

    def __init__(self, db, run_id: int, jobs: list[dict], candidates: list[str], t0: float, t1: float,
                 alpha: float, beta: float, min_pairs: int = MIN_PAIRS) -> None:
        self.db, self.run_id, self.jobs = db, run_id, jobs
        self.reference, self.others = candidates[0], list(candidates[1:])
        self.t0, self.t1, self.alpha, self.beta, self.min_pairs = t0, t1, alpha, beta, min_pairs
        self.verdict: dict[str, str | None] = {c: None for c in self.others}
        self.stopped = False  # (the run was interrupted)
        self.pairs: dict[str, int] = {c: 0 for c in self.others}
        self.update()

    def _subject(self, job: dict) -> str | None:
        for name in job["bots"]:
            if name == self.reference or name in self.verdict:
                return name
        return None

    def diffs(self, candidate: str) -> list[float]:
        from .reuse import run_clause

        clause, args = run_clause("g", [self.run_id])
        rows = self.db.q(f"SELECT g.map, g.seed, s.bot, s.position, s.points, o.bot AS opponent FROM games g "
                         f"JOIN seats s ON s.game_id = g.id JOIN seats o ON o.game_id = g.id AND o.position != s.position "
                         f"WHERE {clause} AND g.branch_of IS NULL AND s.bot IN (?, ?)", *args, self.reference, candidate)
        by: dict[tuple, dict[str, float]] = {}
        for r in rows:
            if r["points"] is None:
                continue
            by.setdefault((r["opponent"], r["map"], r["seed"], r["position"]), {})[r["bot"]] = r["points"]
        return [v[candidate] - v[self.reference] for v in by.values() if candidate in v and self.reference in v]

    def update(self) -> None:
        for c in self.others:
            if self.verdict[c] is None:
                d = self.diffs(c)
                self.pairs[c] = len(d)
                self.verdict[c] = decide(d, self.t0, self.t1, self.alpha, self.beta, self.min_pairs)

    def keep(self, index: int) -> bool:
        who = self._subject(self.jobs[index])
        if who == self.reference:
            return any(v is None for v in self.verdict.values())
        return who is None or self.verdict.get(who) is None

    def summary(self) -> str:
        lines = [f"sequential test (--sprt): H0 {self.t0:+.2f} vs H1 {self.t1:+.2f} points a game, "
                 f"alpha {self.alpha:g}, beta {self.beta:g}"]
        for c in self.others:
            v = self.verdict[c]
            text = {"better": f"BETTER: H1 accepted (at least about {self.t1:+.2f} a game)",
                    "not better": f"NOT BETTER: H0 accepted (no gain of {self.t1:+.2f} a game)"}.get(
                v, "undecided: the run stopped before the cap" if self.stopped else
                "undecided at the cap (--games): more games, or call it no clear difference")
            lines.append(f"  {c}: {text}, after {self.pairs[c]} pairs (LLR {llr(self.diffs(c), self.t0, self.t1):+.2f})")
        return "\n".join(lines)
