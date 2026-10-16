"""What we say. A ``Talker`` turns the situation into the next chat message (or nothing).

``ScriptedTalker`` is the default: short templates, deterministic, under 50 words. The seam for a
language model is ``Talker``: it gets a ``TalkContext`` (the beliefs, the plan, ``facts()`` as plain
JSON for a prompt) and returns an ``Utterance`` or ``None``. Whatever a talker returns passes
``agent.check_message`` (limits, recipients) before it is sent, and any exception means "terminate".
Nothing here calls a model.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .belief import Belief, Memory, Result
from .policy import Settings, kill_scores, known_wolves_alive, vote_villager, vote_wolf
from .view import SEER, VILLAGER, WOLF, Snapshot


def pn(seat: int) -> str:
    return f"Player{seat}"


@dataclass(frozen=True)
class Utterance:
    text: str
    recipients: tuple[int, ...] = ()  # () broadcasts, (i,) is private
    act: str = ""  # what it was, so it isn't said twice in a day
    commit: Callable[[], None] | None = field(default=None, compare=False)  # run once it has been sent


@dataclass
class TalkContext:
    snap: Snapshot
    mem: Memory
    bel: Belief  # our own posterior (private facts included)
    village: Belief  # the public posterior: what the table can know
    settings: Settings
    rng: random.Random
    new_msgs: list = field(default_factory=list)  # messages we haven't seen before this call

    def facts(self) -> dict:
        """A JSON-able summary an LLM talker can put in its prompt."""
        snap, mem = self.snap, self.mem
        return {
            "me": snap.me, "role": snap.role, "partner": snap.partner, "day": snap.day,
            "alive": sorted(snap.alive),
            "dead": {s: {"role": d.role, "cause": d.cause, "day": d.day} for s, d in snap.dead.items()},
            "seer_claims": {s: {"day": d, "results": [(r.target, r.verdict) for r in mem.results if r.by == s]}
                            for s, (d, _) in mem.claims.items()},
            "my_checks": dict(mem.my_checks),
            "p_wolf": {s: round(self.bel.p_wolf(s), 3) for s in sorted(snap.alive)},
            "p_seer": {s: round(self.bel.p_seer(s), 3) for s in sorted(snap.alive)},
        }


class Talker(Protocol):
    def next_message(self, ctx: TalkContext) -> Utterance | None: ...


# -- shared bits -------------------------------------------------------------------------------

def _results_text(results: list[tuple[int, str]]) -> str:
    return ", ".join(f"{pn(t)} is {'a WOLF' if v == WOLF else 'a villager'}" for t, v in results)


def reason_for(snap: Snapshot, mem: Memory, target: int) -> str:
    """One short, true reason to suspect ``target`` from the record, or a plain admission."""
    for r in mem.results:
        if r.target == target and r.verdict == WOLF and r.by in mem.claims:
            return f"{pn(r.by)}, who claims seer, checked them as a wolf"
    bad = [(d, tgt) for d, voter, tgt in snap.votes
           if voter == target and tgt in snap.dead and snap.dead[tgt].role in (VILLAGER, SEER)]
    if bad:
        d, tgt = bad[0]
        return f"they voted for {pn(tgt)} on day {d}, who turned out to be {snap.dead[tgt].role}"
    n = len({by for (by, t, _), s in mem.accusations.items() if t == target and s >= 0.6})
    if n >= 2:
        return f"{n} players already point at them"
    cleared = [t for t, v in mem.my_checks.items() if v == VILLAGER]
    if snap.role == SEER and cleared:
        return "my checks cleared the others I looked at"
    return "nobody has a better lead and a no-lynch day helps the wolves"


def best_claimant(ctx: TalkContext) -> int | None:
    """The claimed seer (not us) most likely to be the real one, if any is more likely than not."""
    cands = [c for c in ctx.mem.claims if c != ctx.snap.me and c in ctx.snap.alive]
    if not cands:
        return None
    c = max(cands, key=lambda s: (ctx.village.p_seer(s), -s))
    return c if ctx.village.p_seer(c) >= 0.4 else None


def accused_us(ctx: TalkContext) -> bool:
    me = ctx.snap.me
    return any(t == me and d == ctx.snap.day and by != me and s >= 0.6 for (by, t, d), s in ctx.mem.accusations.items())


# -- the scripted talker ---------------------------------------------------------------------------

class ScriptedTalker:
    def next_message(self, ctx: TalkContext) -> Utterance | None:
        role = ctx.snap.role
        order = {SEER: self._seer, WOLF: self._wolf}.get(role or "", self._villager)
        for utterance in order(ctx):
            if utterance is not None and utterance.act not in ctx.mem.sent.get(ctx.snap.day, []):
                return utterance
        return None

    # villager ---------------------------------------------------------------------------------

    def _target_villager(self, ctx: TalkContext) -> int:
        return vote_villager(ctx.snap, ctx.mem, ctx.bel, ctx.rng, ctx.settings)

    def _villager(self, ctx: TalkContext):
        snap, mem = ctx.snap, ctx.mem
        claimant = best_claimant(ctx)
        yield self._open_villager(ctx, claimant)
        yield self._stance(ctx, claimant, self._target_villager(ctx))
        if accused_us(ctx):
            yield Utterance("I'm a plain villager, not a wolf. Look at my votes. Let's decide by evidence, "
                            "and the seer should claim.", act="defend")

    def _open_villager(self, ctx: TalkContext, claimant: int | None):
        """The day's first line when nobody has claimed seer. Says nothing about our own role."""
        if claimant is None and not ctx.mem.claims:
            if ctx.snap.day <= 1:
                return Utterance("No information yet. Seer, please claim now with your result, "
                                 "and we back a credible seer. A day with no lynch only helps the wolves.", act="open")
            return Utterance("Seer, please claim with your results. We vote on the most credible evidence; "
                             "a day with no lynch only helps the wolves.", act="open")
        return None

    def _stance(self, ctx: TalkContext, claimant: int | None, target: int) -> Utterance | None:
        snap, mem = ctx.snap, ctx.mem
        mem.vote_plan[snap.day] = target
        others = [c for c in mem.claims if c != snap.me and c in snap.alive]
        if len(others) >= 2:
            return Utterance(f"{', '.join(pn(c) for c in sorted(others))} all claim seer, so at least one is lying. "
                             f"I trust {pn(claimant) if claimant is not None else pn(others[0])} more. I vote {pn(target)}.",
                             act="stance")
        if claimant is not None:
            return Utterance(f"I believe {pn(claimant)} is the seer, their results are consistent. "
                             f"I vote {pn(target)}: {reason_for(snap, mem, target)}.", act="stance")
        return Utterance(f"I vote {pn(target)}: {reason_for(snap, mem, target)}.", act="stance")

    # seer -------------------------------------------------------------------------------------

    def _checks(self, ctx: TalkContext) -> list[tuple[int, str]]:
        mem = ctx.mem
        return sorted(mem.my_checks.items(), key=lambda kv: mem.check_days.get(kv[0], 0))

    def _should_claim(self, ctx: TalkContext) -> bool:
        snap, mem = ctx.snap, ctx.mem
        if not mem.my_checks:
            return False
        if known_wolves_alive(snap, mem):
            return True
        if snap.day >= ctx.settings.claim_by_day:
            return True
        if any(c != snap.me for c in mem.claims):
            return True  # a rival claim: answer at once
        return accused_us(ctx)

    def _seer(self, ctx: TalkContext):
        snap, mem = ctx.snap, ctx.mem
        checks = self._checks(ctx)
        wolves = known_wolves_alive(snap, mem)
        target = self._target_villager(ctx)
        mem.vote_plan[snap.day] = target
        if self._should_claim(ctx):
            said = mem.claim_day is not None
            if not said or len(checks) > mem.claimed_checks:
                lead = ("I am the REAL seer." if any(c != snap.me for c in mem.claims) else "I am the seer.") if not said \
                    else "Seer update."
                text = f"{lead} My checks: {_results_text(checks)}. "
                text += (f"Vote {pn(wolves[0])} today. " if wolves else f"I vote {pn(target)}. ")
                text += "Don't vote me out."
                n, day = len(checks), snap.day

                def commit(n=n, day=day):
                    mem.claimed_checks = n
                    if mem.claim_day is None:
                        mem.claim_day = day

                yield Utterance(text, act="claim", commit=commit)
            yield Utterance(f"Reminder: vote {pn(wolves[0] if wolves else target)}. Only the seer's checks are certain.",
                            act="remind")
        else:
            yield self._open_villager(ctx, best_claimant(ctx)) if not mem.claims else None
            yield self._stance(ctx, best_claimant(ctx), target)
        if accused_us(ctx):
            yield Utterance("I'm not a wolf. Judge me by my votes. Let's not hand the wolves a free villager.", act="defend")

    # wolf -------------------------------------------------------------------------------------

    def _wolf(self, ctx: TalkContext):
        snap, mem = ctx.snap, ctx.mem
        partner = snap.partner
        alive_partner = partner is not None and partner in snap.alive
        legal_kill = [s for s in snap.alive if s not in (snap.me, partner)]
        kills = kill_scores(snap, mem, ctx.bel, legal_kill) if legal_kill else {}
        kill = max(sorted(kills), key=lambda s: kills[s]) if kills else None
        vote = vote_wolf(snap, mem, ctx.bel, ctx.village, ctx.rng, ctx.settings)
        mem.vote_plan[snap.day] = vote
        if alive_partner:
            bits = []
            if kill is not None:
                bits.append(f"kill {kill} tonight")
            if vote < 7:
                bits.append(f"vote {vote} today")
            counter = designated_counterclaimer(ctx)
            if counter is not None:
                bits.append("I will claim seer" if counter == snap.me else "you claim seer")
            if bits:
                yield Utterance("; ".join(bits).capitalize() + ". Don't vote me or each other.", (partner,), act="plan")
        decision = self._counterclaim(ctx)
        if decision is not None:
            yield decision
        if alive_partner and partner in mem.claims:
            yield Utterance(f"I believe {pn(partner)}: their check on {pn(_fake_target(ctx, partner))} fits what I see. "
                            f"Vote {pn(vote)}.", act="support")
        if mem.fake_claim_day is None:
            claimant = best_claimant(ctx)
            yield self._open_villager(ctx, claimant)
            if vote < 7:
                rivals = [c for c in mem.claims if c not in (snap.me, partner) and c in snap.alive]
                if rivals:
                    yield Utterance(f"I can't tell whom to trust among the seer claims. "
                                    f"I vote {pn(vote)}: {reason_for(snap, mem, vote)}.", act="stance")
                else:
                    yield self._stance(ctx, None, vote)
        if accused_us(ctx):
            if mem.fake_claim_day is not None:
                yield Utterance("Of course the wolves accuse the real seer. I am the seer; my checks are right. "
                                "Do not lynch me.", act="defend")
            else:
                yield Utterance("I'm not a wolf and the accusation has no evidence. "
                                "Look at the votes before you lynch a villager.", act="defend")

    def _counterclaim(self, ctx: TalkContext) -> Utterance | None:
        snap, mem, st = ctx.snap, ctx.mem, ctx.settings
        if mem.fake_claim_day is not None and mem.fake_claim_day < snap.day:
            return self._fake_update(ctx)
        if mem.fake_claim_day is not None:
            return None
        rivals = [c for c in mem.claims if c not in (snap.me, snap.partner) and c in snap.alive]
        if rivals and designated_counterclaimer(ctx) != snap.me:
            return None
        if rivals and st.counterclaim:
            rival = max(rivals, key=lambda s: (ctx.bel.p_seer(s), -s))
            results = [Result(snap.me, rival, WOLF, snap.day)]
            if snap.day >= 2 and snap.partner is not None and snap.partner in snap.alive:
                results.append(Result(snap.me, snap.partner, VILLAGER, snap.day))
            text = (f"I am the real seer. I checked {pn(rival)}: WOLF. {pn(rival)} is lying to save a wolf. "
                    f"{'I also cleared ' + pn(snap.partner) + '. ' if len(results) > 1 else ''}"
                    f"Vote {pn(rival)} today.")
            return Utterance(text, act="counterclaim", commit=lambda: self._fake(mem, results, snap.day))
        wolves = sorted(w for w in (snap.me, snap.partner) if w is not None and (w == snap.me or w in snap.alive))
        if not rivals and snap.day == 1 and st.preempt_claim > 0 and wolves[0] == snap.me \
                and mem.preempt(ctx.rng, st.preempt_claim) and not (snap.partner in mem.claims):
            pool = [s for s in snap.alive if s not in (snap.me, snap.partner)]
            if not pool:
                return None
            victim = max(pool, key=lambda s: (ctx.village.p_wolf(s), -s))
            results = [Result(snap.me, victim, WOLF, snap.day)]
            return Utterance(f"I am the seer. Night 1 I checked {pn(victim)}: WOLF. Vote {pn(victim)} today.",
                             act="counterclaim", commit=lambda: self._fake(mem, results, snap.day))
        return None

    def _fake_update(self, ctx: TalkContext) -> Utterance | None:
        snap, mem = ctx.snap, ctx.mem
        pool = [s for s in snap.alive if s not in (snap.me, snap.partner) and all(r.target != s for r in mem.fake_results)]
        if not pool:
            return None
        victim = max(pool, key=lambda s: (ctx.village.p_wolf(s), -s))
        result = Result(snap.me, victim, WOLF, snap.day)
        return Utterance(f"Seer update: last night I checked {pn(victim)}: WOLF. Vote {pn(victim)}.", act=f"fake{snap.day}",
                         commit=lambda: mem.fake_results.append(result))

    @staticmethod
    def _fake(mem: Memory, results: list[Result], day: int) -> None:
        mem.fake_results.extend(results)
        mem.fake_claim_day = day


def _fake_target(ctx: TalkContext, partner: int) -> int:
    for r in ctx.mem.results:
        if r.by == partner:
            return r.target
    return partner


def designated_counterclaimer(ctx: TalkContext) -> int | None:
    """Which wolf counter-claims a rival seer: the one the real seer did NOT name, else the lower seat.
    Both wolves compute this from the same public record, so they agree without talking."""
    snap, mem = ctx.snap, ctx.mem
    rivals = [c for c in mem.claims if c not in (snap.me, snap.partner) and c in snap.alive]
    if not rivals:
        return None
    if snap.partner is None or snap.partner not in snap.alive:
        return snap.me
    if snap.partner in mem.claims:
        return snap.partner  # already claimed
    named = {r.target for r in mem.results if r.by in rivals and r.verdict == WOLF}
    wolves = sorted((snap.me, snap.partner))
    free = [w for w in wolves if w not in named]
    return free[0] if len(free) == 1 else wolves[0]
