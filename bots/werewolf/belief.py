"""The belief tracker: what happened, who claimed what, and who is probably a wolf.

``Memory`` is what accumulates over a match (chat events, our own seer results, our plans).
``Belief`` is the inference: it enumerates every assignment "these two seats are the wolves, this one
is the seer" (21 x up to 5 = 105 of them), throws out the ones that contradict a hard fact (a
revealed role, our own role, our own seer results), and weighs the rest by how likely the observed
claims, results, votes and accusations are under each. All numbers are plain heuristics, in
``Weights``; they are rough on purpose and are tuned (if at all) in the simulator, not trusted.

Deterministic and fast: about 105 hypotheses and a few hundred factors, a few milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .chat import N_SEATS, ChatEvent
from .view import SEER, VILLAGER, WOLF, Snapshot


@dataclass(frozen=True)
class Weights:
    claim_seer: float = 0.9  # a seer claims (at some point)
    claim_wolf: float = 0.25  # a wolf claims seer (a counter-claim)
    claim_villager: float = 0.03
    quiet_seer_day1: float = 0.7  # no claim yet
    quiet_seer_later: float = 0.3
    quiet_wolf: float = 0.75
    quiet_villager: float = 0.97
    first_claim_bonus: float = 1.3  # the earliest of several claimants
    fake_accuses_partner: float = 0.15  # a lying "seer" naming their own partner a wolf
    fake_names_villager_wolf: float = 0.7  # ... naming someone else a wolf (the usual lie)
    fake_clears_partner: float = 0.5
    fake_clears_villager: float = 0.25
    vote_wolf_on_wolf: float = 0.3  # relative weight of a wolf voting for the other wolf
    vote_other_on_wolf: float = 1.5  # a non-wolf voting for a wolf
    vote_abstain: float = 0.4
    accuse_wolf_on_wolf: float = 0.4  # per unit strength: 1 - this
    accuse_other_on_wolf: float = 0.25  # 1 + this
    survive_night: float = 0.7  # a claimed seer still alive after a night: less likely the real one
    broken: float = 0.001  # a hypothesis a claimed seer's results contradict


@dataclass(frozen=True)
class Result:
    by: int
    target: int
    verdict: str  # "wolf" | "villager"
    day: int


@dataclass
class Memory:
    """Everything the bot remembers across calls in one match."""

    seen: set[int] = field(default_factory=set)  # message seqs already read
    order: int = 0
    claims: dict[int, tuple[int, int]] = field(default_factory=dict)  # seat -> (day, order)
    results: list[Result] = field(default_factory=list)
    accusations: dict[tuple[int, int, int], float] = field(default_factory=dict)  # (by, target, day) -> strength
    defenses: dict[tuple[int, int, int], float] = field(default_factory=dict)
    announced: dict[tuple[int, int], int] = field(default_factory=dict)  # (seat, day) -> who they said they'd vote
    partner_kill: dict[int, int] = field(default_factory=dict)  # day -> target the partner proposed
    partner_vote: dict[int, int] = field(default_factory=dict)
    partner_counterclaim: set[int] = field(default_factory=set)  # days the partner said they'd claim seer
    my_checks: dict[int, str] = field(default_factory=dict)  # seer: target -> "wolf" | "villager"
    check_days: dict[int, int] = field(default_factory=dict)
    pending_check: int | None = None
    pending_day: int = 0
    warned_unparsed: bool = False
    unparsed_checks: int = 0
    # wolf
    fake_results: list[Result] = field(default_factory=list)
    fake_claim_day: int | None = None
    # talk
    sent: dict[int, list[str]] = field(default_factory=dict)  # day -> acts spoken
    chats: dict[int, int] = field(default_factory=dict)  # day -> chats sent
    done: set[int] = field(default_factory=set)  # days whose window we terminated
    claim_day: int | None = None  # seer: the day we claimed
    claimed_checks: int = 0  # seer: how many of our checks the claim has announced
    vote_plan: dict[int, int] = field(default_factory=dict)  # day -> our announced target
    preempt_roll: bool | None = None
    version: int = 0

    def preempt(self, rng, p: float) -> bool:
        """The wolf's one die roll per match for a day-1 seer claim before anyone else's."""
        if self.preempt_roll is None:
            self.preempt_roll = rng.random() < p
        return self.preempt_roll

    def add_events(self, events: list[ChatEvent], day: int) -> None:
        for e in events:
            self.version += 1
            self.order += 1
            if e.kind == "claim":
                self.claims.setdefault(e.sender, (day, self.order))
            elif e.kind == "result" and e.target is not None and e.verdict:
                r = Result(e.sender, e.target, e.verdict, day)
                if r not in self.results:
                    self.results.append(r)
            elif e.kind == "accuse" and e.target is not None:
                key = (e.sender, e.target, day)
                self.accusations[key] = max(self.accusations.get(key, 0.0), e.strength)
                self.announced[(e.sender, day)] = e.target if e.strength >= 1.0 else self.announced.get((e.sender, day), e.target)
            elif e.kind == "defend" and e.target is not None:
                key = (e.sender, e.target, day)
                self.defenses[key] = max(self.defenses.get(key, 0.0), e.strength)
            elif e.kind == "plan_kill" and e.target is not None:
                self.partner_kill[day] = e.target
            elif e.kind == "plan_vote" and e.target is not None:
                self.partner_vote[day] = e.target
            elif e.kind == "plan_counterclaim":
                self.partner_counterclaim.add(day)


class Belief:
    """Posterior over (wolf pair, seer) given the public record, chat and (optionally) our private facts."""

    def __init__(self, snap: Snapshot, mem: Memory, *, private: bool = True, weights: Weights | None = None) -> None:
        self.snap = snap
        self.mem = mem
        self.private = private
        self.w = weights or Weights()
        self.hyps: list[tuple[tuple[int, int], int, float]] = []
        self._pw = [0.0] * N_SEATS
        self._ps = [0.0] * N_SEATS
        self._compute()

    # -- hard facts -----------------------------------------------------------------------

    def _consistent(self, pair: tuple[int, int], seer: int, *, facts: bool, mine: bool) -> bool:
        snap = self.snap
        if facts:
            for seat, d in snap.dead.items():
                if d.role == WOLF and seat not in pair:
                    return False
                if d.role == SEER and seer != seat:
                    return False
                if d.role == VILLAGER and (seat in pair or seer == seat):
                    return False
                if d.role is not None and d.role != WOLF and seat in pair:
                    return False
        if mine and self.private:
            me = snap.me
            if snap.role == WOLF:
                if me not in pair or seer == me:
                    return False
                if snap.partner is not None and snap.partner not in pair:
                    return False
            elif snap.role == VILLAGER:
                if me in pair or seer == me:
                    return False
            elif snap.role == SEER:
                if seer != me:
                    return False
            for target, verdict in self.mem.my_checks.items():
                if (target in pair) != (verdict == WOLF):
                    return False
        return True

    def _role(self, seat: int, pair: tuple[int, int], seer: int) -> str:
        return WOLF if seat in pair else SEER if seat == seer else VILLAGER

    # -- the weighing ---------------------------------------------------------------------

    def _compute(self) -> None:
        w, snap, mem = self.w, self.snap, self.mem
        raw = [(p, s) for p in combinations(range(N_SEATS), 2) for s in range(N_SEATS) if s not in p]
        cands = [(p, s) for p, s in raw if self._consistent(p, s, facts=True, mine=True)]
        if not cands:  # a fact we misread: drop our private facts, then the revealed roles
            cands = [(p, s) for p, s in raw if self._consistent(p, s, facts=True, mine=False)] or raw
        claimants = sorted(mem.claims, key=lambda c: mem.claims[c][1])
        first = claimants[0] if claimants else None
        nights_after = {c: sum(1 for d in snap.dead.values() if d.cause == "night_kill" and d.day > mem.claims[c][0]
                               and d.seat != c) for c in claimants}
        quiet_seer = w.quiet_seer_day1 if snap.day <= 1 else w.quiet_seer_later
        votes_by_day: dict[int, list[tuple[int, int | None]]] = {}
        for d, voter, target in snap.votes:
            votes_by_day.setdefault(d, []).append((voter, target))
        alive_at = {d: snap.alive_at_vote(d) for d in votes_by_day}

        for pair, seer in cands:
            weight = 1.0
            # claims
            for seat in range(N_SEATS):
                role = self._role(seat, pair, seer)
                if seat in mem.claims:
                    weight *= {SEER: w.claim_seer, WOLF: w.claim_wolf, VILLAGER: w.claim_villager}[role]
                elif seat in snap.alive:
                    weight *= {SEER: quiet_seer, WOLF: w.quiet_wolf, VILLAGER: w.quiet_villager}[role]
            if len(claimants) > 1 and seer == first:
                weight *= w.first_claim_bonus
            for c in claimants:
                if seer == c and c in snap.alive:
                    weight *= w.survive_night ** nights_after[c]
            # claimed results
            for r in mem.results:
                truth = (r.target in pair) == (r.verdict == WOLF)
                if r.by == seer:
                    if not truth:
                        weight *= w.broken
                elif r.by in pair:
                    if r.verdict == WOLF:
                        weight *= w.fake_accuses_partner if r.target in pair else w.fake_names_villager_wolf
                    else:
                        weight *= w.fake_clears_partner if r.target in pair else w.fake_clears_villager
            # votes
            for d, ballots in votes_by_day.items():
                alive = alive_at[d]
                for voter, target in ballots:
                    v_wolf = voter in pair
                    own = w.vote_wolf_on_wolf if v_wolf else w.vote_other_on_wolf

                    def wt(t: int) -> float:
                        return own if t in pair else 1.0

                    total = w.vote_abstain + sum(wt(t) for t in alive if t != voter)
                    weight *= (w.vote_abstain if target is None else wt(target)) / total if total else 1.0
            # accusations and defenses (chat)
            for (by, target, _), strength in mem.accusations.items():
                if target in pair:
                    weight *= (1 - w.accuse_wolf_on_wolf * strength) if by in pair else (1 + w.accuse_other_on_wolf * strength)
            for (by, target, _), strength in mem.defenses.items():
                if target in pair:
                    weight *= (1 + 0.3 * strength) if by in pair else (1 - 0.15 * strength)
            self.hyps.append((pair, seer, weight))

        total = sum(h[2] for h in self.hyps) or 1.0
        for pair, seer, wt in self.hyps:
            for p in pair:
                self._pw[p] += wt / total
            self._ps[seer] += wt / total

    # -- reading it -----------------------------------------------------------------------

    def p_wolf(self, seat: int) -> float:
        return self._pw[seat]

    def p_seer(self, seat: int) -> float:
        return self._ps[seat]

    def p_pair(self, a: int, b: int) -> float:
        key = (min(a, b), max(a, b))
        total = sum(h[2] for h in self.hyps) or 1.0
        return sum(wt for pair, _, wt in self.hyps if pair == key) / total

    def ranked_wolves(self, among: list[int] | set[int]) -> list[int]:
        """``among`` sorted by wolf probability, highest first; ties go to the lower seat."""
        return sorted(among, key=lambda s: (-round(self._pw[s], 9), s))
