"""What to do at night and in the vote, per role. Pure functions of the snapshot, memory and belief."""

from __future__ import annotations

import random
from dataclasses import dataclass

from .belief import Belief, Memory
from .chat import ABSTAIN
from .view import SEER, VILLAGER, WOLF, Snapshot


@dataclass(frozen=True)
class Settings:
    """The bot's knobs. The defaults are what plays; the simulator varies them for ablations."""

    talk: bool = True
    max_chats: int = 4  # per day, of the platform's 5
    claim_by_day: int = 2  # a seer without a wolf finding claims by this day
    counterclaim: bool = True  # a wolf counter-claims seer when a real seer has claimed
    preempt_claim: float = 0.0  # chance a wolf claims seer on day 1 before anyone else does
    no_info_vote: str = "lowest"  # "lowest" | "hash": how a vote with nothing to go on is placed
    abstain_below: float = 0.0  # abstain when the best suspect is below this wolf probability (0 = never)
    follow_partner: bool = True


def _pick(scores: dict[int, float], rng: random.Random, tie: str = "lowest") -> int:
    best = max(scores.values())
    top = sorted(s for s, v in scores.items() if v >= best - 1e-9)
    return top[0] if tie == "lowest" or len(top) == 1 else rng.choice(top)


# -- night --------------------------------------------------------------------------------

def night_seer(snap: Snapshot, mem: Memory, bel: Belief, rng: random.Random) -> int:
    legal = snap.targets()
    pool = [s for s in legal if s not in mem.my_checks] or legal
    return _pick({s: bel.p_wolf(s) for s in pool}, rng, "hash")


def threat(snap: Snapshot, mem: Memory, c: int, wolves: set[int]) -> float:
    """How much seat ``c`` has gone after the wolves: votes on them, accusations of them."""
    t = 0.0
    for _, voter, target in snap.votes:
        if voter == c and target in wolves:
            t += 0.6
    for (by, target, _), strength in mem.accusations.items():
        if by == c and target in wolves:
            t += 0.4 * strength
    return t


def kill_scores(snap: Snapshot, mem: Memory, bel: Belief, legal: list[int]) -> dict[int, float]:
    wolves = {snap.me} | ({snap.partner} if snap.partner is not None else set())
    scores = {}
    for c in legal:
        score = 5.0 * bel.p_seer(c) + threat(snap, mem, c, wolves)
        if c in mem.claims:
            score += 2.0  # a seer claim from outside the wolf pair: the real seer, or a villager who'd confuse the vote
        scores[c] = score
    return scores


def night_wolf(snap: Snapshot, mem: Memory, bel: Belief, rng: random.Random, settings: Settings) -> int:
    legal = snap.targets()
    if settings.follow_partner and snap.partner is not None and snap.partner < snap.me and mem.partner_kill:
        plan = mem.partner_kill[max(mem.partner_kill)]
        if plan in legal:
            return plan  # the lower seat's pick wins a disagreement anyway
    return _pick(kill_scores(snap, mem, bel, legal), rng, "hash")


# -- day vote -----------------------------------------------------------------------------

def known_wolves_alive(snap: Snapshot, mem: Memory) -> list[int]:
    return sorted(t for t, v in mem.my_checks.items() if v == WOLF and t in snap.alive)


def vote_villager(snap: Snapshot, mem: Memory, bel: Belief, rng: random.Random, settings: Settings) -> int:
    """Villager and seer: the most likely wolf among the legal seats, never an abstention by default."""
    legal = snap.targets()
    if not legal:
        return ABSTAIN
    if snap.role == SEER:
        known = [s for s in known_wolves_alive(snap, mem) if s in legal]
        if known:
            return known[0]
    scores = {}
    for s in legal:
        p = bel.p_wolf(s)
        if snap.role == SEER and mem.my_checks.get(s) == VILLAGER:
            p = 0.0
        scores[s] = p
    plan = mem.vote_plan.get(snap.day)
    if plan in scores:
        scores[plan] += 0.04  # stick with what we said unless the evidence moved
    best = max(scores.values())
    if best < settings.abstain_below and snap.lynch_free_days() < 2:
        return ABSTAIN
    return _pick(scores, rng, settings.no_info_vote)


def vote_wolf(snap: Snapshot, mem: Memory, bel: Belief, village: Belief, rng: random.Random, settings: Settings) -> int:
    """Wolf: blend in with where the village is heading, never the partner, and lynch the seer when we can."""
    legal = [s for s in snap.targets() if s != snap.partner]
    if not legal:
        return ABSTAIN
    if settings.follow_partner and snap.partner is not None and snap.partner < snap.me and mem.partner_vote:
        plan = mem.partner_vote[max(mem.partner_vote)]
        if plan in legal:
            return plan
    scores = {}
    for s in legal:
        score = village.p_wolf(s) + 0.8 * bel.p_seer(s)
        if s in mem.claims:
            score += 0.3
        scores[s] = score
    plan = mem.vote_plan.get(snap.day)
    if plan in scores:
        scores[plan] += 0.04
    return _pick(scores, rng, settings.no_info_vote)
