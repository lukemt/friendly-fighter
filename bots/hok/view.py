"""Reading an Honor of Kings state: everything the team policy needs, parsed
defensively.

The platform's state (``/skill/hok`` section 3, upstream's ``tests/test_hok.py``
fixtures) carries, for a team agent:

- ``seats`` (our five), ``camp`` (1 or 2), ``state_version`` (steps played),
  ``max_steps``, ``time_limit_s``/``seconds_left``, ``pacing``;
- ``observation.seats["<seat>"].summary``: ``my_hero`` (``hp``, ``max_hp``,
  ``level``, ``x``, ``z``, ``kills``, ``deaths``, ``money``, ``ep``,
  ``revive_time``), ``other_heroes`` (fogged ones: ``visible: false``, no
  position), ``structures`` (``camp``, ``type``, ``hp``, ``max_hp``, ``x``,
  ``z``), ``frame_no``;
- ``simultaneous``: ``your_pending_seats``, ``standing_orders``;
- ``legal_actions``: per pending hero, ``s<seat>:macro_*`` and ``s<seat>:button_N``
  entries; a macro's label ends ``(nearest: <what>, <distance> away)``.

Nothing here raises on a missing or oddly typed field: an absent value is
``None`` (or empty), and the policy treats "unknown" conservatively. Pure
functions only; no memory between states (that is the policy's).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from altruagent import GameState, LegalAction

GAME_TYPE = "honor_of_kings_5v5"
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")
SKILLS = ("macro_skill1", "macro_skill2", "macro_skill3")
# Orders the policy may give besides macros; the agent turns them into a listed raw button.
HOLD = "hold"  # the do-nothing button (live: s<n>:button_1 "NoOp"); a raw action clears the standing order
HEAL = "heal"  # the heal summoner skill (live: s<n>:button_11 "HealSkill")
RAW_ORDERS = (HOLD, HEAL)
STEP_SECONDS = 0.2  # one step is 3 frames, 0.2 s of game time (/skill/hok section 1)
FRAMES_PER_SECOND = 15.0

_SEAT_ID = re.compile(r"^s(\d+):(.+)$")
_DISTANCE = re.compile(r"(\d+(?:\.\d+)?)\s*away")
_NEAREST = re.compile(r"nearest:\s*([^()]*?),\s*\d+(?:\.\d+)?\s*away")
_TOWARD = re.compile(r"toward\s+(.*?)\s*\(\s*\d+(?:\.\d+)?\s*away")
_HOLD_LABELS = ("noop", "no-op", "no op", "none", "nothing", "idle")


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return None


def distance(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float:
    if a is None or b is None:
        return math.inf
    return math.hypot(a[0] - b[0], a[1] - b[1])


def label_distance(label: str | None) -> float:
    """A macro label's target distance (``"... (nearest: x, 4123 away)"``); inf without one."""
    m = _DISTANCE.search(label or "")
    return float(m.group(1)) if m else math.inf


def label_target(label: str | None) -> str:
    """What a macro label names as its target, lowercased; "" without one. Live labels:
    ``"Basic attack (nearest: enemy hero 169, 4123 away)"``,
    ``"Cast skill 1 toward enemy hero 169 (4123 away)"``,
    ``"Move toward the nearest enemy tower (9000 away)"``."""
    m = _NEAREST.search(label or "") or _TOWARD.search(label or "")
    return m.group(1).strip().lower() if m else ""


@dataclass
class Offer:
    """One legal-action entry for one hero."""

    seat: int
    base: str                 # "macro_attack", "button_0", ...
    action: LegalAction
    label: str = ""
    dist: float = math.inf    # the target distance from the label (macros)
    target: str = ""          # what the label names as the target

    @property
    def payload(self) -> dict:
        return _dict(self.action.input.get("action"))

    def fill_fields(self, *, centred: bool = False) -> dict | None:
        """A complete raw action from this entry: the listed constant fields plus one legal
        value of each ``legal_*`` field (the first; ``centred``: the middle one of an
        ``offset`` list, so an aimed skill lands on the hero itself); None when a field
        has no value."""
        out: dict[str, Any] = {}
        for key, value in self.payload.items():
            if key.startswith("legal_"):
                values = _list(value)
                if not values:
                    return None
                name = key[len("legal_"):]
                out[name] = values[len(values) // 2] if centred and "offset" in name else values[0]
            elif key != "seat":
                out[key] = value
        out["seat"] = self.seat
        return out

    @property
    def needs_fields(self) -> bool:
        return any(k.startswith("legal_") for k in self.payload)


@dataclass
class SeatOffers:
    macros: dict[str, Offer] = field(default_factory=dict)
    buttons: dict[str, Offer] = field(default_factory=dict)

    def all(self) -> list[Offer]:
        return list(self.macros.values()) + list(self.buttons.values())

    def hold(self) -> Offer | None:
        """The do-nothing button: one whose label says so (live: ``button_1`` "NoOp"), else
        ``button_0`` (upstream's fixtures: "None"); only one with no fields to fill."""
        named = [o for o in self.buttons.values() if not o.needs_fields
                 and any(w in o.label.lower().rsplit("]", 1)[-1] for w in _HOLD_LABELS)]
        if named:
            return min(named, key=lambda o: o.base)
        o = self.buttons.get("button_0")
        return o if o is not None and not o.needs_fields else None

    def button_named(self, *words: str) -> Offer | None:
        """A raw button whose label names one of ``words`` (e.g. "healskill")."""
        for o in sorted(self.buttons.values(), key=lambda o: o.base):
            label = o.label.lower().rsplit("]", 1)[-1].replace(" ", "")
            if any(w in label for w in words):
                return o
        return None

    def raw_for(self, order: str) -> Offer | None:
        if order == HOLD:
            return self.hold()
        if order == HEAL:
            return self.button_named("healskill")
        return self.buttons.get(order)


@dataclass
class Hero:
    seat: int | None
    camp: int | None
    hp: float | None
    max_hp: float | None
    x: float | None
    z: float | None
    visible: bool = True
    runtime_id: Any = None
    hero_id: Any = None
    level: float | None = None
    revive_time: float | None = None
    kills: float | None = None
    deaths: float | None = None
    money: float | None = None
    ep: float | None = None

    @property
    def pos(self) -> tuple[float, float] | None:
        if self.x is None or self.z is None:
            return None
        return (self.x, self.z)

    @property
    def hp_frac(self) -> float | None:
        if self.hp is None or not self.max_hp:
            return None
        return max(0.0, min(1.0, self.hp / self.max_hp))

    @property
    def dead(self) -> bool:
        if self.hp is not None and self.hp <= 0:
            return True
        return bool(self.revive_time and self.revive_time > 0 and (self.hp is None or self.hp <= 0))

    @property
    def key(self) -> Any:
        return self.runtime_id if self.runtime_id is not None else ("hero", self.hero_id)


@dataclass
class Structure:
    camp: int | None
    type: str
    hp: float | None
    max_hp: float | None
    x: float | None
    z: float | None

    @property
    def kind(self) -> str:
        t = self.type.upper()
        if "CRYSTAL" in t or "BASE" in t or "NEXUS" in t:
            return "crystal"
        if "SPRING" in t or "SPAWN" in t or "FOUNTAIN" in t:
            return "spawn"
        if "TOWER" in t or "TURRET" in t:
            return "tower"
        return "other"

    @property
    def pos(self) -> tuple[float, float] | None:
        if self.x is None or self.z is None:
            return None
        return (self.x, self.z)

    @property
    def key(self) -> tuple:
        return (self.camp, self.type, None if self.x is None else round(self.x), None if self.z is None else round(self.z))

    @property
    def alive(self) -> bool:
        return self.hp is None or self.hp > 0

    @property
    def hp_frac(self) -> float | None:
        if self.hp is None or not self.max_hp:
            return None
        return max(0.0, min(1.0, self.hp / self.max_hp))


@dataclass
class TeamView:
    """One state, read for the team policy."""

    version: int
    phase: str
    realtime: bool
    camp: int | None
    seats: list[int]
    pending: list[int]
    standing: dict[int, str]
    offers: dict[int, SeatOffers]
    heroes: dict[int, Hero]             # our heroes by seat (from each seat's my_hero)
    enemies: list[Hero]                 # enemy heroes we know of (visible or not)
    structures: list[Structure]
    frame_no: int | None
    max_steps: int | None
    seconds_left: float | None
    time_limit_s: float | None
    step_interval_s: float | None
    standing_known: bool = True         # the state reports standing orders at all

    @property
    def game_seconds(self) -> float:
        if self.frame_no is not None and self.frame_no > 0:
            return self.frame_no / FRAMES_PER_SECOND
        return self.version * STEP_SECONDS

    def seconds_to_limit(self) -> float | None:
        """Wall seconds until the step or time limit, whichever comes first (None if unknown)."""
        left: list[float] = []
        if self.seconds_left is not None:
            left.append(self.seconds_left)
        if self.max_steps:
            per_step = self.step_interval_s if self.realtime and self.step_interval_s else STEP_SECONDS
            left.append(max(0, self.max_steps - self.version) * per_step)
        return min(left) if left else None

    def visible_enemies(self) -> list[Hero]:
        return [e for e in self.enemies if e.visible and e.pos is not None and not e.dead]

    def enemy_structures(self) -> list[Structure]:
        return [s for s in self.structures if self.camp is not None and s.camp is not None and s.camp != self.camp]

    def own_structures(self) -> list[Structure]:
        return [s for s in self.structures if self.camp is not None and s.camp == self.camp]


# -- parsing ------------------------------------------------------------------------------------


def is_hok(state: GameState, game_type: str | None) -> bool:
    """Whether this is an Honor of Kings match: by game type, or by its unmistakable
    shape (seat-tagged ``s<n>:macro_*`` actions, per-seat summaries)."""
    if game_type == GAME_TYPE:
        return True
    raw = state.raw if isinstance(state.raw, dict) else {}
    if raw.get("game_type") == GAME_TYPE:
        return True
    if game_type:  # another named game
        return False
    if any(_SEAT_ID.match(a.action_id or "") and "macro_" in (a.action_id or "") for a in state.legal_actions or []):
        return True
    obs = raw.get("observation")
    return isinstance(obs, dict) and isinstance(obs.get("seats"), dict) and isinstance(raw.get("simultaneous"), dict)


def is_realtime(raw: dict) -> bool:
    pacing = _dict(raw.get("pacing"))
    return pacing.get("mode") == "realtime"


def offers_by_seat(actions: list[LegalAction]) -> dict[int, SeatOffers]:
    out: dict[int, SeatOffers] = {}
    for action in actions or []:
        if not isinstance(action, LegalAction):
            continue
        m = _SEAT_ID.match(action.action_id or "")
        if not m:
            continue
        seat, base = int(m.group(1)), m.group(2)
        label = action.label if isinstance(action.label, str) else ""
        entry = out.setdefault(seat, SeatOffers())
        offer = Offer(seat=seat, base=base, action=action, label=label,
                      dist=label_distance(label), target=label_target(label))
        if base in MACROS:
            entry.macros.setdefault(base, offer)
        else:
            entry.buttons.setdefault(base, offer)
    return out


def normalize_order(value: Any) -> str | None:
    """A standing order as a macro id: ``"macro_attack"``, also from ``"s3:macro_attack"``
    or ``{"macro": "macro_attack"}``; None for anything else."""
    if isinstance(value, dict):
        value = value.get("macro") or value.get("action_id")
    if not isinstance(value, str):
        return None
    m = _SEAT_ID.match(value)
    return m.group(2) if m else value


def _hero(data: dict, seat: int | None) -> Hero:
    return Hero(
        seat=seat,
        camp=_int(data.get("camp")),
        hp=_num(data.get("hp")),
        max_hp=_num(data.get("max_hp")),
        x=_num(data.get("x")),
        z=_num(data.get("z")),
        visible=data.get("visible") is not False,
        runtime_id=data.get("runtime_id"),
        hero_id=data.get("hero_id"),
        level=_num(data.get("level")),
        revive_time=_num(data.get("revive_time")),
        kills=_num(data.get("kills")),
        deaths=_num(data.get("deaths")),
        money=_num(data.get("money")),
        ep=_num(data.get("ep")),
    )


def summary_of(raw: dict, seat: int) -> dict:
    seats = _dict(_dict(raw.get("observation")).get("seats"))
    entry = seats.get(str(seat))
    if entry is None:
        entry = seats.get(seat)  # type: ignore[call-overload]
    return _dict(_dict(entry).get("summary"))


def read_view(state: GameState) -> TeamView:
    raw = state.raw if isinstance(state.raw, dict) else {}
    simultaneous = _dict(raw.get("simultaneous"))
    offers = offers_by_seat(state.legal_actions or [])

    seats = [s for s in (_int(x) for x in _list(raw.get("seats"))) if s is not None]
    if not seats:
        seats = sorted(offers) or sorted(
            s for s in (_int(k) for k in _dict(_dict(raw.get("observation")).get("seats"))) if s is not None)

    pending_raw = simultaneous.get("your_pending_seats")
    if isinstance(pending_raw, list):
        pending = [s for s in (_int(x) for x in pending_raw) if s is not None]
    else:
        pending = sorted(offers)

    standing: dict[int, str] = {}
    for k, v in _dict(simultaneous.get("standing_orders")).items():
        seat, order = _int(k), normalize_order(v)
        if seat is not None and order:
            standing[seat] = order

    heroes: dict[int, Hero] = {}
    enemies: dict[Any, Hero] = {}
    structures: dict[tuple, Structure] = {}
    frame_no: int | None = None
    camp = _int(raw.get("camp"))
    for seat in seats:
        summary = summary_of(raw, seat)
        if not summary:
            continue
        me = _dict(summary.get("my_hero"))
        if me:
            heroes[seat] = _hero(me, seat)
            if camp is None and heroes[seat].camp is not None:
                camp = heroes[seat].camp
        f = _int(summary.get("frame_no"))
        if f is not None:
            frame_no = f if frame_no is None else max(frame_no, f)
        listed = _list(summary.get("structures"))
        if structures and len(listed) <= len(structures):
            listed = []  # every seat sees the same structures: read them once
        for s in listed:
            if not isinstance(s, dict):
                continue
            st = Structure(camp=_int(s.get("camp")), type=str(s.get("type") or ""), hp=_num(s.get("hp")),
                           max_hp=_num(s.get("max_hp")), x=_num(s.get("x")), z=_num(s.get("z")))
            structures.setdefault(st.key, st)
    if camp is None and seats:
        camp = 1 if min(seats) < 5 else 2
    own_ids = {h.runtime_id for h in heroes.values() if h.runtime_id is not None}
    own_hero_ids = {h.hero_id for h in heroes.values() if h.hero_id is not None}
    for seat in seats:
        for other in _list(summary_of(raw, seat).get("other_heroes")):
            if not isinstance(other, dict):
                continue
            if camp is not None and _int(other.get("camp")) == camp:
                continue  # an ally (our own heroes come from their own my_hero)
            rid = other.get("runtime_id")
            if rid is not None and not isinstance(rid, (dict, list)):
                seen = enemies.get(rid)
                if seen is not None and seen.visible and seen.pos is not None:
                    continue  # already read from another seat's view
            hero = _hero(other, None)
            if hero.camp is not None and camp is not None and hero.camp == camp:
                continue
            if hero.camp is None and (hero.runtime_id in own_ids or (hero.runtime_id is None and hero.hero_id in own_hero_ids)):
                continue
            known = enemies.get(hero.key)
            # One seat may see an enemy another one doesn't (the fog is per camp in the
            # game, but take the best view we have).
            if known is None or (not known.visible and hero.visible) or (known.pos is None and hero.pos is not None):
                enemies[hero.key] = hero

    pacing = _dict(raw.get("pacing"))
    return TeamView(
        version=_int(raw.get("state_version")) if _int(raw.get("state_version")) is not None else int(state.state_version or 0),
        phase=str(raw.get("phase") or state.phase or ""),
        realtime=is_realtime(raw),
        camp=camp,
        seats=seats,
        pending=pending,
        standing=standing,
        offers=offers,
        heroes=heroes,
        enemies=list(enemies.values()),
        structures=list(structures.values()),
        frame_no=frame_no,
        max_steps=_int(raw.get("max_steps")),
        seconds_left=_num(raw.get("seconds_left")),
        time_limit_s=_num(raw.get("time_limit_s")),
        step_interval_s=_num(pacing.get("step_interval_s")),
        standing_known=isinstance(simultaneous.get("standing_orders"), dict),
    )
