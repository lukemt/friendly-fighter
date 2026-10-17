"""The public record and our private view of one Werewolf state: a tolerant reader.

``state.raw["game_state"]`` holds ``day``, ``phase``, ``alive``, ``dead`` and ``vote_history`` in
display names (``Player3``); ``state.raw["players"]`` maps names to seats. The exact shapes of
``dead`` and ``vote_history`` came from the platform docs, not a recording yet, so every reader here
accepts several shapes and degrades to "less information", never to an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .chat import ABSTAIN, N_SEATS

WOLF, SEER, VILLAGER = "wolf", "seer", "villager"
_NAME_RE = re.compile(r"(\d+)\s*$")


def role_of(text: Any) -> str | None:
    t = str(text or "").lower()
    if "wolf" in t:
        return WOLF
    if "seer" in t:
        return SEER
    if "villager" in t or "townsfolk" in t or "town" == t:
        return VILLAGER
    return None


@dataclass(frozen=True)
class Death:
    seat: int
    role: str | None
    cause: str
    day: int


@dataclass
class Snapshot:
    me: int
    role: str | None
    partner: int | None
    day: int
    sub_phase: str | None
    alive: set[int]
    dead: dict[int, Death]
    votes: list[tuple[int, int, int | None]]  # (day, voter, target; None = abstain)
    lynched: dict[int, int | None]  # day -> seat voted out (None: nobody)
    observation: str
    messages: list[Any] = field(default_factory=list)
    legal: list[int] = field(default_factory=list)
    eliminated: bool = False
    winner: str | None = None

    def targets(self) -> list[int]:
        """Seats we may vote for or kill: the server's legal seats when it offers any (never the abstention),
        else (in a discussion window, when nothing is legal yet) every other living seat."""
        if self.legal:
            return [s for s in self.legal if s != ABSTAIN]
        return sorted(self.alive - {self.me})

    def alive_at_vote(self, day: int) -> set[int]:
        """Who could vote (and be voted for) on ``day``: everyone not dead before that day's vote."""
        out = set(range(N_SEATS))
        for d in self.dead.values():
            if d.cause == "night_kill" and d.day <= day:
                out.discard(d.seat)
            elif d.cause != "night_kill" and d.day < day:
                out.discard(d.seat)
        return out

    def lynch_free_days(self) -> int:
        """Consecutive most recent days (before the current one) that ended without a vote-out."""
        n = 0
        for d in range(self.day - 1, 0, -1):
            if d in self.lynched and self.lynched[d] is None:
                n += 1
            else:
                break
        return n


def seat_of(x: Any, names: dict[str, int] | None = None) -> int | None:
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x if 0 <= x < N_SEATS else None
    if isinstance(x, dict):
        for key in ("player", "name", "seat", "position", "id"):
            if key in x:
                return seat_of(x[key], names)
        return None
    s = str(x).strip()
    if names and s in names:
        return names[s]
    m = _NAME_RE.search(s)
    if m and 0 <= int(m.group(1)) < N_SEATS:
        return int(m.group(1))
    return None


def _day(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def parse_dead(raw: Any, names: dict[str, int]) -> dict[int, Death]:
    out: dict[int, Death] = {}
    items = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, (list, tuple)) else []
    for item in items:
        if isinstance(item, dict):
            seat = seat_of(item, names)
            if seat is None:
                continue
            out[seat] = Death(seat, role_of(item.get("role")), str(item.get("cause") or ""), _day(item.get("day")))
        else:
            seat = seat_of(item, names)
            if seat is not None:
                out[seat] = Death(seat, None, "", 0)
    return out


def _vote_target(x: Any, names: dict[str, int]) -> tuple[bool, int | None]:
    """(is it a vote at all, target seat or None for an abstention)."""
    if x is None:
        return True, None
    if isinstance(x, str) and x.strip().lower() in ("abstain", "none", "7", "skip", ""):
        return True, None
    seat = seat_of(x, names)
    if seat is None:
        return (isinstance(x, int) and x == ABSTAIN), None
    return True, seat


def _pairs(votes: Any, names: dict[str, int]) -> list[tuple[int, int | None]]:
    out: list[tuple[int, int | None]] = []
    if isinstance(votes, dict):
        for k, v in votes.items():
            voter = seat_of(k, names)
            ok, target = _vote_target(v, names)
            if voter is not None and ok:
                out.append((voter, target))
    elif isinstance(votes, list):
        for item in votes:
            voter = target_raw = None
            if isinstance(item, dict):
                voter = seat_of(item.get("voter", item.get("player", item.get("from"))), names)
                target_raw = item.get("target", item.get("vote", item.get("to")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                voter, target_raw = seat_of(item[0], names), item[1]
            ok, target = _vote_target(target_raw, names)
            if voter is not None and ok:
                out.append((voter, target))
    return out


def parse_votes(raw: Any, names: dict[str, int]) -> tuple[list[tuple[int, int, int | None]], dict[int, int | None]]:
    votes: list[tuple[int, int, int | None]] = []
    lynched: dict[int, int | None] = {}
    if isinstance(raw, dict):
        entries = []
        for k, v in raw.items():
            if isinstance(v, dict):
                entries.append({"day": k, **v})
            else:
                entries.append({"day": k, "votes": v})
    else:
        entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, (list, tuple)) else []
    for i, e in enumerate(entries, start=1):
        day = _day(e.get("day"), i)
        tally = e.get("votes", e.get("tally", e.get("ballots")))
        for voter, target in _pairs(tally, names):
            votes.append((day, voter, target))
        for key in ("eliminated", "lynched", "outcome", "result"):
            if key in e:
                lynched[day] = seat_of(e[key], names) if e[key] not in (None, "none", "None", "") else None
                break
        else:
            lynched.setdefault(day, None)
    return votes, lynched


def read_snapshot(state: Any, context: Any) -> Snapshot:
    raw = state.raw if isinstance(state.raw, dict) else {}
    gs = raw.get("game_state") if isinstance(raw.get("game_state"), dict) else {}
    names: dict[str, int] = {}
    for p in raw.get("players") if isinstance(raw.get("players"), list) else []:
        if isinstance(p, dict) and "name" in p and "position" in p:
            names[str(p["name"])] = int(p["position"])
    me = raw.get("your_position")
    if me is None:
        me = getattr(context, "seat_position", None)
    me = int(me) if me is not None else 0
    obs = state.observation if isinstance(state.observation, str) else ""
    role = None
    m = re.search(r"your role\s*(?:is)?\s*[:\-]?\s*([A-Za-z]+)", obs, re.I)
    if m:
        role = role_of(m.group(1))
    partner = None
    if role == WOLF:
        m = re.search(r"fellow\s+(?:wolf|wolves|werewolf|werewolves)\s*(?:is|are)?\s*:?\s*([^\n]*)", obs, re.I) or re.search(
            r"(?:partner|other wolf|ally)\s*(?:is)?\s*:?\s*([^\n]*)", obs, re.I)
        if m:
            seats = [int(x) for x in re.findall(r"(\d)", m.group(1)) if int(x) != me and int(x) < N_SEATS]
            partner = seats[0] if seats else None
    dead = parse_dead(gs.get("dead"), names)
    alive_raw = gs.get("alive")
    if isinstance(alive_raw, (list, tuple)):
        alive = {s for s in (seat_of(a, names) for a in alive_raw) if s is not None}
    else:
        alive = set(range(N_SEATS)) - set(dead)
    votes, lynched = parse_votes(gs.get("vote_history"), names)
    legal = []
    for a in state.legal_actions or []:
        try:
            legal.append(int(a.action_id))
        except (TypeError, ValueError):
            pass
    return Snapshot(
        me=me, role=role, partner=partner, day=_day(gs.get("day"), 1), sub_phase=gs.get("phase"),
        alive=alive, dead=dead, votes=votes, lynched=lynched, observation=obs,
        messages=list(state.new_messages or []), legal=legal, eliminated=bool(raw.get("eliminated")),
        winner=gs.get("winner"),
    )
