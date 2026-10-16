"""A small abstract 5v5 MOBA, to compare HoK team policies with each other.

**This is not Honor of Kings.** It is a model built from the platform's guide
and general MOBA knowledge: three lanes, three towers per lane and a crystal
per side, minion waves, five heroes a side (tank, fighter, mage, marksman,
support) with a basic attack and three skills, levels and gold, fog, deaths
and revives, a fountain. Its numbers are guesses. Use it for *relative*
comparisons against baselines, never as a forecast of live results.

What it reproduces from the platform's contract (/skill/hok):
- one step = 0.2 s = 3 frames; every hero gets one action a step;
- macros ``macro_advance`` (walk toward the nearest enemy tower or crystal),
  ``macro_retreat`` (walk home), ``macro_attack`` (basic attack on the nearest
  target), ``macro_skill1..3`` (cast at the nearest target), each offered only
  while possible, with labels ``"... (nearest: <what>, <d> away)"``;
- a macro is the hero's standing order, re-computed every step; a raw button
  plays once and clears it;
- the result: crystal destroyed, else at the step limit the tiebreak (damage
  to enemy towers and crystal, then hero kills, else a draw).

Model choices that matter for policies (all guesses, all switchable):
- towers prefer minions, but turn on a hero that hits one of their heroes
  nearby, and their damage on the same hero ramps up shot after shot;
- a tower is only hittable once the one before it on its lane is down; the
  crystal once a high-ground tower is down;
- ``skill_fallback="idle"``: a standing skill order on cooldown does nothing
  (the hero stands), the harsh reading of "re-computes the same macro";
- skills hit heroes and minions, not structures.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

STEP_SECONDS = 0.2
FRAMES_PER_STEP = 3
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")

# The live map (first live game, e5f159ed): one diagonal lane; per camp an outer tower (4000 HP)
# at +-11.5k, an inner tower (6000) at +-21.5k, the crystal (8000) at +-34.3k, the spring at +-45k.
LIVE_BASE = {1: (-34330.0, -34390.0), 2: (34360.0, 34330.0)}
LIVE_SPAWN = {1: (-45000.0, -45070.0), 2: (45070.0, 45000.0)}
LIVE_LANES = [[LIVE_BASE[1], LIVE_BASE[2]]]
LIVE_TOWERS = ((0.332, 4000.0), (0.187, 6000.0))   # camp 1's (fraction along the lane, HP), outer first
# The first guess (before any live game): three lanes, three towers each.
OLD_BASE = {1: (-42000.0, -42000.0), 2: (42000.0, 42000.0)}
OLD_SPAWN = {1: (-48000.0, -48000.0), 2: (48000.0, 48000.0)}
OLD_LANES = [
    [OLD_BASE[1], (-44000.0, -28000.0), (-44000.0, 44000.0), (28000.0, 44000.0), OLD_BASE[2]],   # top
    [OLD_BASE[1], OLD_BASE[2]],                                                                   # mid
    [OLD_BASE[1], (-28000.0, -44000.0), (44000.0, -44000.0), (44000.0, 28000.0), OLD_BASE[2]],   # bottom
]
OLD_TOWERS = ((0.36, 6000.0), (0.24, 6000.0), (0.12, 6000.0))
BASE = LIVE_BASE
SPAWN = LIVE_SPAWN
LANES = LIVE_LANES
CRYSTAL_HP = 8000.0
TOWER_RANGE = 8800.0
TOWER_DAMAGE = 450.0
TOWER_RAMP = 0.35                       # +35% per consecutive shot on the same hero
CRYSTAL_DAMAGE = 650.0
FOUNTAIN_RANGE = 7000.0
VISION = 10000.0
ACQUIRE = 12000.0                       # macro_attack/skills look this far for a target
WAVE_FIRST_S, WAVE_EVERY_S = 15.0, 30.0
MINION_SPEED = 2200.0
MINION_AGGRO = 6000.0
HERO_SPEED = 3000.0
PASSIVE_GOLD_PER_S = 4.0

ROLES = {
    #            hp    atk  range  interval  skills (multipliers)   skill range
    "tank":     (3600, 120, 2200, 1.1, (1.2, 1.5, 2.5), 6000),
    "fighter":  (3200, 165, 2200, 1.0, (1.5, 1.8, 3.0), 6000),
    "mage":     (2500, 115, 7000, 1.1, (2.4, 2.8, 4.5), 8000),
    "marksman": (2400, 185, 7500, 0.8, (1.4, 1.6, 3.0), 8000),
    "support":  (2900, 95, 6000, 1.1, (1.2, 1.6, 2.0), 8000),
}
ROLE_ORDER = ("tank", "fighter", "mage", "marksman", "support")
SKILL_CD = (6.0, 9.0, 30.0)
SKILL_LEVEL = (1, 2, 4)


def _lane_point(lane: list[tuple[float, float]], frac: float) -> tuple[float, float]:
    lengths = [math.dist(lane[i], lane[i + 1]) for i in range(len(lane) - 1)]
    target = frac * sum(lengths)
    for i, seg in enumerate(lengths):
        if target <= seg:
            a, b = lane[i], lane[i + 1]
            r = target / seg if seg else 0.0
            return (a[0] + (b[0] - a[0]) * r, a[1] + (b[1] - a[1]) * r)
        target -= seg
    return lane[-1]


class Unit:
    __slots__ = ("uid", "camp", "kind", "x", "z", "hp", "max_hp", "atk", "rng", "interval", "ready_at",
                 "lane", "tier", "path_s", "seat", "hero_id", "role", "level", "exp", "money", "kills",
                 "deaths", "revive_at", "order", "oneshot", "skill_ready", "skill_mult", "skill_rng",
                 "hits", "last_damage", "aggro_at", "focus", "focus_n", "speed", "dealt")

    def __init__(self, uid: int, camp: int, kind: str, x: float, z: float, hp: float, atk: float,
                 rng: float, interval: float) -> None:
        self.uid, self.camp, self.kind = uid, camp, kind
        self.x, self.z = x, z
        self.hp = self.max_hp = hp
        self.atk, self.rng, self.interval = atk, rng, interval
        self.ready_at = 0.0
        self.lane = -1
        self.tier = -1
        self.path_s = 0.0
        self.seat = -1
        self.hero_id = 0
        self.role = ""
        self.level = 1
        self.exp = 0.0
        self.money = 0.0
        self.kills = 0
        self.deaths = 0
        self.revive_at = 0.0
        self.order: str | None = None
        self.oneshot: dict | None = None
        self.skill_ready = [0.0, 0.0, 0.0]
        self.skill_mult = (1.0, 1.0, 1.0)
        self.skill_rng = 8000.0
        self.hits: dict[int, float] = {}    # attacker hero uid -> last time it hit this unit
        self.last_damage = (-1, -1e9)       # (hero uid, t)
        self.aggro_at = -1e9                # last time this hero hit an enemy hero
        self.focus = -1
        self.focus_n = 0
        self.speed = HERO_SPEED
        self.dealt = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def pos(self) -> tuple[float, float]:
        return (self.x, self.z)


@dataclass
class SimConfig:
    max_steps: int = 3000
    map: str = "live"                   # "live": the one-lane map seen live; "3lane": the first guess
    fountain_heal: float = 0.0          # share of max HP a second at the spring (live: none seen)
    revive_per_level: float = 2.2       # revive = 2 s + this x level (live: 5-25 s)
    skill_fallback: str = "idle"        # a standing skill order on cooldown: "idle" | "attack"
    remove_destroyed: bool = True       # destroyed structures leave the state's list
    opaque_labels: bool = False         # labels name no target kind (forces distance matching)
    jitter: float = 0.1                 # damage +-10%
    tower_dmg_mult: float = 1.0
    hero_power_mult: dict = field(default_factory=lambda: {1: 1.0, 2: 1.0})


class Sim:
    """One match. Camps 1 and 2; seats 0-4 are camp 1, 5-9 camp 2."""

    def __init__(self, seed: int = 0, config: SimConfig | None = None) -> None:
        self.cfg = config or SimConfig()
        live = self.cfg.map == "live"
        self.base = LIVE_BASE if live else OLD_BASE
        self.spawn = LIVE_SPAWN if live else OLD_SPAWN
        self.lanes = LIVE_LANES if live else OLD_LANES
        self.towers = LIVE_TOWERS if live else OLD_TOWERS
        self.last_tier = len(self.towers) - 1
        self.heal_ready: dict[int, float] = {}
        self.rng = random.Random(seed)
        self.step_no = 0
        self.t = 0.0
        self._uid = 0
        self.units: list[Unit] = []
        self.heroes: list[Unit] = []
        self.minions: list[Unit] = []
        self.structures: list[Unit] = []
        self.next_wave = WAVE_FIRST_S
        self.structure_damage = {1: 0.0, 2: 0.0}   # dealt by camp to enemy towers and crystal
        self.kills = {1: 0, 2: 0}
        self.winner: int | None = None
        self.termination: str | None = None
        self.events: list[tuple[float, str]] = []
        self._vis_cache: tuple[int, dict] = (-1, {})
        self._targetable: dict[int, bool] = {}
        self._summary_cache: dict[int, tuple[int, dict]] = {}
        self._entries_cache: dict[int, tuple[int, list]] = {}
        self._lane_minions: list[list[Unit]] = [[]]
        self._camp_view_cache: dict[int, tuple[int, list, list]] = {}
        self._build()

    # -- setup ----------------------------------------------------------------------------------

    def _new(self, *args) -> Unit:
        self._uid += 1
        u = Unit(self._uid, *args)
        self.units.append(u)
        return u

    def _build(self) -> None:
        for camp in (1, 2):
            for lane_i, lane in enumerate(self.lanes):
                for tier, (frac, hp) in enumerate(self.towers):
                    f = frac if camp == 1 else 1.0 - frac
                    x, z = _lane_point(lane, f)
                    t = self._new(camp, "tower", x, z, hp, TOWER_DAMAGE * self.cfg.tower_dmg_mult,
                                  TOWER_RANGE, 1.0)
                    t.lane, t.tier = lane_i, tier
                    self.structures.append(t)
            c = self._new(camp, "crystal", *self.base[camp], CRYSTAL_HP,
                          CRYSTAL_DAMAGE * self.cfg.tower_dmg_mult, TOWER_RANGE, 1.0)
            c.tier = self.last_tier + 1
            self.structures.append(c)
            s = self._new(camp, "spawn", *self.spawn[camp], 1e9, 0, FOUNTAIN_RANGE, 1.0)
            self.structures.append(s)
            for i, role in enumerate(ROLE_ORDER):
                hp, atk, rng, interval, mult, srng = ROLES[role]
                sx, sz = self.spawn[camp]
                h = self._new(camp, "hero", sx + 500 * (i - 2), sz - 500 * (i - 2) * (1 if camp == 1 else -1),
                              hp, atk, rng, interval)
                h.seat = i if camp == 1 else 5 + i
                h.hero_id = 100 + 10 * i + camp
                h.role = role
                h.skill_mult, h.skill_rng = mult, srng
                self.heroes.append(h)

    # -- queries --------------------------------------------------------------------------------

    def hero(self, seat: int) -> Unit:
        return self.heroes[seat]

    def camp_heroes(self, camp: int) -> list[Unit]:
        return [h for h in self.heroes if h.camp == camp]

    def targetable(self, s: Unit) -> bool:
        if not s.alive or s.kind == "spawn":
            return False
        cached = self._targetable.get(s.uid)
        if cached is None:
            cached = self._targetable[s.uid] = self._compute_targetable(s)
        return cached

    def _compute_targetable(self, s: Unit) -> bool:
        if s.kind == "tower":
            if s.tier == 0:
                return True
            prev = [o for o in self.structures if o.kind == "tower" and o.camp == s.camp and o.lane == s.lane
                    and o.tier == s.tier - 1]
            return all(not o.alive for o in prev)
        if s.kind == "crystal":
            return any(not o.alive for o in self.structures
                       if o.kind == "tower" and o.camp == s.camp and o.tier == self.last_tier)
        return True

    def visible_to(self, camp: int, u: Unit) -> bool:
        """Heroes are seen by any living unit of the camp within VISION (cached per step)."""
        if u.camp == camp:
            return True
        if u.kind != "hero":
            return True
        cache = self._vis_cache
        if cache[0] != self.step_no:
            cache = self._vis_cache = (self.step_no, {})
        key = (camp, u.uid)
        seen = cache[1].get(key)
        if seen is None:
            seen = False
            for o in self.units:
                if o.camp == camp and o.alive and o.kind != "spawn" and abs(o.x - u.x) <= VISION \
                        and abs(o.z - u.z) <= VISION and math.dist(o.pos, u.pos) <= VISION:
                    seen = True
                    break
            cache[1][key] = seen
        return seen

    def nearest_target(self, h: Unit, *, structures: bool = True, max_d: float = ACQUIRE) -> tuple[Unit | None, float]:
        best, best_d = None, max_d
        enemy = 3 - h.camp
        for u in self.heroes:
            if u.camp == enemy and u.alive:
                d = math.dist(h.pos, u.pos)
                if d <= best_d and self.visible_to(h.camp, u):
                    best, best_d = u, d
        for u in self.minions:
            if u.camp == enemy and u.alive:
                d = math.dist(h.pos, u.pos)
                if d <= best_d:
                    best, best_d = u, d
        if structures:
            for u in self.structures:
                if u.camp == enemy and u.kind != "spawn" and u.alive and self.targetable(u):
                    d = math.dist(h.pos, u.pos)
                    if d <= best_d:
                        best, best_d = u, d
        return best, best_d

    def nearest_enemy_structure(self, h: Unit) -> Unit | None:
        cands = [s for s in self.structures if s.camp != h.camp and s.alive and s.kind in ("tower", "crystal")]
        return min(cands, key=lambda s: math.dist(h.pos, s.pos), default=None)

    def macros_for(self, h: Unit) -> dict[str, tuple[Unit | None, float]]:
        """The macros on offer for a hero now, with their target and distance."""
        if not h.alive:
            return {}
        out: dict[str, tuple[Unit | None, float]] = {}
        s = self.nearest_enemy_structure(h)
        if s is not None:
            out["macro_advance"] = (s, math.dist(h.pos, s.pos))
        home = self.spawn[h.camp]
        if math.dist(h.pos, home) > 3000:
            out["macro_retreat"] = (None, math.dist(h.pos, home))
        target, d = self.nearest_target(h)
        if target is not None:
            out["macro_attack"] = (target, d)
        unit_target, ud = self.nearest_target(h, max_d=h.skill_rng)
        if unit_target is not None:
            for i in range(3):
                if h.level >= SKILL_LEVEL[i] and self.t >= h.skill_ready[i]:
                    out[f"macro_skill{i + 1}"] = (unit_target, ud)
        return out

    def describe(self, target: Unit | None, viewer_camp: int) -> str:
        if target is None:
            return "home"
        if self.cfg.opaque_labels:
            return f"unit {target.uid}"
        side = "enemy" if target.camp != viewer_camp else "allied"
        if target.kind == "hero":
            return f"{side} hero {target.hero_id}"
        if target.kind == "minion":
            return f"{side} minion"
        return f"{side} {target.kind}"

    # -- orders ---------------------------------------------------------------------------------

    def apply(self, seat: int, action: dict) -> str | None:
        """Apply one hero's action; returns an error text when it isn't legal now."""
        h = self.heroes[seat]
        if "macro" in action:
            macro = action["macro"]
            if macro not in self.macros_for(h):
                return f"{macro} is not available for seat {seat} now"
            h.order = macro
            h.oneshot = None
            return None
        button = action.get("which_button")
        if button == 1:  # NoOp
            h.order = None
            h.oneshot = {"which_button": 1}
            return None
        if button == 2:
            mv = action.get("move")
            if not (isinstance(mv, int) and 1 <= mv <= 24) or not h.alive:
                return "move not in the legal values"
            h.order = None
            h.oneshot = {"which_button": 2, "move": mv}
            return None
        if button == 11:
            if not h.alive or self.t < self.heal_ready.get(h.uid, 0.0):
                return "HealSkill is not available now"
            h.order = None
            h.oneshot = {"which_button": 11}
            return None
        return f"unknown action {action!r}"

    # -- the step -------------------------------------------------------------------------------

    def step(self) -> None:
        if self.winner is not None or self.termination is not None:
            return
        dt = STEP_SECONDS
        self.t += dt
        self.step_no += 1
        if self.t >= self.next_wave:
            self.next_wave += WAVE_EVERY_S
            self._spawn_wave()
        lanes: list[list[Unit]] = [[] for _ in self.lanes]
        for m in self.minions:
            lanes[m.lane].append(m)
        self._lane_minions = lanes
        for h in self.heroes:
            self._hero_step(h, dt)
        for m in self.minions:
            if m.alive:
                self._minion_step(m, dt)
        for s in self.structures:
            if s.alive:
                self._structure_step(s, dt)
        self.minions = [m for m in self.minions if m.alive]
        self.units = [u for u in self.units if u.kind != "minion" or u.alive]
        for camp in (1, 2):
            crystal = next(s for s in self.structures if s.kind == "crystal" and s.camp == camp)
            if not crystal.alive:
                self.winner = 3 - camp
                self.termination = "crystal_destroyed"
                return
        if self.step_no >= self.cfg.max_steps:
            self.termination = "step_limit"
            self.winner = self.tiebreak_winner()

    def tiebreak_winner(self) -> int | None:
        d1, d2 = self.structure_damage[1], self.structure_damage[2]
        if abs(d1 - d2) > 1e-6:
            return 1 if d1 > d2 else 2
        if self.kills[1] != self.kills[2]:
            return 1 if self.kills[1] > self.kills[2] else 2
        return None

    def _spawn_wave(self) -> None:
        for camp in (1, 2):
            for lane_i in range(len(self.lanes)):
                for k in range(3):
                    ranged = k == 2
                    s0 = 0.02 + self.rng.uniform(0, 0.004) - 0.004 * k
                    f = s0 if camp == 1 else 1.0 - s0
                    x, z = _lane_point(self.lanes[lane_i], f)
                    m = self._new(camp, "minion", x, z, 1000.0 if ranged else 1500.0, 90.0 if ranged else 70.0,
                                  5000.0 if ranged else 1500.0, 1.0)
                    m.lane, m.path_s, m.speed = lane_i, f, MINION_SPEED
                    self.minions.append(m)

    def _move_toward(self, u: Unit, p: tuple[float, float], dt: float, stop: float = 0.0) -> None:
        d = math.dist(u.pos, p)
        if d <= stop or d < 1e-6:
            return
        step = min(u.speed * dt, d - stop)
        u.x += (p[0] - u.x) / d * step
        u.z += (p[1] - u.z) / d * step

    def _damage(self, src: Unit, dst: Unit, amount: float) -> None:
        if not dst.alive or amount <= 0:
            return
        amount *= 1.0 + self.rng.uniform(-self.cfg.jitter, self.cfg.jitter)
        dealt = min(dst.hp, amount)
        dst.hp -= dealt
        if dst.kind in ("tower", "crystal") and src.camp != dst.camp:
            self.structure_damage[src.camp] += dealt
        if src.kind == "hero":
            dst.last_damage = (src.uid, self.t)
            dst.hits[src.uid] = self.t
            if dst.kind == "hero":
                src.aggro_at = self.t
        if dst.hp <= 0:
            self._died(dst, src)

    def _died(self, u: Unit, killer: Unit) -> None:
        u.hp = 0.0
        if u.kind == "hero":
            u.deaths += 1
            u.revive_at = self.t + min(45.0, 2.0 + self.cfg.revive_per_level * u.level)
            u.order = u.order  # the standing order stays
            credit = None
            if killer.kind == "hero":
                credit = killer
            else:
                uid, when = u.last_damage
                if self.t - when <= 5.0:
                    credit = next((h for h in self.heroes if h.uid == uid), None)
            if credit is not None and credit.camp != u.camp:
                credit.kills += 1
                credit.money += 250
                self._exp(credit, 150)
                self.kills[credit.camp] += 1
                for h in self.heroes:
                    if h is not credit and h.camp == credit.camp and self.t - u.hits.get(h.uid, -1e9) <= 10:
                        h.money += 100
            u.hits.clear()
            self.events.append((self.t, f"hero {u.seat} died"))
        elif u.kind == "minion":
            near = [h for h in self.heroes if h.camp != u.camp and h.alive and math.dist(h.pos, u.pos) <= 12000]
            for h in near:
                self._exp(h, 40.0 / len(near))
                h.money += 35 if h is killer else 10
        elif u.kind in ("tower", "crystal"):
            self._targetable.clear()
            for h in self.heroes:
                if h.camp != u.camp:
                    h.money += 150
            self.events.append((self.t, f"camp {u.camp} {u.kind} lane {u.lane} tier {u.tier} destroyed"))

    def _exp(self, h: Unit, amount: float) -> None:
        h.exp += amount
        while h.level < 15 and h.exp >= 100 + 60 * h.level:
            h.exp -= 100 + 60 * h.level
            h.level += 1
            frac = h.hp / h.max_hp if h.max_hp else 1.0
            base_hp, base_atk = ROLES[h.role][0], ROLES[h.role][1]
            h.max_hp = base_hp * (1 + 0.08 * (h.level - 1))
            h.hp = max(h.hp, frac * h.max_hp)
            h.atk = base_atk * (1 + 0.07 * (h.level - 1))

    def _power(self, h: Unit) -> float:
        return (1.0 + h.money / 12000.0) * self.cfg.hero_power_mult.get(h.camp, 1.0)

    def _hero_step(self, h: Unit, dt: float) -> None:
        if not h.alive:
            if self.t >= h.revive_at and h.revive_at > 0:
                h.hp = h.max_hp
                h.x, h.z = self.spawn[h.camp]
                h.skill_ready = [min(r, self.t) for r in h.skill_ready]
            return
        h.money += PASSIVE_GOLD_PER_S * dt
        h.hp = min(h.max_hp, h.hp + 0.0025 * h.max_hp * dt)
        if self.cfg.fountain_heal and math.dist(h.pos, self.spawn[h.camp]) <= FOUNTAIN_RANGE:
            h.hp = min(h.max_hp, h.hp + self.cfg.fountain_heal * h.max_hp * dt)
        oneshot, h.oneshot = h.oneshot, None
        if oneshot is not None:
            if oneshot.get("which_button") == 2:
                angle = math.radians((oneshot["move"] - 1) * 15.0)
                self._move_toward(h, (h.x + math.cos(angle) * 1e5, h.z + math.sin(angle) * 1e5), dt)
            elif oneshot.get("which_button") == 11:   # the heal summoner skill: self and allies near
                self.heal_ready[h.uid] = self.t + 120.0
                for a in self.heroes:
                    if a.camp == h.camp and a.alive and math.dist(a.pos, h.pos) <= 4000:
                        a.hp = min(a.max_hp, a.hp + 0.15 * a.max_hp)
            return
        order = h.order
        if order is None:
            return
        if order == "macro_retreat":
            self._move_toward(h, self.spawn[h.camp], dt)
            return
        if order == "macro_advance":
            s = self.nearest_enemy_structure(h)
            if s is not None:
                self._move_toward(h, s.pos, dt, stop=h.rng * 0.9)
            return
        if order.startswith("macro_skill"):
            i = int(order[-1]) - 1
            target, d = self.nearest_target(h, max_d=h.skill_rng)
            if h.level >= SKILL_LEVEL[i] and self.t >= h.skill_ready[i] and target is not None:
                h.skill_ready[i] = self.t + SKILL_CD[i]
                dmg = h.atk * h.skill_mult[i] * self._power(h)
                self._damage(h, target, dmg * (0.5 if target.kind in ("tower", "crystal") else 1.0))
                if i == 2:  # the ultimate splashes half its damage around the target
                    for u in self.heroes + self.minions:
                        if u.camp == target.camp and u is not target and u.alive \
                                and math.dist(u.pos, target.pos) <= 3000:
                            self._damage(h, u, dmg * 0.5)
                return
            if self.cfg.skill_fallback != "attack":
                return
            order = "macro_attack"
        if order == "macro_attack":
            target, d = self.nearest_target(h)
            if target is None:
                return
            if d > h.rng:
                self._move_toward(h, target.pos, dt, stop=h.rng * 0.95)
            elif self.t >= h.ready_at:
                h.ready_at = self.t + h.interval
                self._damage(h, target, h.atk * self._power(h))

    def _minion_step(self, m: Unit, dt: float) -> None:
        enemy = 3 - m.camp
        best, best_d = None, MINION_AGGRO
        for u in self._lane_minions[m.lane]:
            if u.camp == enemy and u.alive:
                d = math.dist(m.pos, u.pos)
                if d < best_d:
                    best, best_d = u, d
        if best is None:
            for u in self.heroes:
                if u.camp == enemy and u.alive:
                    d = math.dist(m.pos, u.pos)
                    if d < best_d:
                        best, best_d = u, d
        if best is None:
            for s in self.structures:
                if s.camp == enemy and s.kind != "spawn" and s.alive and self.targetable(s):
                    d = math.dist(m.pos, s.pos)
                    if d < best_d + 3000:
                        best, best_d = s, d
        if best is not None:
            if best_d > m.rng:
                self._move_toward(m, best.pos, dt, stop=m.rng * 0.95)
            elif self.t >= m.ready_at:
                m.ready_at = self.t + m.interval
                self._damage(m, best, m.atk)
            return
        # Walk the lane toward the enemy base.
        lane = self.lanes[m.lane]
        direction = 1.0 if m.camp == 1 else -1.0
        total = sum(math.dist(lane[i], lane[i + 1]) for i in range(len(lane) - 1))
        m.path_s = min(1.0, max(0.0, m.path_s + direction * m.speed * dt / total))
        m.x, m.z = _lane_point(lane, m.path_s)

    def _structure_step(self, s: Unit, dt: float) -> None:
        if s.kind == "spawn":
            for h in self.heroes:
                if h.camp != s.camp and h.alive and math.dist(h.pos, s.pos) <= FOUNTAIN_RANGE:
                    self._damage(s, h, 3000 * dt)
            return
        if self.t < s.ready_at:
            return
        enemy = 3 - s.camp
        target = None
        # A hero that just hit one of our heroes in range draws the fire.
        for h in self.heroes:
            if h.camp == enemy and h.alive and self.t - h.aggro_at <= 2.0 and math.dist(h.pos, s.pos) <= s.rng:
                target = h
                break
        if target is None:
            near = [m for m in self.minions if m.camp == enemy and m.alive and math.dist(m.pos, s.pos) <= s.rng]
            if near:
                target = min(near, key=lambda m: math.dist(m.pos, s.pos))
        if target is None:
            near = [h for h in self.heroes if h.camp == enemy and h.alive and math.dist(h.pos, s.pos) <= s.rng]
            if near:
                target = min(near, key=lambda h: math.dist(h.pos, s.pos))
        if target is None:
            s.focus, s.focus_n = -1, 0
            return
        s.ready_at = self.t + s.interval
        if target.kind == "minion":
            s.focus, s.focus_n = -1, 0
            self._damage(s, target, 0.5 * target.max_hp)
            return
        s.focus_n = s.focus_n + 1 if s.focus == target.uid else 1
        s.focus = target.uid
        self._damage(s, target, s.atk * (1 + TOWER_RAMP * (s.focus_n - 1)))

    # -- the platform's view ----------------------------------------------------------------------

    def summary(self, seat: int) -> dict[str, Any]:
        """One hero's view (cached for the step: nothing in it changes between steps)."""
        cached = self._summary_cache.get(seat)
        if cached is not None and cached[0] == self.step_no:
            return cached[1]
        out = self._summary(seat)
        self._summary_cache[seat] = (self.step_no, out)
        return out

    def _camp_view(self, camp: int) -> tuple[list[dict], list[dict]]:
        """(other_heroes entries for all ten heroes, structures) as one camp sees them this step."""
        cached = self._camp_view_cache.get(camp)
        if cached is not None and cached[0] == self.step_no:
            return cached[1], cached[2]
        others = []
        for o in self.heroes:
            entry: dict[str, Any] = {"runtime_id": o.uid, "hero_id": o.hero_id, "camp": o.camp}
            if o.camp == camp or (o.alive and self.visible_to(camp, o)):
                entry.update({"visible": True, "hp": round(o.hp), "max_hp": round(o.max_hp),
                              "x": round(o.x), "z": round(o.z), "level": o.level})
            else:
                entry["visible"] = False
            others.append(entry)
        structures = []
        for s in self.structures:
            if self.cfg.remove_destroyed and not s.alive:
                continue
            structures.append({"camp": s.camp, "type": {"tower": "ACTOR_TOWER", "crystal": "ACTOR_CRYSTAL",
                                                        "spawn": "ACTOR_SPRING"}[s.kind],
                               "hp": round(s.hp) if s.kind != "spawn" else 1,
                               "max_hp": round(s.max_hp) if s.kind != "spawn" else 1,
                               "x": round(s.x), "z": round(s.z)})
        self._camp_view_cache[camp] = (self.step_no, others, structures)
        return others, structures

    def _summary(self, seat: int) -> dict[str, Any]:
        h = self.heroes[seat]
        camp = h.camp
        me = {
            "hero_id": h.hero_id, "runtime_id": h.uid, "camp": camp, "hp": round(h.hp), "max_hp": round(h.max_hp),
            "level": h.level, "x": round(h.x), "z": round(h.z), "kills": h.kills, "deaths": h.deaths,
            "money": round(h.money), "ep": 100,
            "revive_time": max(0, round((h.revive_at - self.t) * 1000)) if not h.alive else 0,
        }
        all_others, structures = self._camp_view(camp)
        others = [e for e in all_others if e["runtime_id"] != h.uid]
        return {"my_hero": me, "other_heroes": others, "structures": structures,
                "frame_no": self.step_no * FRAMES_PER_STEP}

    def legal_entries(self, seat: int) -> list[dict[str, Any]]:
        cached = self._entries_cache.get(seat)
        if cached is not None and cached[0] == self.step_no:
            return cached[1]
        out = self._legal_entries(seat)
        self._entries_cache[seat] = (self.step_no, out)
        return out

    def _legal_entries(self, seat: int) -> list[dict[str, Any]]:
        h = self.heroes[seat]
        out = [{"action_id": f"s{seat}:button_1", "label": f"[seat {seat}] NoOp",
                "input": {"action": {"which_button": 1, "seat": seat}}}]
        if h.alive:
            out.append({"action_id": f"s{seat}:button_2", "label": f"[seat {seat}] Move",
                        "input": {"action": {"which_button": 2, "legal_move": list(range(1, 25)), "seat": seat}}})
            if self.t >= self.heal_ready.get(h.uid, 0.0):
                out.append({"action_id": f"s{seat}:button_11", "label": f"[seat {seat}] HealSkill",
                            "input": {"action": {"which_button": 11, "legal_offset_x": list(range(16, 27)),
                                                 "legal_offset_z": list(range(16, 27)), "legal_target": [0],
                                                 "seat": seat}}})
        for macro, (target, d) in self.macros_for(h).items():
            what = self.describe(target, h.camp)
            if macro == "macro_advance":
                label = f"Move toward the nearest {what} ({round(d)} away)"
            elif macro == "macro_retreat":
                label = f"Move back toward your own base ({round(d)} away)"
            elif macro == "macro_attack":
                label = f"Basic attack (nearest: {what}, {round(d)} away)"
            else:
                label = f"Cast skill {macro[-1]} toward {what} ({round(d)} away)"
            out.append({"action_id": f"s{seat}:{macro}", "label": f"[seat {seat}] {label}",
                        "input": {"action": {"macro": macro, "seat": seat}}})
        return out

    def result(self, camp: int) -> dict[str, Any]:
        reason = self.termination
        return {
            "is_terminal": True, "status": "completed", "termination_reason": reason,
            "outcome": {"winner_camp": self.winner,
                        "score": {str(c): {"enemy_structure_damage": round(self.structure_damage[c]),
                                           "kills": self.kills[c]} for c in (1, 2)}},
            "your_return": 1.0 if self.winner == camp else (0.0 if self.winner is None else -1.0),
        }
