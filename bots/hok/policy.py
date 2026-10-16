"""The team policy: one macro per hero, from fixed rules, in well under a millisecond.

What it does, in order of precedence for each living hero:

1. **Retreat on low HP and come back healed** (hysteresis: out below
   ``retreat_hp``, back only at ``return_hp``), and **evade** for a moment
   when the hero is losing HP fast near a threat (an enemy hero or an enemy
   tower): the time-to-death at the current damage rate is below
   ``danger_ttd`` seconds. This is Red Alert's "a unit that dies costs its full
   price": a death gives the enemy gold and kills, and takes the hero out for
   its revive time.
2. **Fight together, not one by one.** An enemy hero in range is fought only
   when our heroes around this one are at least as strong as theirs (HP-
   weighted, fogged enemies seen lately counted too). Outnumbered heroes back
   off, unless they stand under our own tower.
3. **Skills on heroes.** A skill whose label names an enemy hero (or whose
   distance matches one) is cast first, the ultimate (skill 3) included; then
   the basic attack. Skills on minions only while no enemy hero is near.
4. **No dives.** Never chase an enemy hero under its tower, and never walk into
   an enemy tower's range with fewer than ``min_pushers`` heroes there (the
   tower kills a lone hero), unless the tower is almost dead.
5. **Defend:** one of our structures losing HP pulls the healthy heroes near it
   back to fight there.
6. **Push together:** the hero far ahead of the team waits (the do-nothing
   button) for the others; otherwise farm what is in range, then advance.
7. **Play for the tiebreak** in the last ``endgame_seconds``: ahead on damage
   to structures, protect our own (pull back, fight only near home); level or
   behind, all in (no waiting, retreat only when nearly dead).

The macros aim at the *nearest* target and walk the hero by themselves, so
the policy decides the hero's intent (fight, push, back off, wait) and the
server does the aiming. Everything it knows comes from the state (``view``);
its memory (HP history, retreat state, structure damage, last-seen enemies)
only smooths that. A fresh policy after a crash plays at once.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Any

from .view import HEAL, HOLD, SKILLS, Hero, Offer, SeatOffers, Structure, TeamView, distance

# Priorities: lower is sent first when several heroes need a new order.
P_SAVE, P_SKILL_HERO, P_HIT_HERO, P_POSITION, P_ATTACK, P_SKILL_UNIT, P_HOLD, P_ADVANCE = range(8)


@dataclass
class Knobs:
    """Strategy settings. Distances are in game units (the guide's labels: "4123 away");
    upstream's example fights what is closer than 9000."""

    retreat_hp: float = 0.25          # below this share of max HP, a threatened hero escapes...
    calm_s: float = 3.0               # ...until it has been out of danger this long (game seconds)
    heal_at_home: bool = False        # the old way: walk home below retreat_hp, back at return_hp.
                                      # Live (e5f159ed): home heals ~0.25%/s, so that took minutes;
                                      # a death costs a 5-25 s revive and comes back at full HP
    return_hp: float = 0.60
    heal_hp: float = 0.40             # the heal summoner skill, when listed, below this share of HP
                                      # and threatened
    allin_retreat_hp: float = 0.12    # the retreat line when all in
    danger_ttd: float = 2.0           # seconds to death at the current damage rate: evade
    danger_hp: float = 0.6            # ...but only below this share of HP
    evade_seconds: float = 2.0        # an evade lasts this long (game seconds)
    tower_aggro_rate: float = 0.0     # under an enemy tower, losing this share of max HP a second with
                                      # no enemy hero near: step out (0 = off: on the sim, stepping out
                                      # cost more push time than it saved heroes)
    tower_aggro_hp: float = 0.75      # ...once below this share of HP
    engage_range: float = 9000.0      # an enemy hero this close is in the fight
    assist_range: float = 13000.0     # heroes this close count for each side's strength
    attack_range: float = 9000.0      # a macro target this close is worth hitting
    skill_range: float = 9000.0
    tower_range: float = 9000.0       # an enemy tower hits what comes this close
    min_pushers: int = 2              # heroes needed at an enemy tower to hit it (sim: 2 > 3 > 1)
    low_tower: float = 0.15           # a tower below this share of HP is taken whatever
    outnumber_margin: float = 0.8     # back off when their strength > ours + this
    fog_weight: float = 0.5           # an enemy seen in the last few seconds nearby, now fogged
    skills_on_minions: bool = True
    attack_monsters: bool = False     # a macro whose label names a jungle monster or boss isn't used
    hold: bool = True                 # the do-nothing button to wait (else keep advancing)
    group_lead: float = 7000.0        # a hero this far ahead of the team's median waits
    patience_s: float = 15.0          # ...but never longer than this (game seconds), then it goes on
    defend_range: float = 40000.0     # heroes this close to a structure enemy heroes are hitting defend it
    defend_hp: float = 0.45           # ...when they have this share of HP
    endgame_seconds: float = 60.0     # tiebreak play in the last minute
    protect_lead: float = 0.5         # "ahead": structure damage lead > this x a tower's max HP
    window_dead: int = 3              # this many enemy heroes dead: a push window, no waiting


def knobs_from_dict(wanted: dict | None) -> Knobs:
    knobs = Knobs()
    for f in fields(Knobs):
        if wanted and f.name in wanted:
            current = getattr(knobs, f.name)
            setattr(knobs, f.name, type(current)(wanted[f.name]))
    return knobs


@dataclass
class Choice:
    base: str                     # a macro id or "button_0"
    reason: str
    priority: int
    alternatives: list[str] = field(default_factory=list)


@dataclass
class _Ctx:
    t: float
    posture: str                          # "normal" | "protect" | "allin"
    alive: dict[int, Hero]
    enemies: list[Hero]                   # visible, positioned, alive
    ghosts: list[tuple[float, float]]     # recently seen enemies now in the fog
    enemy_towers: list[Structure]
    own_towers: list[Structure]
    threatened: list[Structure]           # our structures losing HP lately
    fronts: dict[int, float]              # seat -> distance to its nearest enemy structure
    home: tuple[float, float] | None
    retreat_hp: float
    min_pushers: int
    window: bool = False                  # enough enemy heroes dead: push now


class TeamPolicy:
    def __init__(self, knobs: Knobs | None = None) -> None:
        self.knobs = knobs or Knobs()
        self._hp: dict[int, deque] = {}
        self._max_hp: dict[int, float] = {}
        self._retreating: dict[int, bool] = {}
        self._escape_until: dict[int, float] = {}
        self._evade_until: dict[int, float] = {}
        self._holding: dict[int, bool] = {}
        self._waiting_since: dict[int, float] = {}
        self._struct_max: dict[tuple, float] = {}
        self._struct_last: dict[tuple, float] = {}
        self._struct_camp: dict[tuple, int | None] = {}
        self._struct_hit: dict[tuple, float] = {}
        self._enemy_seen: dict[Any, tuple[float, tuple[float, float]]] = {}
        self._home: tuple[float, float] | None = None
        self._kills_seen: float | None = None
        self._enemy_last: dict[Any, tuple[float, float]] = {}     # key -> (t last seen, hp share)
        self._enemy_dead_until: dict[Any, float] = {}
        self._observed_version: int | None = None
        self._posture = "normal"
        self.enemies_dead = 0
        self.posture_changes: list[tuple[float, str]] = []

    # -- memory ------------------------------------------------------------------------------

    def observe(self, view: TeamView) -> None:
        """Update the memory from one state (once per state_version)."""
        if self._observed_version == view.version:
            return
        self._observed_version = view.version
        t = view.game_seconds
        for seat, hero in view.heroes.items():
            hist = self._hp.setdefault(seat, deque(maxlen=40))
            if hero.max_hp:
                self._max_hp[seat] = hero.max_hp
            if hero.hp is not None:
                if hist and hist[-1][0] > t:
                    hist.clear()  # time went backwards: a new match or a restart
                hist.append((t, hero.hp))
            while hist and t - hist[0][0] > 3.0:
                hist.popleft()
        seen_now: set[tuple] = set()
        for s in view.structures:
            seen_now.add(s.key)
            self._struct_camp[s.key] = s.camp
            top = max(x for x in (s.max_hp, s.hp, self._struct_max.get(s.key)) if x is not None) \
                if any(x is not None for x in (s.max_hp, s.hp, self._struct_max.get(s.key))) else None
            if top is not None:
                self._struct_max[s.key] = top
            if s.hp is not None:
                last = self._struct_last.get(s.key)
                if last is not None and s.hp < last - 1e-6:
                    self._struct_hit[s.key] = t
                self._struct_last[s.key] = s.hp
        # A structure that left the list low on HP was destroyed; one that left healthy
        # may only be out of sight: keep its last HP.
        for key, last in list(self._struct_last.items()):
            if key not in seen_now:
                top = self._struct_max.get(key)
                if top and last <= 0.25 * top:
                    self._struct_last[key] = 0.0
        # Enemy deaths: shown dead, or gone from sight low on HP just as our kill count rose.
        revive_guess = 8.0 + t / 20.0
        vanished: list[tuple[float, Any]] = []
        for e in view.enemies:
            if e.visible and e.pos is not None and not e.dead:
                self._enemy_seen[e.key] = (t, e.pos)
                self._enemy_last[e.key] = (t, e.hp_frac if e.hp_frac is not None else 1.0)
                self._enemy_dead_until.pop(e.key, None)
            elif e.dead:
                self._enemy_seen.pop(e.key, None)
                self._enemy_dead_until[e.key] = max(self._enemy_dead_until.get(e.key, 0.0), t + revive_guess)
            else:
                last = self._enemy_last.get(e.key)
                if last is not None and t - last[0] <= 2.0 and last[1] <= 0.35 \
                        and self._enemy_dead_until.get(e.key, -1.0) < t:
                    vanished.append((last[0], e.key))
        kills = sum(h.kills or 0 for h in view.heroes.values())
        if self._kills_seen is not None and kills > self._kills_seen:
            for _, key in sorted(vanished, reverse=True)[: int(kills - self._kills_seen)]:
                self._enemy_dead_until[key] = t + revive_guess
                self._enemy_seen.pop(key, None)
        if self._kills_seen is None or kills >= self._kills_seen:
            self._kills_seen = kills
        if self._home is None:
            own = [s for s in view.own_structures() if s.pos and s.kind in ("spawn", "crystal")]
            spawn = [s for s in own if s.kind == "spawn"]
            if spawn or own:
                self._home = (spawn or own)[0].pos
            else:
                pts = [h.pos for h in view.heroes.values() if h.pos]
                if pts and t < 5.0:
                    self._home = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def score(self, view: TeamView) -> tuple[float, float]:
        """(damage we dealt to enemy structures, damage we took), from what we have seen."""
        dealt = taken = 0.0
        for key, last in self._struct_last.items():
            top = self._struct_max.get(key)
            if top is None:
                continue
            camp = self._struct_camp.get(key)
            if camp is None or view.camp is None:
                continue
            kind = Structure(camp, key[1], None, None, None, None).kind
            if kind == "spawn":
                continue
            lost = max(0.0, top - last)
            if camp == view.camp:
                taken += lost
            else:
                dealt += lost
        return dealt, taken

    def _posture_for(self, view: TeamView) -> str:
        left = view.seconds_to_limit()
        if left is None or left > self.knobs.endgame_seconds:
            return "normal"
        dealt, taken = self.score(view)
        tops = [v for k, v in self._struct_max.items()
                if Structure(None, k[1], None, None, None, None).kind == "tower"]
        unit = statistics.median(tops) if tops else 0.0
        if dealt - taken > self.knobs.protect_lead * unit and dealt > taken:
            return "protect"
        return "allin"

    def _loss_rate(self, seat: int, t: float, window: float = 1.0) -> float:
        """Share of max HP lost per second over the last ``window`` seconds (0 if none)."""
        hist = self._hp.get(seat)
        if not hist or len(hist) < 2:
            return 0.0
        recent = [(tt, hp) for tt, hp in hist if t - tt <= window]
        if len(recent) < 2:
            return 0.0
        (t0, hp0), (t1, hp1) = recent[0], recent[-1]
        top = self._max_hp.get(seat) or 0.0
        if t1 - t0 < 0.3 or hp1 >= hp0 or top <= 0:
            return 0.0
        return (hp0 - hp1) / top / (t1 - t0)

    def _ttd(self, seat: int, t: float) -> float:
        hist = self._hp.get(seat)
        if not hist or len(hist) < 2:
            return math.inf
        recent = [(tt, hp) for tt, hp in hist if t - tt <= 1.5]
        if len(recent) < 2:
            return math.inf
        (t0, hp0), (t1, hp1) = recent[0], recent[-1]
        if t1 - t0 < 0.3 or hp1 >= hp0:
            return math.inf
        dps = (hp0 - hp1) / (t1 - t0)
        return hp1 / dps if dps > 0 else math.inf

    # -- the plan ----------------------------------------------------------------------------

    def plan(self, view: TeamView) -> dict[int, Choice]:
        """A choice for every hero that has macros on offer now."""
        self.observe(view)
        k = self.knobs
        t = view.game_seconds
        posture = self._posture_for(view)
        if posture != self._posture:
            self._posture = posture
            self.posture_changes.append((t, posture))
        alive = {s: h for s, h in view.heroes.items() if not h.dead and h.pos is not None}
        enemies = view.visible_enemies()
        visible_keys = {e.key for e in enemies}
        ghosts = [pos for key, (seen, pos) in self._enemy_seen.items()
                  if key not in visible_keys and t - seen <= 4.0]
        enemy_structs = [s for s in view.enemy_structures() if s.alive and s.pos and s.kind in ("tower", "crystal")]
        enemy_towers = [s for s in enemy_structs if s.kind == "tower"]
        own_towers = [s for s in view.own_structures() if s.alive and s.pos and s.kind in ("tower", "crystal")]
        # Ours under attack by enemy heroes (minions alone are left to our own minions).
        threatened = [s for s in own_towers if t - self._struct_hit.get(s.key, -1e9) <= 3.0
                      and any(distance(s.pos, e.pos) <= k.tower_range + 5000 for e in enemies)]
        fronts = {}
        for seat, hero in alive.items():
            fronts[seat] = min((distance(hero.pos, s.pos) for s in enemy_structs), default=math.inf)
        available = sum(1 for s2, h in alive.items() if not self._retreating.get(s2)
                        and (h.hp_frac is None or h.hp_frac >= 0.3))
        enemies_dead = sum(1 for until in self._enemy_dead_until.values() if until > t)
        self.enemies_dead = enemies_dead
        window = enemies_dead >= k.window_dead
        ctx = _Ctx(
            t=t, posture=posture, alive=alive, enemies=enemies, ghosts=ghosts,
            enemy_towers=enemy_towers, own_towers=own_towers, threatened=threatened, fronts=fronts,
            home=self._home,
            retreat_hp=k.allin_retreat_hp if posture == "allin" else k.retreat_hp,
            min_pushers=1 if posture == "allin" or window else max(1, min(k.min_pushers, available)),
            window=window,
        )
        out: dict[int, Choice] = {}
        for seat, offers in view.offers.items():
            if not offers.macros:
                continue
            hero = view.heroes.get(seat)
            choice = self._hero_choice(seat, hero, offers, ctx)
            if choice is not None:
                out[seat] = choice
        return out

    def _hero_choice(self, seat: int, hero: Hero | None, offers: SeatOffers, c: _Ctx) -> Choice | None:
        k = self.knobs
        m = offers.macros
        if hero is None or hero.pos is None:
            # No view of this hero: keep it useful without knowing more.
            return _first(m, [("macro_attack", "no view: attack"), ("macro_advance", "no view: advance")], P_ADVANCE)
        pos = hero.pos
        hp = hero.hp_frac if hero.hp_frac is not None else 1.0

        # 1. Low on HP: escape while threatened (or, the old way, go home and heal), and evade
        #    fast HP loss near a threat.
        near = [e for e in c.enemies if distance(pos, e.pos) <= k.engage_range]
        in_enemy_tower = any(distance(pos, s.pos) <= k.tower_range for s in c.enemy_towers)
        threatened = bool(near) or (in_enemy_tower and self._loss_rate(seat, c.t) > 0.02)
        if k.heal_at_home:
            if hp < c.retreat_hp:
                self._retreating[seat] = True
            elif self._retreating.get(seat) and hp >= k.return_hp:
                self._retreating[seat] = False
        else:
            if hp < c.retreat_hp and threatened:
                self._escape_until[seat] = c.t + k.calm_s
            self._retreating[seat] = c.t < self._escape_until.get(seat, -1e9)
        if hp < k.heal_hp and (threatened or self._retreating.get(seat)) and offers.raw_for(HEAL) is not None:
            return Choice(HEAL, "low hp: heal skill", P_SAVE, ["macro_retreat", "macro_attack"])
        if (hp < k.danger_hp and (near or in_enemy_tower) and c.posture != "allin"
                and self._ttd(seat, c.t) < k.danger_ttd):
            self._evade_until[seat] = c.t + k.evade_seconds
        elif (in_enemy_tower and not near and hp < k.tower_aggro_hp and k.tower_aggro_rate > 0
              and self._loss_rate(seat, c.t) >= k.tower_aggro_rate):
            self._evade_until[seat] = c.t + k.evade_seconds  # the tower is on this hero: step out
        if self._retreating.get(seat) or near:
            self._waiting_since.pop(seat, None)  # a wait starts afresh after a fight or a retreat
        if self._retreating.get(seat):
            self._holding[seat] = False
            return self._go_home(m, "low hp: escape" if not k.heal_at_home else "low hp: retreat", hero, c)
        if c.t < self._evade_until.get(seat, -1e9):
            self._holding[seat] = False
            return self._go_home(m, "losing hp fast: evade", hero, c)

        under_own_tower = any(distance(pos, s.pos) <= k.tower_range for s in c.own_towers)

        # 2-3. A fight: enemy heroes near.
        if near:
            self._holding[seat] = False
            ours = sum(max(0.25, h.hp_frac if h.hp_frac is not None else 1.0)
                       for s, h in c.alive.items() if distance(pos, h.pos) <= k.assist_range
                       and not self._retreating.get(s))
            theirs = sum(e.hp_frac if e.hp_frac is not None else 1.0
                         for e in c.enemies if distance(pos, e.pos) <= k.assist_range)
            theirs += k.fog_weight * sum(1 for g in c.ghosts if distance(pos, g) <= k.assist_range)
            if theirs > ours + k.outnumber_margin and not under_own_tower and c.posture != "allin":
                return self._go_home(m, f"outnumbered {theirs:.1f} vs {ours:.1f}: back off", hero, c)
            # Don't chase a hero under its own tower without the team.
            target = min(near, key=lambda e: distance(pos, e.pos))
            if self._tower_guarded(target.pos, c) and not self._enough_at(target.pos, c):
                if in_enemy_tower:
                    return self._go_home(m, "enemy under its tower: step out", hero, c, priority=P_POSITION)
                return self._hold(m, "enemy under its tower: wait", P_POSITION)
            for macro in ("macro_skill3", "macro_skill1", "macro_skill2"):
                o = m.get(macro)
                if o and self._aims_at_hero(o, pos, c) and o.dist <= k.skill_range * 1.15:
                    return Choice(macro, f"{macro[6:]} on hero", P_SKILL_HERO, ["macro_attack", "macro_advance"])
            o = m.get("macro_attack")
            if o and not self._monster(o):
                at_hero = self._aims_at_hero(o, pos, c)
                return Choice("macro_attack", "attack hero" if at_hero else "attack into the fight",
                              P_HIT_HERO if at_hero else P_ATTACK, ["macro_advance"])
            return _first(m, [("macro_advance", "close in")], P_ADVANCE)

        # 5. Defend a structure of ours that is losing HP.
        if c.threatened and hp >= k.defend_hp and c.posture != "allin":
            s = min(c.threatened, key=lambda s: distance(pos, s.pos))
            d = distance(pos, s.pos)
            if d <= k.defend_range:
                self._holding[seat] = False
                o = m.get("macro_attack")
                if o and o.dist <= k.attack_range and d <= k.tower_range + k.attack_range:
                    return Choice("macro_attack", "defend: attack", P_POSITION, ["macro_retreat"])
                if c.home is None or distance(s.pos, c.home) < distance(pos, c.home):
                    return self._go_home(m, "defend: go back to the structure", hero, c, priority=P_POSITION)
                if "macro_advance" in m:  # the structure lies ahead of this hero, toward the enemy
                    return Choice("macro_advance", "defend: go to the structure", P_POSITION, ["macro_attack"])

        # 7. Protect a lead: stay out of enemy tower range, near our side.
        if c.posture == "protect":
            front = c.fronts.get(seat, math.inf)
            if front <= k.tower_range + 6000:
                return self._go_home(m, "protect the lead: pull back", hero, c, priority=P_POSITION)
            o = m.get("macro_attack")
            if o and o.dist <= k.attack_range and not self._aims_at_structure(o, pos, c) and not self._monster(o):
                return Choice("macro_attack", "protect: farm in range", P_ATTACK, ["macro_retreat"])
            return self._hold(m, "protect the lead: hold", P_HOLD)

        # 4. Enemy tower ahead: only with the team.
        tower = min(c.enemy_towers, key=lambda s: distance(pos, s.pos), default=None)
        if tower is not None and c.posture != "allin":
            dt = distance(pos, tower.pos)
            low = tower.hp_frac is not None and tower.hp_frac <= k.low_tower
            if dt <= k.tower_range + 1500 and not low and not self._enough_at(tower.pos, c) \
                    and self._patient(seat, c.t):
                self._holding[seat] = False
                if dt <= k.tower_range:
                    return self._go_home(m, "tower without the team: step out", hero, c, priority=P_POSITION)
                o = m.get("macro_attack")
                if o and o.dist <= k.attack_range and not self._aims_at_structure(o, pos, c) \
                        and not self._monster(o):
                    return Choice("macro_attack", "wait for the team: farm", P_ATTACK, ["macro_retreat"])
                return self._hold(m, "wait for the team at the tower", P_HOLD)

        # 6. Grouping: the hero far ahead of the team waits.
        if k.hold and c.posture == "normal" and not c.window and len(c.fronts) >= 3 and seat in c.fronts:
            healthy = [c.fronts[s] for s in c.fronts if not self._retreating.get(s)]
            if len(healthy) >= 3:
                lead = statistics.median(healthy) - c.fronts[seat]
                limit = k.group_lead * (0.5 if self._holding.get(seat) else 1.0)
                if lead > limit and self._patient(seat, c.t):
                    self._holding[seat] = True
                    o = m.get("macro_attack")
                    if o and o.dist <= k.attack_range and not self._aims_at_structure(o, pos, c) \
                            and not self._monster(o):
                        return Choice("macro_attack", "ahead of the team: farm", P_ATTACK, ["macro_advance"])
                    return self._hold(m, "ahead of the team: wait", P_HOLD)
        self._holding[seat] = False
        self._waiting_since.pop(seat, None)

        # Farm and push.
        o = m.get("macro_attack")
        if o and o.dist <= k.attack_range and not self._monster(o):
            return Choice("macro_attack", "push: attack in range", P_ATTACK, ["macro_advance"])
        if k.skills_on_minions and not any(distance(pos, e.pos) <= 2 * k.engage_range for e in c.enemies):
            for macro in ("macro_skill1", "macro_skill2"):
                o = m.get(macro)
                if o and o.dist <= k.skill_range and not self._aims_at_structure(o, pos, c) \
                        and not self._monster(o):
                    return Choice(macro, f"{macro[6:]} on the wave", P_SKILL_UNIT, ["macro_attack", "macro_advance"])
        return _first(m, [("macro_advance", "push: advance"), ("macro_attack", "attack (no advance)")], P_ADVANCE)

    # -- helpers -----------------------------------------------------------------------------

    def _patient(self, seat: int, t: float) -> bool:
        """Whether this hero may still wait for the team: waits end after ``patience_s``, and a
        hero that ran out of patience goes on for as long again before it waits anew."""
        since = self._waiting_since.setdefault(seat, t)
        waited = t - since
        if waited <= self.knobs.patience_s:
            return True
        if waited > 2 * self.knobs.patience_s:
            self._waiting_since[seat] = t
            return True
        return False

    def _go_home(self, m: dict[str, Offer], reason: str, hero: Hero, c: _Ctx, *,
                 priority: int = P_SAVE) -> Choice:
        if "macro_retreat" in m:
            return Choice("macro_retreat", reason, priority, ["macro_attack", "macro_advance"])
        # Already home (no retreat on offer): fight what comes, else wait there.
        o = m.get("macro_attack")
        if o and o.dist <= self.knobs.attack_range:
            return Choice("macro_attack", reason + " (home: attack)", priority, ["macro_advance"])
        return self._hold(m, reason + " (home: hold)", priority)

    def _hold(self, m: dict[str, Offer], reason: str, priority: int) -> Choice:
        if self.knobs.hold:
            return Choice(HOLD, reason, priority, ["macro_retreat", "macro_attack"])
        return _first(m, [("macro_attack", reason + " (attack)"), ("macro_retreat", reason + " (retreat)"),
                          ("macro_advance", reason + " (advance)")], priority)

    def _tower_guarded(self, pos: tuple[float, float] | None, c: _Ctx) -> bool:
        if pos is None:
            return False
        for s in c.enemy_towers:
            if distance(pos, s.pos) <= self.knobs.tower_range and not (
                    s.hp_frac is not None and s.hp_frac <= self.knobs.low_tower):
                return True
        return False

    def _enough_at(self, pos: tuple[float, float] | None, c: _Ctx) -> bool:
        if pos is None:
            return False
        reach = self.knobs.tower_range + 4000
        n = sum(1 for s, h in c.alive.items()
                if distance(h.pos, pos) <= reach and not self._retreating.get(s)
                and (h.hp_frac is None or h.hp_frac >= 0.4))
        return n >= c.min_pushers

    def _monster(self, o: Offer) -> bool:
        """The label names a jungle monster or boss (left alone unless ``attack_monsters``)."""
        if self.knobs.attack_monsters or not o.target:
            return False
        return any(w in o.target for w in _MONSTER_WORDS)

    def _aims_at_hero(self, o: Offer, pos: tuple[float, float], c: _Ctx) -> bool:
        if "hero" in o.target:
            return "enemy" in o.target or not any(w in o.target for w in ("ally", "friend", "own"))
        if o.target and any(w in o.target for w in ("tower", "crystal", "soldier", "minion", "creep", "monster")):
            return False
        return _matches(o.dist, [distance(pos, e.pos) for e in c.enemies])

    def _aims_at_structure(self, o: Offer, pos: tuple[float, float], c: _Ctx) -> bool:
        if any(w in o.target for w in ("tower", "crystal", "turret", "organ")):
            return True
        if o.target and any(w in o.target for w in ("hero", "soldier", "minion", "creep", "monster")):
            return False
        return _matches(o.dist, [distance(pos, s.pos) for s in c.enemy_towers])


_MONSTER_WORDS = ("monster", "jungle", "neutral", "tyrant", "overlord", "dragon", "baron", "buff", "boss")


def _matches(d: float, candidates: list[float]) -> bool:
    if not math.isfinite(d):
        return False
    return any(abs(d - x) <= max(300.0, 0.06 * x) for x in candidates if math.isfinite(x))


def _first(m: dict[str, Offer], options: list[tuple[str, str]], priority: int) -> Choice | None:
    for base, reason in options:
        if base in m:
            return Choice(base, reason, priority)
    if m:
        base = next(iter(m))
        return Choice(base, f"only {base} on offer", priority)
    return None
