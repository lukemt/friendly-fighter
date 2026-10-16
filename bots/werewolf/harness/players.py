"""Opponents for the simulator: today's agent, a random bot, and a simple talking rule bot.

- ``FirstLegal``: ``legal_actions[0]`` and no talk -- what friendly-fighter played before this bot
  (it votes for and kills the lowest seat).
- ``RandomBot``: a uniformly random legal action (abstain included), no talk.
- ``RuleBot``: a plain scripted player standing in for a competent LLM seat. It talks in its own
  phrasing (not ours), follows the first seer claim, accuses at random when it knows nothing, claims
  seer as the seer, and as a wolf kills a claimed seer, votes with the table and sometimes
  counter-claims. It is a stand-in: how real seats play is unknown until recordings exist.
"""

from __future__ import annotations

import random
import re
from typing import Any

from altruagent import TERMINATE_MESSAGING, SendMessage

ABSTAIN = 7


def ids(state) -> list[int]:
    return [int(a.action_id) for a in state.legal_actions]


def pick(state, seat: int):
    return next(a for a in state.legal_actions if a.action_id == str(seat))


class FirstLegal:
    def choose_action(self, state, context):
        return state.legal_actions[0]

    def choose_message(self, state, context):
        return TERMINATE_MESSAGING


class RandomBot:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def choose_action(self, state, context):
        return self.rng.choice(state.legal_actions)

    def choose_message(self, state, context):
        return TERMINATE_MESSAGING


class RuleBot:
    """See the module docstring."""

    def __init__(self, seat: int, rng: random.Random, *, counterclaim: float = 0.5) -> None:
        self.seat = seat
        self.rng = rng
        self.counterclaim = counterclaim
        self.role: str | None = None
        self.partner: int | None = None
        self.seen: set[int] = set()
        self.claimant: int | None = None  # first seer claimant (not us)
        self.accused: dict[int, int] = {}  # target -> mentions
        self.found: dict[int, str] = {}  # seer: target -> verdict
        self.pending: int | None = None
        self.said: set[tuple] = set()
        self.claimed = False
        self.fake: tuple[int, str] | None = None
        self.claim_results: dict[int, list[tuple[int, str]]] = {}

    # -- reading ---------------------------------------------------------------------------------

    def _read(self, state) -> None:
        obs = state.observation
        m = re.search(r"Your role: (\w+)", obs)
        self.role = m.group(1) if m else self.role
        m = re.search(r"fellow wolves: Player(\d)", obs)
        if m:
            self.partner = int(m.group(1))
        if self.role == "seer" and self.pending is not None:
            m = re.search(rf"Player{self.pending} is a (wolf|villager)", obs)
            if m:
                self.found[self.pending] = m.group(1)
                self.pending = None
        for msg in state.new_messages:
            if msg.index in self.seen or msg.sender == self.seat:
                continue
            self.seen.add(msg.index)
            text = msg.content.lower()
            if re.search(r"\bi(?:'m| am) (?:the )?(?:real )?seer", text):
                if self.claimant is None:
                    self.claimant = msg.sender
                for t, v in re.findall(r"player\s?(\d)\W{1,12}(?:is\s+)?(?:a\s+)?(wolf|villager)", text):
                    self.claim_results.setdefault(msg.sender, []).append((int(t), v))
            for t in re.findall(r"(?:suspect|vote|lynch)\D{0,16}player\s?(\d)", text):
                self.accused[int(t)] = self.accused.get(int(t), 0) + 1

    def _alive(self, state) -> list[int]:
        return [int(re.sub(r"\D", "", n)) for n in state.raw["game_state"]["alive"]]

    # -- moves -----------------------------------------------------------------------------------

    def choose_action(self, state, context):
        self._read(state)
        legal = ids(state)
        sub = state.raw["game_state"]["phase"]
        rng = self.rng
        if sub == "night_seer":
            fresh = [s for s in legal if s not in self.found]
            self.pending = rng.choice(fresh or legal)
            return pick(state, self.pending)
        if sub == "night_wolf":
            cand = [s for s in legal if self.claimant == s] or legal
            return pick(state, rng.choice(cand))
        # day vote
        real = [s for s in legal if s != ABSTAIN]
        if self.role == "seer":
            wolves = [t for t, v in self.found.items() if v == "wolf" and t in legal]
            if wolves:
                return pick(state, wolves[0])
            real = [s for s in real if self.found.get(s) != "villager"] or real
        if self.role == "wolf":
            real = [s for s in real if s != self.partner] or real
        target = None
        if self.role != "seer" and self.claimant is not None and self.claimant != self.seat:
            claim = [t for t, v in self.claim_results.get(self.claimant, []) if v == "wolf" and t in real]
            if claim and (self.role != "wolf" or self.claimant != self.partner):
                target = claim[0]
            elif self.role == "wolf" and self.claimant in real and self.claimant != self.partner:
                target = self.claimant  # a wolf goes after the claimed seer
        if target is None:
            hot = sorted(((n, -t) for t, n in self.accused.items() if t in real), reverse=True)
            if hot and hot[0][0] >= 2:
                target = -hot[0][1]
        if target is None:
            if rng.random() < 0.12:
                return pick(state, ABSTAIN)
            target = rng.choice(real)
        return pick(state, target)

    def choose_message(self, state, context):
        self._read(state)
        gs = state.raw["game_state"]
        day = gs["day"]
        legal_others = [s for s in self._alive(state) if s != self.seat]
        key = (day,)
        if key in self.said and ("again", day) in self.said:
            return TERMINATE_MESSAGING
        rng = self.rng
        if key not in self.said:
            self.said.add(key)
            if self.role == "seer" and self.found and (any(v == "wolf" for v in self.found.values()) or day >= 2):
                res = ", ".join(f"Player {t} is a {v}" for t, v in self.found.items())
                self.claimed = True
                return SendMessage(f"I'm the seer. I investigated: {res}. Vote accordingly!")
            if self.role == "wolf" and self.claimant is not None and self.claimant != self.partner and not self.fake \
                    and rng.random() < self.counterclaim:
                t = self.claimant
                self.fake = (t, "wolf")
                return SendMessage(f"No, I'm the seer! I checked Player {t} and they are a wolf. Don't trust them.")
            who = [s for s in legal_others if s != self.partner] or legal_others
            suspect = max(who, key=lambda s: (self.accused.get(s, 0), rng.random()))
            return SendMessage(f"I suspect Player {suspect}. I think we should vote for Player {suspect}.")
        self.said.add(("again", day))
        return TERMINATE_MESSAGING


def make_player(kind: str, seat: int, rng: random.Random, **kw: Any):
    if kind == "first":
        return FirstLegal()
    if kind == "random":
        return RandomBot(rng)
    if kind == "rule":
        return RuleBot(seat, rng, **kw)
    raise ValueError(f"unknown player kind {kind!r}")
