"""friendly-fighter's Red Alert controller: a fast, scripted RTS player.

Red Alert is real time (25 ticks/s, a fresh view about 5 times a second), so
this player never waits on a model. Every view it is shown, it builds one
batch of orders from fixed rules:

1. deploy the MCV, place every finished building at once;
2. keep power ahead of demand, follow a build order (power, barracks,
   refinery, war factory, second refinery, service depot, defenses, radar,
   more factories/refineries as cash allows);
3. keep harvesters topped up and working;
4. keep the infantry and vehicle queues busy with the best unit available;
5. defend: everything near home attack-moves onto enemies close to the base;
6. attack in waves once the army is big enough, aimed at remembered enemy
   buildings, then the enemy spawn, then a sweep of the map (victory needs
   every enemy building dead).

Strategy knobs (posture, wave size, unit mix, target) live in ``Knobs``; the
optional OpenAI commander (``agent/commander.py``) may change them from a
background thread, but play never blocks on it.

Duplicate orders are the classic failure here: verdicts lag 0.2-0.4 s, so a
``build`` sent now is not in ``production`` on the next view. Every
build/train/place is remembered for a few seconds and treated as queued, and
orders the server refuses are cooled down instead of resent every tick.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from altruagent.console import print_line
from altruagent import WAIT, DecisionContext, GameState, WithReasoning
from examples.llm.redalert import order_key, refusal_rows, reason_still_holds

# --- Static game knowledge (overridden by context.game_config's rules) ---------------

BARRACKS = ("tent", "barr")
POWER = ("apwr", "powr")
DEFENSES = ("tsla", "gun", "ftur", "pbox", "hbox")
NONCOMBAT = frozenset(
    {"mcv", "harv", "medi", "mech", "e6", "spy", "thf", "truk", "mnly", "mrj", "mgg", "lst"}
)
NAVAL = frozenset({"ss", "msub", "dd", "ca", "pt", "lst"})
AIR = frozenset({"heli", "hind", "mig", "yak", "tran", "badr", "u2", "mh60"})
# Best first; filtered by available_production each time.
VEHICLE_PRIORITY = ("4tnk", "3tnk", "2tnk", "1tnk", "v2rl", "arty", "ftrk", "jeep", "apc")
SUPPORT_VEHICLES = ("v2rl", "arty")  # one of every SUPPORT_EVERY vehicles, when available
SUPPORT_EVERY = 4
INFANTRY_PRIORITY = ("e1", "e3")  # alternated
# unit_focus "antiinf" (rush-wave): against an infantry-heavy enemy, flamethrowers, grenadiers and
# riflemen instead of rocket soldiers; artillery as the only support vehicle (V2s are for buildings).
ANTI_INFANTRY_PRIORITY = ("e4", "e1", "e2")
# Focus-fire order for enemy buildings: construction yard (no rebuilding), then
# defenses, then production, then refineries, then the rest. Walls are ignored.
FOCUS_PRIORITY = (("fact",), DEFENSES, ("weap", "tent", "barr"), ("proc",))
WALLS = {"fenc", "brik", "sbag", "cycl", "barb", "wood"}
DEFAULT_QUEUES = {
    "Infantry": {"e1", "e2", "e3", "e4", "e6", "e7", "medi", "mech", "spy", "thf", "dog", "shok"},
    "Vehicle": {"mcv", "harv", "1tnk", "2tnk", "3tnk", "4tnk", "jeep", "apc", "arty", "v2rl", "ftrk",
                "mnly", "mrj", "mgg", "ttnk", "ctnk", "qtnk", "stnk", "dtrk", "truk"},
    "Defense": {"pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "gap", "iron", "pdox", "mslo"},
}
DEFAULT_COSTS = {
    "powr": 300, "apwr": 500, "tent": 500, "barr": 500, "proc": 1400, "weap": 2000, "fix": 1200,
    "dome": 1000, "pbox": 400, "hbox": 600, "gun": 600, "ftur": 600, "tsla": 1500, "harv": 1100,
    "e1": 100, "e3": 300, "1tnk": 700, "2tnk": 850, "3tnk": 1150, "4tnk": 1700, "jeep": 500,
    "apc": 850, "ftrk": 600, "arty": 600, "v2rl": 700,
}
DEFAULT_POWER = {
    "powr": 100, "apwr": 200, "tent": -20, "barr": -20, "proc": -30, "weap": -30, "fix": -30,
    "dome": -40, "pbox": 0, "hbox": 0, "gun": -40, "ftur": -20, "tsla": -100,
}

# --- Tuning ------------------------------------------------------------------------

PENDING_SECONDS = 3.0         # a sent build/train/place counts as queued this long
POWER_MARGIN = 20             # keep at least this much spare power
HARVESTERS_PER_REFINERY = 2
MAX_HARVESTERS = 6
EARLY_INFANTRY_CAP = 5        # infantry before the war factory is placed
# The "guard" opening (Knobs.opening): rushes hit at ~3:15-4:10 (OpenRA's Rush AI wiped every
# standard opening in the lab), and a second wave may follow at ~5:30-6:10; a defense right after
# the barracks, 10 infantry first, and no waves before GUARD_SECONDS beat one (docs/LAB.md).
GUARD_INFANTRY_CAP = 10
GUARD_SECONDS = 360
GUARD_DEFENSES = ("ftur", "pbox", "gun", "hbox", "tsla")  # cheap and good against infantry first
GUARD_STAGE_CELLS = 4         # the guard gathers this far from home toward the enemy (defenses go at 6)
GUARD_EMERGENCY_ARMY = 8      # attacked at home with fewer combat units than this: infantry from all the money
FIRST_WAVE_VEHICLES = 3       # the first wave waits for this many vehicles...
FIRST_WAVE_SECONDS = 360      # ...or until this much game time has passed
DEFEND_RADIUS = 16            # enemies this close to home are a threat
HOME_RADIUS = 25              # our units this close to home help defend
REORDER_SECONDS = 6.0         # don't re-order the same unit more often than this
DEFEND_REORDER_SECONDS = 3.0
WAVE_REORDER_SECONDS = 8.0
REFUSAL_COOLDOWN_SECONDS = 4.0
REFUSAL_COOLDOWN_MAX_SECONDS = 30.0
PLACE_ATTEMPTS_BEFORE_CANCEL = 6  # a ready building we fail to place this often is cancelled
MAX_ORDERS = 20
MAX_COMMANDS = 180            # the server allows 200 engine commands per batch
MAX_UNITS_PER_ORDER = 50
TIEBREAK_HOLD_SECONDS = 60    # last minute: no new waves when ahead on kills
SCOUT_GRID = 4                # sweep cells: an N x N grid over the playable area
TICKS_PER_SECOND = 25
STATUS_SECONDS = 30.0         # a status line in the log this often
VISION_CHECK_RADIUS = 5       # a remembered building cell this close to our unit is "seen"
SURPLUS_CASH = 1500           # free money above this buys more units (any focus) and factories
REINFORCE_GROUP = 4           # new units join a running wave in groups this big, never one by one
STAGE_RADIUS = 8              # units this close to the staging point count as gathered
ENGAGE_RADIUS = 10            # enemy units this close to the wave are fought first (attack-move)
FOCUS_RADIUS = 14             # visible enemy buildings this close to the wave get focus fire


@dataclass
class Knobs:
    """Strategy settings; the commander may replace them at any time."""

    posture: str = "attack"          # "eco" | "defend" | "attack"
    attack_threshold: int = 8        # combat units needed to launch a wave
    unit_focus: str = "mixed"        # "infantry" | "vehicles" | "mixed" | "rockets" (mixed, rocket infantry only) | "antiinf"
    target: str = "nearest"          # "nearest" | "enemy_base" | "harvesters"
    opening: str = "standard"        # "standard" | "guard": a defense and infantry at home through the rush window
    defenses: int = 2                # at most this many defenses (once the economy is up)
    greed: int = 0                   # extra refineries (up to 2 + greed) before the rest, and as many more harvesters
    early_refineries: int = 0        # refineries before the war factory beyond the first (income sooner, tanks later)
    aim: tuple[int, int] | None = None  # attack here (the commander's pick), unless enemies are in the way
    guard_defenses: int = 1          # the guard opening builds this many defenses once the barracks stands (rush-wave)
    guard_infantry: int = GUARD_INFANTRY_CAP  # ...and this many infantry before the war factory (rush-wave)
    late_buildings_from: float = 0.0  # no service depot or radar dome before this game time, seconds (rush-wave)
    guard_mix: bool = False          # the guard alternates rifles and rockets (v10's orders of 2 always pick rifles)
    defend_radius: int = DEFEND_RADIUS  # enemies this close to home pull the army out to fight (rush-wave: smaller = fight at the towers)
    note: str = ""


@dataclass
class _Refusal:
    order: dict[str, Any]
    reason: str
    count: int = 0
    until: float = 0.0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    return [x for x in value or [] if isinstance(x, dict)] if isinstance(value, list) else []


def _cell(obj: Any) -> tuple[int, int] | None:
    if isinstance(obj, dict) and isinstance(obj.get("x"), (int, float)) and isinstance(obj.get("y"), (int, float)):
        return int(obj["x"]), int(obj["y"])
    if isinstance(obj, (list, tuple)) and len(obj) == 2 and all(isinstance(v, (int, float)) for v in obj):
        return int(obj[0]), int(obj[1])
    return None


def _dist(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _centroid(cells: list[tuple[int, int]]) -> tuple[int, int] | None:
    if not cells:
        return None
    return round(sum(c[0] for c in cells) / len(cells)), round(sum(c[1] for c in cells) / len(cells))


def _commands(order: dict[str, Any]) -> int:
    if isinstance(order.get("units"), list):
        return len(order["units"])
    if order.get("cmd") == "train":
        return int(order.get("count") or 1)
    return 1


def is_combat(unit: dict[str, Any]) -> bool:
    kind = str(unit.get("type") or "").lower()
    return (
        not unit.get("wreck")
        and not kind.endswith("husk")
        and kind not in NONCOMBAT
        and kind not in NAVAL
        and kind not in AIR
    )


class RedAlertBot:
    """One Red Alert match. ``choose_action`` answers each view with a batch
    (``WithReasoning``) or ``WAIT``; ``on_action_result`` learns refusals."""

    def __init__(
        self,
        *,
        log: Callable[[str], None] = print_line,
        clock: Callable[[], float] | None = None,
        knobs: Knobs | None = None,
    ) -> None:
        self._log = log
        # Game time (the newest observation's tick / 25) by default: it runs at the wall clock's pace
        # on the platform, and keeps the bot's timers right when a local game runs faster (ffsim's
        # virtual time) or the agent falls behind.
        self._game_seconds = 0.0
        self._clock = clock or (lambda: self._game_seconds)
        self._lock = threading.Lock()
        self._knobs = knobs or Knobs()
        self._config_used = False
        self._queues: dict[str, str] = {}
        self._costs = dict(DEFAULT_COSTS)
        self._power = dict(DEFAULT_POWER)
        self._spawns: list[tuple[int, int]] = []
        self._pending: dict[tuple[str, str], float] = {}     # (cmd, item) -> sent at
        self._unit_ordered: dict[int, float] = {}            # unit id -> last ordered at
        self._rallied: set[int] = set()
        self._rally_point: tuple[int, int] | None = None
        self._refusals: dict[str, _Refusal] = {}
        self._place_attempts: dict[str, int] = {}
        self._repaired: dict[int, float] = {}
        self._last_sent: list[dict[str, Any]] = []
        self._enemy_buildings: dict[int, tuple[tuple[int, int], str]] = {}
        self._scouted: set[tuple[int, int]] = set()
        self._wave_active = False
        self._waves = 0
        self._wave_size = 0
        self._wave_ids: set[int] = set()
        self._focus_id: int | None = None
        self._vehicles_ordered = 0
        self._infantry_ordered = 0
        self._last_note = ""
        self._last_status = -1e9
        self.snapshot: dict[str, Any] = {}                    # read by the commander
        self.batches = 0

    # -- knobs (thread-safe) ----------------------------------------------------------

    @property
    def knobs(self) -> Knobs:
        with self._lock:
            return replace(self._knobs)

    def set_knobs(self, knobs: Knobs) -> None:
        with self._lock:
            self._knobs = knobs

    # -- the runner's hooks -----------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        raw = state.raw
        obs = raw.get("observation")
        if not isinstance(obs, dict):
            return WAIT
        self._use_config(context.game_config, raw)
        orders, notes = self.decide(raw)
        now = self._clock()
        if now - self._last_status >= STATUS_SECONDS:
            self._last_status = now
            economy = _dict(obs.get("economy"))
            military = _dict(obs.get("military"))
            self._log(
                f"[ff] status t={_dict(raw.get('time')).get('game_seconds')}s {obs.get('faction')}: "
                f"cash={economy.get('cash')} power={economy.get('power_provided')}/{economy.get('power_drained')} "
                f"buildings={len(_list(obs.get('buildings')))} units={len(_list(obs.get('units')))} "
                f"k/d={military.get('kills_cost')}/{military.get('deaths_cost')} waves={self._waves}"
            )
        if not orders:
            return WAIT
        self._last_sent = orders
        self.batches += 1
        summary = "; ".join(notes)[:280] or f"{len(orders)} orders"
        if summary != self._last_note:
            self._log(f"[ff] red_alert t={obs.get('tick')}: {summary}")
            self._last_note = summary
        return WithReasoning({"type": "orders", "orders": orders}, summary)

    def on_action_result(self, result: dict, context: DecisionContext) -> None:
        sent, self._last_sent = self._last_sent, []
        if not isinstance(result, dict):
            return
        if result.get("error") == "INVALID_ACTION":
            for index, _cmd, reason, _detail in refusal_rows(result.get("detail")):
                if 0 <= index < len(sent):
                    self._refuse(sent[index], reason)
            return
        if result.get("error"):
            # The batch never ran: forget its pending marks so it can be resent.
            for order in sent:
                self._pending.pop((order.get("cmd"), str(order.get("item") or "").lower()), None)
            return
        for row in _list(result.get("results")):
            index = row.get("index")
            if row.get("status") == "rejected" and isinstance(index, int) and 0 <= index < len(sent):
                self._refuse(sent[index], str(row.get("reason") or "rejected"))

    # -- the decision -----------------------------------------------------------------

    def decide(self, raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        obs = _dict(raw.get("observation"))
        if isinstance(obs.get("tick"), int):
            self._game_seconds = max(self._game_seconds, obs["tick"] / TICKS_PER_SECOND)
        now = self._clock()
        legal = _dict(raw.get("legal_actions"))
        knobs = self.knobs
        self._expire(now)

        units = [u for u in _list(obs.get("units")) if not u.get("wreck")]
        buildings = _list(obs.get("buildings"))
        production = _list(obs.get("production"))
        available = {str(x).lower() for x in obs.get("available_production") or []}
        economy = _dict(obs.get("economy"))
        cash = int(economy.get("cash") or 0) + int(economy.get("ore") or 0)
        types = [str(b.get("type") or "").lower() for b in buildings]
        home = _cell(obs.get("base_center")) or _centroid([c for c in map(_cell, buildings) if c]) or (0, 0)
        enemy_base = _cell(legal.get("enemy_base")) or self._enemy_spawn(home)
        self._remember_enemies(obs, units)

        orders: list[dict[str, Any]] = []
        notes: list[str] = []

        def add(order: dict[str, Any], note: str | None = None) -> bool:
            if len(orders) >= MAX_ORDERS or sum(map(_commands, orders)) + _commands(order) > MAX_COMMANDS:
                return False
            if self._blocked(order, raw):
                return False
            orders.append(order)
            if note:
                notes.append(note)
            return True

        # 1. Deploy the MCV when there is no construction yard.
        if "fact" not in types:
            mcvs = [u["id"] for u in units if str(u.get("type")).lower() == "mcv"]
            if mcvs and not self._is_pending("deploy", "mcv", now):
                add({"cmd": "deploy", "units": mcvs[:1]}, "deploy MCV")
                self._mark("deploy", "mcv", now)

        # 2. Place everything that is ready.
        ready_now = {str(p.get("item") or "").lower() for p in production if p.get("ready")}
        for name in list(self._place_attempts):
            if name not in ready_now:
                del self._place_attempts[name]
        for item in production:
            name = str(item.get("item") or "").lower()
            queue = str(item.get("queue") or "")
            if item.get("ready") and queue in ("Building", "Defense") and not self._is_pending("place", name, now):
                order: dict[str, Any] = {"cmd": "place", "item": name}
                spot = self._place_spot(name, queue, home, enemy_base, raw)
                if spot:
                    order["at"] = list(spot)
                if self._place_attempts.get(name, 0) >= PLACE_ATTEMPTS_BEFORE_CANCEL:
                    if add({"cmd": "cancel", "item": name}, f"cancel unplaceable {name}"):
                        self._place_attempts[name] = 0
                        self._mark("place", name, now)
                elif add(order, f"place {name}"):
                    self._mark("place", name, now)
                    self._place_attempts[name] = self._place_attempts.get(name, 0) + 1

        # 3-4. Base building: power first, then the build order.
        queued = self._queued(production, now)
        building_busy = queued["Building"] > 0
        # Money still owed to queued items (OpenRA takes it as production runs):
        # units are only bought from what is left, so buildings never starve.
        free = cash - self._owed(production, now)
        if "fact" in types and not building_busy:
            pick = self._next_building(types, available, economy, production, cash, knobs, now)
            if pick and add({"cmd": "build", "item": pick}, f"build {pick}"):
                self._mark("build", pick, now)
        if "fact" in types and queued["Defense"] == 0 and (knobs.posture != "eco" or knobs.opening == "guard"):
            pick = self._next_defense(types, available, free, knobs)
            if pick and add({"cmd": "build", "item": pick}, f"build {pick}"):
                self._mark("build", pick, now)

        # 5. Harvesters: enough of them, and all of them working.
        refineries = types.count("proc")
        harvesters = [u for u in units if str(u.get("type")).lower() == "harv"]
        target_harv = min(MAX_HARVESTERS + knobs.greed * HARVESTERS_PER_REFINERY,
                          refineries * HARVESTERS_PER_REFINERY + (1 if knobs.posture == "eco" else 0) + knobs.greed)
        want_harv = refineries > 0 and len(harvesters) + self._count_queued(production, "harv", now) < target_harv
        idle_harv = [u["id"] for u in harvesters
                     if u.get("idle") and now - self._unit_ordered.get(u["id"], -1e9) >= REORDER_SECONDS]
        if idle_harv and add({"cmd": "harvest", "units": idle_harv[:MAX_UNITS_PER_ORDER]}):
            for uid in idle_harv[:MAX_UNITS_PER_ORDER]:
                self._unit_ordered[uid] = now

        # 6. Army production. (The guard opening, attacked at home with a small army: infantry from
        # whatever money there is, before anything else.)
        if knobs.opening == "guard" and self._threats(obs, home, knobs.defend_radius) and \
                sum(1 for u in units if is_combat(u)) < GUARD_EMERGENCY_ARMY:
            infantry = [i for i in INFANTRY_PRIORITY if i in available]
            if infantry and queued["Infantry"] == 0 and cash >= self._costs.get(infantry[0], 100):
                pick = infantry[self._infantry_ordered % len(infantry)]
                if add({"cmd": "train", "item": pick, "count": 3}, "emergency infantry"):
                    self._mark("train", pick, now)
                    self._infantry_ordered += 3
                    queued["Infantry"] += 1
        if want_harv and "harv" in available and queued["Vehicle"] == 0 and free >= 300:
            if add({"cmd": "train", "item": "harv", "count": 1}, "train harv"):
                self._mark("train", "harv", now)
                queued["Vehicle"] += 1
                free -= self._costs.get("harv", 1100)
        self._train_army(add, available, queued, free, types, knobs, now)

        # Rally new units to a staging point on the way to the enemy (the guard opening: next to the
        # base's defense until its window ends; buildings are rallied again when the point moves).
        stage = self._staging(home, enemy_base, raw)
        if knobs.opening == "guard" and now < GUARD_SECONDS and enemy_base:
            dx, dy = enemy_base[0] - home[0], enemy_base[1] - home[1]
            length = math.hypot(dx, dy) or 1.0
            stage = self._clamp((round(home[0] + dx / length * GUARD_STAGE_CELLS),
                                 round(home[1] + dy / length * GUARD_STAGE_CELLS)), raw)
        if knobs.opening == "guard":
            if self._rally_point is not None and _dist(self._rally_point, stage) > 2:
                self._rallied.clear()
            self._rally_point = stage
        for b in buildings:
            if b.get("id") in self._rallied or str(b.get("type")).lower() not in ("tent", "barr", "weap"):
                continue
            if add({"cmd": "rally", "building": b["id"], "to": list(stage)}):
                self._rallied.add(b["id"])

        # 7. Repairs.
        repair = legal.get("repair") or []
        if cash > 500:
            for entry in repair[:2]:
                bid = entry.get("id") if isinstance(entry, dict) else entry
                if isinstance(bid, int) and now - self._repaired.get(bid, -1e9) >= REORDER_SECONDS:
                    if add({"cmd": "repair", "building": bid, "on": True}):
                        self._repaired[bid] = now

        # 8. Defend, else attack.
        combat = [u for u in units if is_combat(u)]
        threats = self._threats(obs, home, knobs.defend_radius)
        if threats:
            self._defend(add, combat, threats, home, now, notes)
        else:
            self._attack(add, combat, raw, home, enemy_base, stage, knobs, now, notes)

        self._update_snapshot(raw, units, buildings, combat, threats, knobs)
        return orders, notes

    # -- building choices ---------------------------------------------------------------

    def _next_building(self, types, available, economy, production, cash, knobs: Knobs, now: float = 0.0) -> str | None:
        have = {t: types.count(t) for t in set(types)}
        spare = int(economy.get("power_provided") or 0) - int(economy.get("power_drained") or 0)
        barracks = next((b for b in BARRACKS if b in available), None)
        has_barracks = any(have.get(b) for b in BARRACKS)

        def can(name: str | None) -> bool:
            return bool(name) and name in available

        plan: list[str | None] = []
        if not have.get("powr") and not have.get("apwr"):
            plan.append("powr")
        if not have.get("proc"):
            plan.append("proc")
        if not has_barracks:
            plan.append(barracks)
        if knobs.early_refineries and not have.get("weap") and have.get("proc", 0) < 1 + knobs.early_refineries:
            plan.append("proc")
        if not have.get("weap"):
            plan.append("weap")
        if have.get("proc", 0) < 2:
            plan.append("proc")
        if knobs.greed and have.get("proc", 0) < 2 + knobs.greed:
            plan.append("proc")
        if now >= knobs.late_buildings_from:  # (rush-wave: money for the guard first)
            if not have.get("fix"):
                plan.append("fix")
            if not have.get("dome"):
                plan.append("dome")
        if knobs.posture == "eco" and have.get("proc", 0) < 3:
            plan.append("proc")
        if cash > SURPLUS_CASH and have.get("weap", 0) < 2 and have.get("proc", 0) >= 2:
            plan.append("weap")
        if cash > 3000 and have.get("proc", 0) < 3:
            plan.append("proc")
        if cash > 3000 and have.get("weap", 0) < 3:
            plan.append("weap")
        if cash > 2500 and has_barracks and sum(have.get(b, 0) for b in BARRACKS) < 2 and have.get("weap", 0) >= 2:
            plan.append(barracks)
        nxt = next((p for p in plan if can(p)), None)

        # Power: keep spare power ahead of the next building's drain.
        drain = -min(0, self._power.get(nxt or "", 0)) if nxt else 0
        if spare - drain < POWER_MARGIN or (nxt is None and spare < 60 and cash > 1500):
            power = next((p for p in POWER if p in available and (p != "apwr" or cash >= 800)), None)
            if power:
                return power
        return nxt

    def _next_defense(self, types, available, free, knobs: Knobs | None = None) -> str | None:
        """At most two defenses, only once the economy is up (war factory and two
        refineries), and only from money nothing else is waiting for. The guard opening
        builds one as soon as the barracks stands."""
        have = sum(types.count(d) for d in DEFENSES)
        if (knobs is not None and knobs.opening == "guard" and have < knobs.guard_defenses
                and any(b in types for b in BARRACKS)):
            return next((d for d in GUARD_DEFENSES if d in available and self._costs.get(d, 600) <= free), None)
        if have >= (knobs.defenses if knobs is not None else 2) or "weap" not in types or types.count("proc") < 2:
            return None
        return next((d for d in DEFENSES if d in available and self._costs.get(d, 600) + 500 <= free), None)

    def _place_spot(self, name, queue, home, enemy_base, raw) -> tuple[int, int] | None:
        """Defenses go a few cells toward the enemy; buildings use the engine's
        own placement next to the construction yard (always valid if room)."""
        if queue != "Defense" or not enemy_base:
            return None
        dx, dy = enemy_base[0] - home[0], enemy_base[1] - home[1]
        length = math.hypot(dx, dy) or 1.0
        spot = (round(home[0] + dx / length * 6), round(home[1] + dy / length * 6))
        return self._clamp(spot, raw)

    # -- army -------------------------------------------------------------------------

    def _train_army(self, add, available, queued, free, types, knobs: Knobs, now) -> None:
        """Units from free money only. Until the war factory stands, a few
        infantry for defense and the rest saved for the factory."""
        has_proc = "proc" in types
        reserve = 0 if "weap" in types else self._costs.get("weap", 2000) // 2
        # Infantry: alternate rifles and rockets.
        infantry = [i for i in INFANTRY_PRIORITY if i in available]
        if knobs.unit_focus == "rockets" and "e3" in available:  # (against aircraft and armor)
            infantry = ["e3"]
        if knobs.unit_focus == "antiinf":  # (against an infantry-heavy enemy; falls back to the default mix)
            infantry = [i for i in ANTI_INFANTRY_PRIORITY if i in available] or infantry
        surplus = free - reserve > SURPLUS_CASH
        want_infantry = knobs.unit_focus != "vehicles" or "weap" not in types or surplus
        if (knobs.opening == "guard" and now < GUARD_SECONDS and infantry and queued["Infantry"] == 0 and has_proc
                and self._infantry_ordered < knobs.guard_infantry):
            # The guard: infantry first, whatever the buildings are waiting for (the queue waits for
            # money like any other: OpenRA shares it out as production runs).
            pick = infantry[(self._infantry_ordered // 2 if knobs.guard_mix else self._infantry_ordered) % len(infantry)]
            if add({"cmd": "train", "item": pick, "count": 2}):
                self._mark("train", pick, now)
                self._infantry_ordered += 2
            infantry = []
        if infantry and queued["Infantry"] == 0 and want_infantry:
            cap = knobs.guard_infantry if knobs.opening == "guard" else EARLY_INFANTRY_CAP
            early_cap = self._infantry_ordered >= (cap if "weap" not in types else 10**6)
            early_cap = early_cap or (not has_proc and self._infantry_ordered >= 2)
            pick = infantry[self._infantry_ordered % len(infantry)]
            if not early_cap and free - reserve >= self._costs.get(pick, 100) * 2:
                count = 3 if surplus else 1
                if add({"cmd": "train", "item": pick, "count": count}):
                    self._mark("train", pick, now)
                    self._infantry_ordered += count
        # Vehicles: the best tank, with some artillery mixed in.
        vehicles = [v for v in VEHICLE_PRIORITY if v in available]
        if vehicles and queued["Vehicle"] == 0 and knobs.unit_focus != "infantry":
            pick = vehicles[0]
            support = [v for v in SUPPORT_VEHICLES if v in available and (v == "arty" or knobs.unit_focus != "antiinf")]
            if support and self._vehicles_ordered % SUPPORT_EVERY == SUPPORT_EVERY - 1:
                pick = support[0]
            cost = self._costs.get(pick, 800)
            if free >= cost * 0.6:
                count = 2 if free - cost >= SURPLUS_CASH else 1
                if add({"cmd": "train", "item": pick, "count": count}):
                    self._mark("train", pick, now)
                    self._vehicles_ordered += count

    def _threats(self, obs, home, radius: int = DEFEND_RADIUS) -> list[tuple[int, int]]:
        cells = []
        for enemy in _list(obs.get("visible_enemies")):
            cell = _cell(enemy)
            if cell and not enemy.get("wreck") and not str(enemy.get("type") or "").endswith("husk"):
                if _dist(cell, home) <= radius:
                    cells.append(cell)
        return cells

    def _defend(self, add, combat, threats, home, now, notes) -> None:
        target = min(threats, key=lambda c: _dist(c, home))
        def near_home(u: dict[str, Any]) -> bool:
            cell = _cell(u)
            return cell is not None and _dist(cell, home) <= HOME_RADIUS

        helpers = [
            u["id"] for u in combat
            if (near_home(u) or not self._wave_active)
            and now - self._unit_ordered.get(u["id"], -1e9) >= DEFEND_REORDER_SECONDS
        ]
        if self._order_units(add, "attack_move", helpers, target, now):
            notes.append(f"defend base vs {len(threats)} enemies at {list(target)}")

    def _attack(self, add, combat, raw, home, enemy_base, stage, knobs: Knobs, now, notes) -> None:
        time_info = _dict(raw.get("time"))
        military = _dict(_dict(raw.get("observation")).get("military"))
        remaining = time_info.get("remaining_seconds")
        ahead = int(military.get("kills_cost") or 0) - int(military.get("deaths_cost") or 0) > 0
        hold = isinstance(remaining, (int, float)) and remaining < TIEBREAK_HOLD_SECONDS and ahead
        # Later waves wait for a slightly bigger army.
        threshold = max(3, int(knobs.attack_threshold)) + 2 * min(self._waves, 3)

        guarding = (knobs.opening == "guard" and isinstance(time_info.get("game_seconds"), (int, float))
                    and time_info["game_seconds"] < GUARD_SECONDS)
        if knobs.posture == "defend" or hold or guarding:
            self._wave_active = False
            return
        vehicles = sum(1 for u in combat if self._queue_of(str(u.get("type") or "").lower()) == "Vehicle")
        game_seconds = time_info.get("game_seconds")
        late = isinstance(game_seconds, (int, float)) and game_seconds >= FIRST_WAVE_SECONDS
        ready = self._waves > 0 or vehicles >= FIRST_WAVE_VEHICLES or late
        alive = {u["id"] for u in combat}
        if not self._wave_active and ready and len(combat) >= threshold:
            self._wave_active = True
            self._waves += 1
            self._wave_ids = set(alive)
            self._wave_size = len(combat)
            notes.append(f"wave {self._waves}: {len(combat)} units attack")
        if self._wave_active:
            self._wave_ids &= alive
            # Newcomers wait at the staging point and join as a group, so they
            # don't trickle into the enemy base one at a time.
            gathered = [u["id"] for u in combat if u["id"] not in self._wave_ids
                        and _cell(u) and _dist(_cell(u), stage) <= STAGE_RADIUS]
            if len(gathered) >= REINFORCE_GROUP:
                self._wave_ids.update(gathered)
                self._wave_size = max(self._wave_size, len(self._wave_ids))
                notes.append(f"{len(gathered)} reinforcements join wave {self._waves}")
            if len(self._wave_ids) < max(2, self._wave_size // 3):
                self._wave_active = False
                self._wave_ids = set()
                notes.append("wave spent, regrouping")

        wave = [u for u in combat if u["id"] in self._wave_ids]
        # Everyone outside the wave gathers at the staging point.
        idle = [u["id"] for u in combat if u["id"] not in self._wave_ids and u.get("idle") and _cell(u)
                and _dist(_cell(u), stage) > 6 and now - self._unit_ordered.get(u["id"], -1e9) >= REORDER_SECONDS]
        self._order_units(add, "move", idle, stage, now)
        if not wave:
            self._focus_id = None
            return

        army_center = _centroid([c for c in (_cell(u) for u in wave) if c]) or home
        def due(u: dict[str, Any]) -> bool:
            since = now - self._unit_ordered.get(u["id"], -1e9)
            return since >= WAVE_REORDER_SECONDS or (bool(u.get("idle")) and since >= 2.0)

        # No enemy units to fight nearby: focus fire on the most important building in reach.
        focus = None if self._enemy_units_near(raw, army_center) else self._focus_building(raw, army_center)
        if focus:
            bid, kind = focus
            changed = bid != self._focus_id
            self._focus_id = bid
            attackers = [u["id"] for u in wave if changed or due(u)]
            for start in range(0, len(attackers), MAX_UNITS_PER_ORDER):
                chunk = attackers[start:start + MAX_UNITS_PER_ORDER]
                if add({"cmd": "attack", "units": chunk, "target": bid}):
                    for uid in chunk:
                        self._unit_ordered[uid] = now
            if attackers:
                notes.append(f"focus fire on {kind} #{bid} with {len(attackers)}")
            return
        if self._focus_id is not None:
            self._focus_id = None
            for u in wave:  # the focus target is gone: move on at once
                self._unit_ordered.pop(u["id"], None)

        target = self._target(raw, army_center, enemy_base, knobs)
        movers = [u["id"] for u in wave if due(u)]
        if target and self._order_units(add, "attack_move", movers, target, now):
            notes.append(f"attack-move {len(movers)} to {list(target)}")

    def _enemy_units_near(self, raw, center) -> bool:
        obs = _dict(raw.get("observation"))
        return any(
            (c := _cell(e)) and not e.get("wreck") and not str(e.get("type") or "").endswith("husk")
            and _dist(c, center) <= ENGAGE_RADIUS
            for e in _list(obs.get("visible_enemies"))
        )

    def _focus_building(self, raw, center) -> tuple[int, str] | None:
        best = None
        for b in _list(_dict(raw.get("observation")).get("visible_enemy_buildings")):
            cell, kind = _cell(b), str(b.get("type") or "").lower()
            if not cell or b.get("wreck") or not isinstance(b.get("id"), int) or kind in WALLS:
                continue
            if _dist(cell, center) > FOCUS_RADIUS:
                continue
            rank = next((i for i, group in enumerate(FOCUS_PRIORITY) if kind in group), len(FOCUS_PRIORITY))
            key = (rank, _dist(cell, center))
            if best is None or key < best[0]:
                best = (key, b["id"], kind)
        return (best[1], best[2]) if best else None

    def _target(self, raw, center, enemy_base, knobs: Knobs) -> tuple[int, int] | None:
        obs = _dict(raw.get("observation"))
        if knobs.target == "harvesters":
            harvs = [c for e in _list(obs.get("visible_enemies"))
                     if str(e.get("type")).lower() == "harv" and not e.get("wreck") and (c := _cell(e))]
            if harvs:
                return min(harvs, key=lambda c: _dist(c, center))
        visible = [c for e in _list(obs.get("visible_enemies")) if not e.get("wreck") and (c := _cell(e))]
        if knobs.target == "nearest" and visible:
            near = min(visible, key=lambda c: _dist(c, center))
            if _dist(near, center) < 12:
                return near
        if knobs.aim is not None:
            return knobs.aim
        known = [cell for cell, _ in self._enemy_buildings.values()]
        if known:
            return min(known, key=lambda c: _dist(c, center))
        if enemy_base and enemy_base not in self._scouted:
            if _dist(center, enemy_base) < 5:
                self._scouted.add(enemy_base)
            else:
                return enemy_base
        return self._scout_target(raw, center)

    def _scout_target(self, raw, center) -> tuple[int, int] | None:
        bounds = _dict(raw.get("map")).get("bounds")
        if not (isinstance(bounds, list) and len(bounds) == 4):
            return None
        x, y, w, h = (int(v) for v in bounds)
        cells = [
            (x + round((i + 0.5) * w / SCOUT_GRID), y + round((j + 0.5) * h / SCOUT_GRID))
            for i in range(SCOUT_GRID) for j in range(SCOUT_GRID)
        ]
        for cell in cells:
            if cell not in self._scouted and _dist(cell, center) < 5:
                self._scouted.add(cell)
        left = [c for c in cells if c not in self._scouted]
        if not left:
            self._scouted.clear()
            left = cells
        return min(left, key=lambda c: _dist(c, center))

    def _order_units(self, add, cmd, ids, to, now) -> bool:
        sent = False
        for start in range(0, len(ids), MAX_UNITS_PER_ORDER):
            chunk = ids[start:start + MAX_UNITS_PER_ORDER]
            if add({"cmd": cmd, "units": chunk, "to": list(to)}):
                for uid in chunk:
                    self._unit_ordered[uid] = now
                sent = True
        return sent

    # -- memory ---------------------------------------------------------------------------

    def _remember_enemies(self, obs, units) -> None:
        visible_ids = set()
        for b in _list(obs.get("visible_enemy_buildings")):
            cell = _cell(b)
            if cell and isinstance(b.get("id"), int) and not b.get("wreck"):
                self._enemy_buildings[b["id"]] = (cell, str(b.get("type") or ""))
                visible_ids.add(b["id"])
        # A remembered building whose cell we can see but that isn't visible is gone.
        ours = [c for c in (_cell(u) for u in units) if c]
        for bid, (cell, _) in list(self._enemy_buildings.items()):
            if bid not in visible_ids and any(_dist(cell, c) <= VISION_CHECK_RADIUS for c in ours):
                del self._enemy_buildings[bid]

    def _queued(self, production, now) -> dict[str, int]:
        counts = {"Building": 0, "Defense": 0, "Infantry": 0, "Vehicle": 0}
        seen: set[str] = set()
        for p in production:
            queue = str(p.get("queue") or "")
            if queue in counts:
                counts[queue] += 1
                seen.add(str(p.get("item") or "").lower())
        for (cmd, item), _ in self._pending.items():
            if cmd in ("build", "train") and item not in seen:
                queue = self._queue_of(item)
                if queue in counts:
                    counts[queue] += 1
        return counts

    def _count_queued(self, production, item, now) -> int:
        n = sum(1 for p in production if str(p.get("item") or "").lower() == item)
        return n or (1 if self._is_pending("train", item, now) else 0)

    def _owed(self, production, now) -> int:
        """Credits still to be paid for queued items, plus orders not yet shown."""
        owed = 0.0
        seen: set[str] = set()
        for p in production:
            item = str(p.get("item") or "").lower()
            seen.add(item)
            if not p.get("ready"):
                progress = p.get("progress") if isinstance(p.get("progress"), (int, float)) else 0.0
                owed += self._costs.get(item, 0) * max(0.0, 1.0 - progress)
        for (cmd, item), _ in self._pending.items():
            if cmd in ("build", "train") and item not in seen:
                owed += self._costs.get(item, 0)
        return int(owed)

    def _queue_of(self, item: str) -> str:
        if item in self._queues:
            return self._queues[item]
        for queue, items in DEFAULT_QUEUES.items():
            if item in items:
                return queue
        return "Building"

    def _mark(self, cmd: str, item: str, now: float) -> None:
        self._pending[(cmd, item)] = now

    def _is_pending(self, cmd: str, item: str, now: float) -> bool:
        sent = self._pending.get((cmd, item))
        return sent is not None and now - sent < PENDING_SECONDS

    def _expire(self, now: float) -> None:
        for key, sent in list(self._pending.items()):
            if now - sent >= PENDING_SECONDS:
                del self._pending[key]

    # -- refusals -----------------------------------------------------------------------

    def _refuse(self, order: dict[str, Any], reason: str) -> None:
        if order.get("cmd") in ("build", "train", "place", "deploy"):
            self._pending.pop((order.get("cmd"), str(order.get("item") or "mcv").lower()), None)
        key = order_key(order)
        entry = self._refusals.get(key) or _Refusal(order=order, reason=reason)
        entry.reason = reason
        entry.count += 1
        cooldown = min(REFUSAL_COOLDOWN_MAX_SECONDS, REFUSAL_COOLDOWN_SECONDS * 2 ** (entry.count - 1))
        entry.until = self._clock() + cooldown
        self._refusals[key] = entry

    def _blocked(self, order: dict[str, Any], raw: dict[str, Any]) -> bool:
        entry = self._refusals.get(order_key(order))
        if entry is None or self._clock() >= entry.until:
            return False
        return reason_still_holds({"order": entry.order, "reason": entry.reason}, raw)


    # -- map ------------------------------------------------------------------------------

    def _use_config(self, config: dict | None, raw: dict[str, Any]) -> None:
        if self._config_used or not isinstance(config, dict):
            return
        self._config_used = True
        rules = _dict(config.get("rules"))
        for section in ("buildings", "units"):
            for name, info in _dict(rules.get(section)).items():
                info = _dict(info)
                key = str(name).lower()
                if info.get("queue"):
                    self._queues[key] = str(info["queue"])
                if isinstance(info.get("cost"), (int, float)):
                    self._costs[key] = int(info["cost"])
                if isinstance(info.get("power"), (int, float)):
                    self._power[key] = int(info["power"])
        map_id = _dict(raw.get("map")).get("id")
        entry = _dict(_dict(rules.get("maps")).get(map_id or ""))
        self._spawns = [c for c in (_cell(s) for s in entry.get("spawns") or []) if c]

    def _enemy_spawn(self, home) -> tuple[int, int] | None:
        if not self._spawns:
            return None
        return max(self._spawns, key=lambda c: _dist(c, home))

    def _staging(self, home, enemy_base, raw) -> tuple[int, int]:
        if not enemy_base:
            return home
        spot = (round(home[0] + (enemy_base[0] - home[0]) * 0.2), round(home[1] + (enemy_base[1] - home[1]) * 0.2))
        return self._clamp(spot, raw)

    def _clamp(self, cell: tuple[int, int], raw: dict[str, Any]) -> tuple[int, int]:
        bounds = _dict(raw.get("map")).get("bounds")
        if not (isinstance(bounds, list) and len(bounds) == 4):
            return cell
        x, y, w, h = (int(v) for v in bounds)
        return min(max(cell[0], x + 1), x + w - 2), min(max(cell[1], y + 1), y + h - 2)

    # -- for the commander ------------------------------------------------------------------

    def _update_snapshot(self, raw, units, buildings, combat, threats, knobs: Knobs) -> None:
        obs = _dict(raw.get("observation"))
        army: dict[str, int] = {}
        for u in combat:
            army[str(u.get("type"))] = army.get(str(u.get("type")), 0) + 1
        enemies: dict[str, int] = {}
        for e in _list(obs.get("visible_enemies")):
            if not e.get("wreck"):
                enemies[str(e.get("type"))] = enemies.get(str(e.get("type")), 0) + 1
        snapshot = {
            "time": _dict(raw.get("time")),
            "faction": obs.get("faction"),
            "enemy_faction": obs.get("enemy_faction"),
            "economy": _dict(obs.get("economy")),
            "military": _dict(obs.get("military")),
            "our_buildings": sorted(str(b.get("type")) for b in buildings),
            "our_army": army,
            "visible_enemy_units": enemies,
            "known_enemy_buildings": sorted(t for _, t in self._enemy_buildings.values()),
            "threats_near_base": len(threats),
            "wave_active": self._wave_active,
            "waves_sent": self._waves,
            "explored_percent": obs.get("explored_percent"),
            "current_knobs": {
                "posture": knobs.posture, "attack_threshold": knobs.attack_threshold,
                "unit_focus": knobs.unit_focus, "target": knobs.target,
            },
        }
        with self._lock:
            self.snapshot = snapshot

    def read_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.snapshot)
