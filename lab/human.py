"""A seat for a person (or Claude) at the controls: the game waits while you think.

    lab human start --vs ai-normal [--map trapped] [--seat 0] [--autopilot macro] [--name claude]
    lab human start --vs ff-turtle --from <game> --at 4:30     # from a moment of a recorded game
    lab h state | map [X Y [R]] | units [TYPE] | enemies | log [N] | events [N]
    lab h build powr | train 3tnk 2 | place powr [X Y] | deploy | attack X Y [UNITS] | move X Y UNITS
    lab h target ENEMY_ID [UNITS] | stop UNITS | harvest [UNITS] | sell ID | repair ID | rally ID X Y | raw JSON
    lab h go [SECONDS]        # run the game this long (default 5 s), sending what you queued
    lab h autopilot off|macro|full   lab h knobs posture=defend ...   lab h note TEXT   lab h resign | quit

The seat's think time is ``paused`` (``ffsim.vclock``): between your commands no game time passes,
so you can study the state as long as you like. ``go`` plays on: your queued orders go out in the
first batch, then each view the autopilot (if on) adds its own orders, until the time is up.
Autopilot ``macro`` is our bot's economy and production (building, placing, training, harvesters,
repairs, rallies) with no unit orders: the army is yours. ``full`` is our bot playing everything,
your orders added on top. ``off``: only what you send.

UNITS: ``all`` (combat units), ``idle``, a type (``3tnk``), ``near:X,Y,R``, or ids (``12 13`` or
``12,13``). Every command and its answer goes to the lab database (``human_log``), and the game
itself like any other lab game.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from altruagent import WithReasoning
from altruagent.models import DecisionContext
from altruagent.mcp_transport import MCPToolError

from . import LAB_DIR
from .driver import LocalGame
from .capture import summarize_orders
from .views import clock, enemies_table, is_combat, minimap, summary, units_table

SESSION_FILE = LAB_DIR / "human" / "session.json"
HELP = """look:   state [json] | map [X Y [R]] | units [TYPE|combat] | enemies | events [N] | log [N] | queue
play:   go [SECONDS] [nostop]  (runs the game; default 5, at most 120; stops early when enemies reach the
        base or our units come under fire after a quiet spell, unless nostop; the game waits between
        commands, so a fight can be played 2-3 seconds at a time)
orders: build ITEM [at X Y] (placed there when ready) | train ITEM [N] | place ITEM [X Y] | deploy [ID]
        harvest [IDS] | rally all X Y (every production building, new ones too) | rally off
        attack X Y [UNITS] (attack-move) | move X Y UNITS | target ENEMY_ID [UNITS] | stop UNITS
        (add 'then' to attack/move: a waypoint after the current orders)
        bombard X Y UNITS (fire at the ground there: artillery hits what it can't see; arty 12, v2rl 10)
        stance defend|return_fire|hold_fire|attack_anything UNITS (defend: hold ground, don't chase)
        sell ID | repair ID | rally BUILDING_ID X Y | raw '<order JSON or list>' | clear
units:  all (combat units, the default) | idle | a type (3tnk) | near:X,Y,R | ids (12 13 or 12,13) | @GROUP
groups: group NAME UNITS (saves a selection: then attack 40 40 @NAME) | group (lists them)
intel:  enemies (in sight, and every enemy building seen so far) | history (where this opponent built in
        earlier lab games on this map: its defenses before you meet them)
help:   autopilot off|macro|full (macro: our bot runs economy and production, the army is yours)
        knobs posture=defend attack_threshold=12 ... (the autopilot's)
other:  info ITEM... (cost, time, power, needs, weapon) | note TEXT | resign | quit (closes the session)"""
MACRO_CMDS = {"deploy", "build", "place", "train", "harvest", "repair", "power_toggle", "rally", "cancel", "sell"}
PRODUCERS = {"tent", "barr", "weap", "kenn", "afld", "hpad", "syrd", "spen"}
STANCES = ("defend", "return_fire", "hold_fire", "attack_anything")
DEFENSES = {"tsla", "ftur", "pbox", "hbox", "gun", "sam", "agun", "gap", "iron", "pdox", "mslo"}
FRAGILE = {"v2rl", "arty", "harv", "mcv"}  # units that die fast when the enemy gets close
AIR = {"heli", "hind", "mh60", "tran", "mig", "yak", "badr", "u2"}
MINES = {"minv", "minp", "mnly"}
UNIT_NOTES = {"mh60": "helicopter: rocket infantry or AA", "heli": "helicopter: rocket infantry or AA",
              "hind": "helicopter: rocket infantry or AA", "mig": "jet: AA", "yak": "plane: AA",
              "mnly": "lays mines", "minv": "anti-vehicle mine", "minp": "anti-personnel mine",
              "arty": "artillery, range 12: kills from out of sight", "v2rl": "rockets, range 10",
              "e3": "rocket infantry: kills tanks and V2s", "e4": "flamethrower: kills infantry blobs",
              "apc": "carries infantry", "jeep": "fast raider", "1tnk": "light tank, fast"}
ALERT_RADIUS = 18  # enemies this close to the base center...
ALERT_UNITS = 2    # ...this many of them: an attack (go stops)
CONTACT_QUIET_SECONDS = 10  # our units under fire after this long without: contact (go stops)
MAX_ORDERS = 20
GO_MAX_SECONDS = 120


def parse_units(obs: dict, words: list[str], groups: dict | None = None) -> list[int]:
    units = [u for u in obs.get("units") or [] if not u.get("wreck")]
    combat = [u for u in units if is_combat(u)]
    alive = {u["id"] for u in units}
    if not words or words == ["all"]:
        return [u["id"] for u in combat]
    out: list[int] = []
    for w in words:
        if w.startswith("@"):
            out += [i for i in (groups or {}).get(w[1:], []) if i in alive]
        elif w == "idle":
            out += [u["id"] for u in combat if u.get("idle")]
        elif w.startswith("near:"):
            x, y, r = (float(v) for v in w[5:].split(","))
            out += [u["id"] for u in combat if math.hypot(u["x"] - x, u["y"] - y) <= r]
        elif all(p.strip().isdigit() for p in w.split(",") if p.strip()):
            out += [int(p) for p in w.split(",") if p.strip()]
        else:
            out += [u["id"] for u in units if u["type"] == w]
    return list(dict.fromkeys(out))


def item_info(names: list[str]) -> str:
    """Cost, build time, power, prerequisites, health and weapon of units and buildings (the game's rules)."""
    from ffsim.rules import Rules

    rules = Rules.load().config.get("rules") or {}
    table = {**(rules.get("buildings") or {}), **(rules.get("units") or {})}
    if not names:
        return "info ITEM [ITEM...]: e.g. info 3tnk tsla"
    lines = []
    for n in names:
        r = table.get(n.lower())
        if not r:
            lines.append(f"{n}: unknown")
            continue
        w = r.get("weapon") or {}
        lines.append(f"{n}: ${r.get('cost')}, {r.get('build_time_s')} s, queue {r.get('queue')}, power {r.get('power')}, "
                     f"needs {','.join(r.get('prerequisites') or []) or '-'}, hp {r.get('hp')}"
                     + (f", speed {r['speed']}" if r.get("speed") else "")
                     + (f", {w.get('weapon')} range {w.get('range_cells')} damage {w.get('damage')}" if w else "")
                     + (f", factions {','.join(r.get('factions') or [])}" if r.get("factions") else ""))
    return "\n".join(lines)


class HumanSeat:
    """The controller of a human seat (a thread of the game's process); ``command(argv)`` runs one
    command and returns its answer as text."""

    def __init__(self, platform, session_id: str, position: int, bot, *, autopilot: str = "macro",
                 knobs: dict | None = None, db=None) -> None:
        self.game = LocalGame(platform, session_id, position)
        self.platform = platform
        self.session_id = session_id
        self.position = position
        self.bot = bot
        self.db = db
        self.autopilot = autopilot
        self.knobs = knobs or {}
        self.queued: list[dict] = []
        self.state = None
        self.context = None
        self.assistant = None
        self.history: list[dict] = []  # batches sent: tick, mine, theirs, verdicts
        self.problems_seen: set = set()
        self.placements: dict[str, tuple[int, int]] = {}  # item -> where to place it when ready (ours, not the autopilot's)
        self.placing: dict[str, float] = {}  # item -> game time a place order went out
        self.rally_point: tuple[int, int] | None = None
        self.rallied: set[int] = set()
        self.groups: dict[str, list[int]] = {}
        self.asked_at: dict[str, tuple[int, int]] = {}  # item -> where it was asked to stand (to report where it went)
        self.blind_ticks: set[int] = set()  # views with no enemy in sight (a loss there: an unseen killer)
        self.enemy_buildings: dict[int, tuple[str, int, int, int]] = {}  # id -> (type, x, y, tick last seen)
        self.threat = False
        self.contact = False
        self.last_contact_tick = 0
        self.alert: str | None = None
        self.warned: set[str] = set()
        self.events: list[dict] = []
        self.new_events: list[dict] = []
        self.error: str | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="human-seat", daemon=True)
        match = platform.matches[session_id]
        match.seats[position].tracker.sink = self._chain(match.seats[position].tracker.sink)

    def _chain(self, previous):
        def sink(ev):
            if previous is not None:
                previous(ev)
            self.events.append(ev)
            self.new_events.append(ev)
        return sink

    # -- the game's thread -----------------------------------------------------------------

    def start(self) -> "HumanSeat":
        self.thread.start()
        return self

    def join(self, timeout: float | None = None) -> None:
        self.done.set()
        self.jobs.put(None)
        self.thread.join(timeout)

    def _loop(self) -> None:
        try:
            self.state = self.game.get_state()  # (checks in: the game starts once both seats have)
            self.context = DecisionContext(session_id=self.session_id, tournament_id=None, game_type="red_alert",
                                           agent_id=self.game.platform.matches[self.session_id].seats[self.position].config.agent_id,
                                           seat_position=self.position)
            try:
                from dataclasses import replace

                self.context = replace(self.context, game_config=self.game.get_game_config("red_alert"))
            except MCPToolError:
                pass
            self._advance(until_playing=True)
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
        while not self.done.is_set():
            job = self.jobs.get()
            if job is None:
                break
            argv, reply = job
            try:
                answer = self._command(argv)
            except Exception as exc:  # noqa: BLE001
                answer = f"error: {type(exc).__name__}: {exc}"
            if self.db is not None:
                try:
                    self.db.human_log(self.session_id, self._tick(), " ".join(argv), answer)
                except Exception:  # noqa: BLE001
                    pass
            reply.put(answer)

    def command(self, argv: list[str], timeout: float = 600.0) -> str:
        reply: queue.Queue = queue.Queue()
        self.jobs.put((argv, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return "error: no answer (is the game still running?)"

    # -- playing -----------------------------------------------------------------------------

    def _obs(self) -> dict:
        return (self.state.raw.get("observation") if self.state is not None else None) or {}

    def _tick(self) -> int | None:
        return self._obs().get("tick")

    def _seconds(self) -> float:
        return float((self.state.raw.get("time") or {}).get("game_seconds") or 0.0) if self.state else 0.0

    def _assistant(self):
        if self.assistant is None:
            from agent.agent import create_agent

            self.assistant = create_agent(knobs=self.knobs, record=False, controls=False)
        return self.assistant

    def _autopilot_orders(self) -> list[dict]:
        if self.autopilot == "off":
            return []
        move = self._assistant().choose_action(self.state, self.context)
        if not isinstance(move, WithReasoning):
            return []
        orders = move.action.get("orders") or []
        if self.autopilot == "macro":
            orders = [o for o in orders if o.get("cmd") in MACRO_CMDS]
        # Leave alone what the person decided: where their buildings go and where units gather.
        orders = [o for o in orders if not (o.get("cmd") == "place" and o.get("item") in self.placements)
                  and not (o.get("cmd") == "rally" and self.rally_point is not None)]
        return orders

    def _send(self) -> None:
        mine, self.queued = self.queued[:MAX_ORDERS], self.queued[MAX_ORDERS:]
        theirs = self._autopilot_orders()[:MAX_ORDERS - len(mine)]
        orders = mine + theirs
        if not orders:
            return
        note = f"human: {len(mine)} order(s)" if mine else "autopilot"
        entry = {"tick": self._tick(), "mine": mine, "autopilot": len(theirs)}
        try:
            result = self.game.play_action(action={"type": "orders", "orders": orders},
                                           state_version=self.state.state_version, reasoning_summary=note)
            entry["verdicts"] = [(r.get("cmd"), r.get("status"), r.get("reason")) for r in result.get("results") or []][:len(mine)]
        except MCPToolError as exc:
            result = {"error": exc.error_code, "detail": str(exc)}
            entry["error"] = f"{exc.error_code}: {exc}"
            if mine and exc.error_code in ("NOT_YOUR_TURN", "STALE_STATE"):
                self.queued = mine + self.queued  # try again next view
        self.history.append(entry)
        if theirs and self.assistant is not None:
            shift = len(mine)
            if isinstance(result, dict) and isinstance(result.get("results"), list):
                rows = [dict(r, index=r["index"] - shift) for r in result["results"]
                        if isinstance(r.get("index"), int) and r["index"] >= shift]
                result = {**result, "results": rows}
            self.assistant.on_action_result(result, self.context)

    def _advance(self, seconds: float = 0.0, *, until_playing: bool = False, alert: bool = True) -> str:
        """Play on for ``seconds`` of game time; returns why it stopped: time, alert or over."""
        target = self._seconds() + seconds
        state = self.state
        seen = None
        why = "time"
        while True:
            if state.is_terminal:
                why = "over"
                break
            playing = state.phase == "playing"
            if state.state_version != seen:
                seen = state.state_version
                self.state = state
                started_threat = self._observe()
                if alert and started_threat and not until_playing:
                    why = "alert"
                    break
            if playing and state.is_current_actor and (self.queued or self.autopilot != "off"):
                self.state = state
                self._send()
                state = self.game.get_state()
                if self.queued:
                    continue
            self.state = state
            if until_playing and playing:
                break
            if not until_playing and playing and self._seconds() >= target and not self.queued:
                break
            state = self.game.wait_for_update(since_version=state.state_version,
                                              since_is_current_actor=bool(state.is_current_actor),
                                              since_phase=state.phase, timeout_seconds=20.0)
        self.state = state
        return why

    def _observe(self) -> bool:
        """Each new view: remember enemy buildings, place and rally as asked, watch for threats.
        True when a threat has just begun (enemies at the base)."""
        obs = self._obs()
        tick = obs.get("tick") or 0
        units = [u for u in obs.get("units") or [] if not u.get("wreck")]
        if not any(not e.get("wreck") for e in obs.get("visible_enemies") or []) and \
                not obs.get("visible_enemy_buildings"):
            self.blind_ticks.add(tick)
        for b in obs.get("visible_enemy_buildings") or []:
            if not b.get("wreck"):
                self.enemy_buildings[b["id"]] = (b["type"], b["x"], b["y"], tick)
        visible = {b["id"] for b in obs.get("visible_enemy_buildings") or []}
        for bid, (_, x, y, _) in list(self.enemy_buildings.items()):
            if bid not in visible and any(math.hypot(u["x"] - x, u["y"] - y) <= 4 for u in units):
                del self.enemy_buildings[bid]  # (we look at its cell and it isn't there: gone)
        now = self._seconds()
        for row in obs.get("production") or []:
            item = row.get("item")
            if row.get("ready") and item in self.placements and now - self.placing.get(item, -99) > 3:
                x, y = self.placements[item]
                self.queued.insert(0, {"cmd": "place", "item": item, "at": [x, y]})
                self.placing[item] = now
        for item in list(self.placements):
            ready = any(r.get("item") == item for r in obs.get("production") or [])
            if not ready and item in self.placing and now - self.placing[item] > 3:
                del self.placements[item]  # (placed: the queue no longer has it)
                self.placing.pop(item, None)
        if self.rally_point is not None:
            for b in obs.get("buildings") or []:
                if b["type"] in PRODUCERS and b["id"] not in self.rallied:
                    self.rallied.add(b["id"])
                    self.queued.append({"cmd": "rally", "building": b["id"], "to": list(self.rally_point)})
        base = obs.get("base_center")
        near = []
        if base:
            near = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck") and e["type"] != "harv"
                    and math.hypot(e["x"] - base[0], e["y"] - base[1]) <= ALERT_RADIUS]
        # Contact: our units or buildings under fire after a quiet spell (CONTACT_QUIET_SECONDS without any).
        hit = [e for e in self.new_events if e.get("kind") in ("under_attack", "unit_lost", "building_lost")
               and (e.get("tick") or 0) > self.last_contact_tick]
        if hit:
            quiet = (hit[0]["tick"] - self.last_contact_tick) / 25 >= CONTACT_QUIET_SECONDS
            self.last_contact_tick = max(e["tick"] for e in hit)
            if quiet and not self.threat:
                e = hit[0]
                self.alert = (f"CONTACT {clock(now)}: our {e.get('type')} at {e.get('x')},{e.get('y')} is under fire"
                              + (f" ({len(hit)} hits)" if len(hit) > 1 else ""))
                self.contact = True
        was, self.threat = self.threat, len(near) >= ALERT_UNITS
        if self.threat and not was:
            cx = round(sum(e["x"] for e in near) / len(near))
            cy = round(sum(e["y"] for e in near) / len(near))
            from collections import Counter

            kinds = ", ".join(f"{k} x{v}" for k, v in Counter(e["type"] for e in near).most_common(5))
            self.alert = f"ALERT {clock(now)}: {len(near)} enemies near the base, around {cx},{cy} ({kinds})"
            return True
        if self.contact:
            self.contact = False
            return True
        return False

    # -- commands ------------------------------------------------------------------------------

    def _queue(self, order: dict) -> str:
        self.queued.append(order)
        return f"queued: {json.dumps(order)}  ({len(self.queued)} waiting; 'go' sends them)"

    def _status_line(self) -> str:
        obs = self._obs()
        mil = obs.get("military") or {}
        eco = obs.get("economy") or {}
        net = (mil.get("kills_cost") or 0) - (mil.get("deaths_cost") or 0)
        combat = sum(1 for u in obs.get("units") or [] if is_combat(u))
        t = self.state.raw.get("time") or {}
        return (f"[{clock(t.get('game_seconds'))}, left {clock(t.get('remaining_seconds'))}] cash {eco.get('cash')}+{eco.get('ore')} "
                f"power {eco.get('power_provided')}/{eco.get('power_drained')}  army {combat} (${mil.get('army_value')})  "
                f"kills ${mil.get('kills_cost')} losses ${mil.get('deaths_cost')} net {net:+d}  "
                f"enemies in sight {sum(1 for e in obs.get('visible_enemies') or [] if not e.get('wreck'))}")

    def _event_lines(self, events: list[dict], limit: int = 25) -> list[str]:
        skip = {"production_ready"}
        rows = [e for e in events if e.get("kind") not in skip]
        out = []
        for e in rows[-limit:]:
            where = f" @{e['x']},{e['y']}" if e.get("x") is not None else ""
            what = e.get("type") or (f"x{e['count']}" if e.get("count") else "")
            out.append(f"  {clock(e['tick'] / 25 if e.get('tick') else None)} {e['kind']} {what}{where}")
        if len(rows) > limit:
            out.insert(0, f"  ({len(rows) - limit} earlier events not shown)")
        return out

    def _start_tick(self) -> int:
        obs = self._obs()
        return int((obs.get("tick") or 0) - self._seconds() * 25)

    def _digest(self, events: list[dict]) -> list[str]:
        """What happened, in a few lines: production, sightings, fights, buildings lost, power."""
        from collections import Counter

        from .report import fights

        out = []
        made = Counter(e["type"] for e in events if e["kind"] == "unit_created")
        built = [e["type"] for e in events if e["kind"] == "building_created"]
        if made or built:
            parts = []
            if made:
                parts.append("made " + ", ".join(f"{k} x{v}" for k, v in made.most_common()))
            if built:
                parts.append("built " + ", ".join(built))
            out.append("  " + "; ".join(parts))
        spotted_b = [e for e in events if e["kind"] == "enemy_spotted" and e.get("type") in self._building_types()]
        spotted_u = Counter(e["type"] for e in events if e["kind"] == "enemy_spotted" and e.get("type") not in self._building_types())
        if spotted_b or spotted_u:
            parts = []
            if spotted_b:
                parts.append("buildings " + " ".join(f"{e['type']}@{e['x']},{e['y']}" for e in spotted_b[:12]))
            if spotted_u:
                parts.append("units " + ", ".join(f"{k} x{v}" for k, v in spotted_u.most_common(6)))
            out.append("  spotted: " + "; ".join(parts))
        start = self._start_tick()
        for t, text in fights([e for e in events if e["kind"] in ("unit_lost", "enemy_killed")], start):
            out.append(f"  {clock((t - start) / 25)} {text}")
        for e in events:
            if e["kind"] == "building_created" and e["type"] in self.asked_at:
                ax, ay = self.asked_at.pop(e["type"])
                if math.hypot(e["x"] - ax, e["y"] - ay) > 2:
                    out.append(f"  !! {e['type']} went to {e['x']},{e['y']}, not {ax},{ay}: buildings must stand close to "
                               "the base (the engine placed it near the construction yard)")
        unseen = [e for e in events if e["kind"] == "unit_lost" and e.get("tick") in self.blind_ticks]
        for e in unseen[:3]:
            near = sorted(((math.hypot(x - e["x"], y - e["y"]), t, x, y) for t, x, y, _ in self.enemy_buildings.values()
                           if t in DEFENSES), key=lambda r: r[0])[:2]
            out.append(f"  !! our {e['type']} died at {e['x']},{e['y']} to something out of sight"
                       + (": nearest known defenses " + ", ".join(f"{t}@{x},{y} ({d:.0f} cells)" for d, t, x, y in near)
                          if near else " (a defense out-ranging our sight? try 'history')"))
        for kind, (seen, text) in {"air": (AIR, "enemy aircraft: riflemen can't hit them (rocket infantry e3, flak "
                                                   "trucks, AA guns and SAMs can)"),
                                   "mines": (MINES, "mines: units die where nothing shows; go around, or clear them")}.items():
            hit = sorted({e["type"] for e in events if e["kind"] == "enemy_spotted" and e.get("type") in seen})
            if hit and kind not in self.warned:
                self.warned.add(kind)
                out.append(f"  !! {', '.join(hit)} seen: {text}")
        out += self._warnings()
        for e in events:
            if e["kind"] in ("building_lost", "building_sold"):
                out.append(f"  {clock((e['tick'] - start) / 25)} {e['kind']} {e['type']} @{e['x']},{e['y']}")
            elif e["kind"] in ("low_power", "power_restored"):
                out.append(f"  {clock((e['tick'] - start) / 25)} {e['kind']}")
        return out

    def _warnings(self) -> list[str]:
        """Things a player tends to miss: money piling up, idle factories, fragile units next to enemies."""
        obs = self._obs()
        out = []
        eco = obs.get("economy") or {}
        money = (eco.get("cash") or 0) + (eco.get("ore") or 0)
        if money >= 2000:
            out.append(f"  !! ${money} unspent: more factories or units")
        busy = {r.get("queue") for r in obs.get("production") or []}
        types = {b["type"] for b in obs.get("buildings") or []}
        idle = [q for q, need in (("Infantry", {"barr", "tent"}), ("Vehicle", {"weap"})) if types & need and q not in busy]
        if idle and money >= 600:
            out.append(f"  !! idle production: {', '.join(idle)}")
        enemies = [e for e in obs.get("visible_enemies") or [] if not e.get("wreck")]
        if len(enemies) >= 5:
            from collections import Counter

            cx = sum(e["x"] for e in enemies) / len(enemies)
            cy = sum(e["y"] for e in enemies) / len(enemies)
            near = [u for u in obs.get("units") or [] if is_combat(u) and math.hypot(u["x"] - cx, u["y"] - cy) <= 12]
            out.append(f"  odds: {len(enemies)} enemies in sight around {cx:.0f},{cy:.0f} ("
                       + ", ".join(f"{k} x{v}" for k, v in Counter(e["type"] for e in enemies).most_common(5))
                       + f") vs {len(near)} of our combat units within 12 cells")
        for u in obs.get("units") or []:
            if u["type"] in FRAGILE and not u.get("wreck"):
                close = [e for e in enemies if math.hypot(e["x"] - u["x"], e["y"] - u["y"]) <= 5]
                if close:
                    out.append(f"  !! {u['type']} #{u['id']} at {u['x']},{u['y']} has {len(close)} enemies within 5 cells")
        return out[:6]

    def _history(self) -> str:
        """Where this opponent put its buildings in earlier lab games on this map, from the same corner."""
        if self.db is None:
            return "no database"
        match = self.platform.matches.get(self.session_id)
        other = match.seats[1 - self.position] if match else None
        meta = (self.platform.match_meta.get(self.session_id) or {}).get("seats") or []
        opp = {"bot": meta[1 - self.position].get("label")} if len(meta) == 2 and meta[1 - self.position].get("label") else None
        legal = self.state.raw.get("legal_actions") or {}
        spawn = legal.get("enemy_base") or None
        map_id = (self.state.raw.get("map") or {}).get("id")
        if spawn is None or other is None:
            return "unknown opponent corner"
        bot = opp["bot"] if opp else other.config.name
        rows = self.db.q(
            "SELECT e.type, e.x, e.y, COUNT(DISTINCT e.game_id) AS n FROM events e JOIN games g ON g.id = e.game_id "
            "JOIN seats o ON o.game_id = g.id AND o.position != e.position WHERE o.bot = ? AND g.map = ? AND "
            "e.kind = 'enemy_spotted' AND e.game_id != ? GROUP BY e.type, e.x, e.y", bot, map_id, self.session_id)
        rows = [r for r in rows if r["type"] in self._building_types()
                and math.hypot(r["x"] - spawn[0], r["y"] - spawn[1]) <= 32]
        if not rows:
            return f"no earlier sightings of {bot}'s buildings on {map_id} near {spawn}"
        games = self.db.one("SELECT COUNT(DISTINCT g.id) AS n FROM games g JOIN seats o ON o.game_id = g.id "
                            "WHERE o.bot = ? AND g.map = ?", bot, map_id)["n"]
        defenses = sorted((r for r in rows if r["type"] in DEFENSES), key=lambda r: (-r["n"], r["x"], r["y"]))
        others = sorted((r for r in rows if r["type"] not in DEFENSES), key=lambda r: (-r["n"], r["x"], r["y"]))
        lines = [f"{bot} on {map_id} from its corner near {spawn[0]},{spawn[1]}: seen in {games} earlier lab games"]
        lines.append("defenses (type@x,y xgames): " + " ".join(f"{r['type']}@{r['x']},{r['y']}x{r['n']}" for r in defenses[:30]))
        lines.append("other buildings: " + " ".join(f"{r['type']}@{r['x']},{r['y']}x{r['n']}" for r in others[:20]))
        units = self.db.q(
            "SELECT e.type, COUNT(DISTINCT e.game_id) AS n FROM events e JOIN games g ON g.id = e.game_id "
            "JOIN seats o ON o.game_id = g.id AND o.position != e.position WHERE o.bot = ? AND g.map = ? AND "
            "e.kind = 'enemy_spotted' AND e.game_id != ? GROUP BY e.type ORDER BY n DESC", bot, map_id, self.session_id)
        units = [r for r in units if r["type"] not in self._building_types()]
        if units:
            notes = [f"{r['type']} ({UNIT_NOTES[r['type']]})" for r in units if r["type"] in UNIT_NOTES]
            lines.append("units seen (in N games): " + " ".join(f"{r['type']}x{r['n']}" for r in units[:25])
                         + ("\n  watch for: " + "; ".join(notes) if notes else ""))
        lines.append("ranges: tsla 7, sam 7.5, gun 6.5, ftur 5, pbox ~5; v2rl reaches 10, arty 12 (bombard X Y UNITS)")
        return "\n".join(lines)

    def _building_types(self) -> set[str]:
        if not hasattr(self, "_btypes"):
            from ffsim.rules import Rules

            self._btypes = set((Rules.load().config.get("rules") or {}).get("buildings") or {})
        return self._btypes

    def _intel(self) -> str:
        tick = self._tick() or 0
        start = self._start_tick()
        rows = sorted(self.enemy_buildings.items(), key=lambda kv: (kv[1][1], kv[1][2]))
        return "\n".join(f"  #{bid:<6} {t:6} @{x},{y:<4} seen {clock((seen - start) / 25)}"
                         + (" (in sight)" if seen == tick else "") for bid, (t, x, y, seen) in rows) or "  (none seen yet)"

    def _result_text(self) -> str:
        r = self.state.raw.get("result") or {}
        return f"GAME OVER: {r.get('outcome')} ({r.get('reason')})"

    def _command(self, argv: list[str]) -> str:
        if not argv or argv[0] in ("help", "-h", "--help"):
            return HELP
        cmd, args = argv[0], argv[1:]
        obs = self._obs()
        if self.state is None:
            return f"the game has not started yet ({self.error or 'waiting'})"
        if cmd == "state":
            if args[:1] == ["json"]:
                return json.dumps(self.state.raw, default=str)
            extra = [f"autopilot {self.autopilot}" + (f" knobs {self.knobs}" if self.knobs else ""),
                     f"queued orders: {len(self.queued)}"]
            text = summary(obs, time_info=self.state.raw.get("time"), seat=f"seat {self.position}", extra=extra)
            if self.state.is_terminal:
                text += "\n" + self._result_text()
            return text
        if cmd == "map":
            if len(args) >= 2:
                return minimap(obs, (self.state.raw.get("map") or {}).get("id"), center=(int(args[0]), int(args[1])),
                               radius=int(args[2]) if len(args) > 2 else 12)
            return minimap(obs, (self.state.raw.get("map") or {}).get("id"))
        if cmd == "units":
            return units_table(obs, args[0] if args else None)
        if cmd == "enemies":
            return enemies_table(obs) + "\nenemy buildings seen (remembered):\n" + self._intel()
        if cmd == "history":
            return self._history()
        if cmd == "group":
            if not args:
                alive = {u["id"] for u in obs.get("units") or [] if not u.get("wreck")}
                return "\n".join(f"  @{k}: {sum(1 for i in v if i in alive)} of {len(v)} alive"
                                 for k, v in self.groups.items()) or "  (no groups: group NAME UNITS)"
            self.groups[args[0]] = parse_units(obs, args[1:], self.groups)
            return f"@{args[0]}: {len(self.groups[args[0]])} units {self.groups[args[0]][:20]}"
        if cmd == "events":
            return "\n".join(self._event_lines(self.events, int(args[0]) if args else 40)) or "(none)"
        if cmd == "log":
            n = int(args[0]) if args else 10
            rows = [h for h in self.history if h.get("mine")][-n:]
            return "\n".join(f"  tick {h['tick']}: {json.dumps(h['mine'])} -> {h.get('verdicts') or h.get('error')}"
                             for h in rows) or "(no orders of yours sent yet)"
        if cmd == "go":
            if self.state.is_terminal:
                return self._result_text()
            seconds = min(float(args[0]) if args else 5.0, GO_MAX_SECONDS)
            self.new_events = []
            sent_before = len(self.history)
            t0 = self._seconds()
            why = self._advance(seconds, alert="nostop" not in args)
            lines = [self._status_line()]
            mine = [h for h in self.history[sent_before:] if h.get("mine")]
            for h in mine:
                lines.append(f"  sent {len(h['mine'])} order(s) at {clock((h['tick'] - self._start_tick()) / 25)}: "
                             f"{summarize_orders(h['mine'])}" + (f" -> {h['error']}" if h.get("error") else ""))
            problems = [p for p in self.state.raw.get("recent_order_problems") or []
                        if (p.get("batch"), p.get("index")) not in self.problems_seen]
            for p in problems:
                self.problems_seen.add((p.get("batch"), p.get("index")))
                lines.append(f"  !! order {p.get('cmd')} {p.get('status')}: {p.get('reason')} {p.get('detail') or ''}".rstrip())
            lines += self._digest(self.new_events)
            if why == "alert":
                lines.append(f"  {self.alert} -- stopped after {self._seconds() - t0:.1f} s (go N nostop runs through)")
            if self.state.is_terminal:
                lines.append(self._result_text())
            return "\n".join(lines)
        if cmd == "autopilot":
            if args and args[0] in ("off", "macro", "full"):
                self.autopilot = args[0]
            return f"autopilot {self.autopilot}"
        if cmd == "knobs":
            for pair in args:
                k, _, v = pair.partition("=")
                self.knobs[k] = int(v) if v.isdigit() else v
            if self.assistant is not None and self.assistant._bot is not None:
                from agent.agent import knobs_from_dict

                self.assistant._bot.set_knobs(knobs_from_dict(self.knobs))
            return f"knobs {self.knobs}"
        if cmd == "info":
            return item_info(args)
        if cmd == "queue":
            return "\n".join(json.dumps(o) for o in self.queued) or "(nothing queued)"
        if cmd == "clear":
            n, self.queued = len(self.queued), []
            return f"cleared {n} queued order(s)"
        if cmd == "note":
            if self.db is not None:
                self.db.add_note(" ".join(args), game_id=self.session_id, author="human")
            return "noted"
        if cmd == "resign":
            self.game.resign()
            self.state = self.game.get_state()
            return self._result_text()
        if cmd == "build":
            rest = [a for a in args[1:] if a != "at"]
            if len(rest) >= 2:
                self.placements[args[0]] = (int(rest[0]), int(rest[1]))
                self.asked_at[args[0]] = (int(rest[0]), int(rest[1]))
                self.placing.pop(args[0], None)
            return self._queue({"cmd": "build", "item": args[0]}) + (
                f"; placed at {rest[0]},{rest[1]} when ready" if len(rest) >= 2 else "")
        if cmd == "train":
            return self._queue({"cmd": "train", "item": args[0], "count": int(args[1]) if len(args) > 1 else 1})
        if cmd == "place":
            order = {"cmd": "place", "item": args[0]}
            if len(args) >= 3:
                order["at"] = [int(args[1]), int(args[2])]
            return self._queue(order)
        if cmd == "deploy":
            ids = [int(a) for a in args] or [u["id"] for u in obs.get("units") or [] if u["type"] == "mcv"][:1]
            return self._queue({"cmd": "deploy", "units": ids})
        if cmd in ("attack", "move"):
            then = "then" in args
            words = [a for a in args[2:] if a != "then"]
            ids = parse_units(obs, words, self.groups)
            if not ids:
                gone = [w for w in words if w.startswith("@") and self.groups.get(w[1:])]
                return "no units match" + (f" (everyone in {', '.join(gone)} is gone: 'all', a type, or a new group)"
                                           if gone else "")
            verb = "attack_move" if cmd == "attack" else "move"
            for i in range(0, len(ids), 50):
                order = {"cmd": verb, "units": ids[i:i + 50], "to": [int(args[0]), int(args[1])]}
                if then:
                    order["queued"] = True
                self._queue(order)
            return f"queued {verb} of {len(ids)} unit(s) to {args[0]},{args[1]}" + (" (after their current orders)" if then else "")
        if cmd == "target":
            ids = parse_units(obs, args[1:], self.groups)
            return self._queue({"cmd": "attack", "units": ids[:50], "target": int(args[0])})
        if cmd == "stance":
            if not args or args[0] not in STANCES:
                return f"stance {'|'.join(STANCES)} [UNITS]  (defend: hold ground, fight what comes in range)"
            ids = parse_units(obs, args[1:], self.groups)
            for i in range(0, len(ids), 50):
                self._queue({"cmd": "set_stance", "units": ids[i:i + 50], "stance": args[0]})
            return f"queued stance {args[0]} for {len(ids)} unit(s)"
        if cmd == "bombard":
            ids = parse_units(obs, args[2:], self.groups)
            if not ids:
                return "no units match"
            self._queue({"cmd": "attack", "units": ids[:50], "to": [int(args[0]), int(args[1])]})
            return f"queued: {len(ids)} unit(s) fire at the ground at {args[0]},{args[1]} (no need to see it)"
        if cmd == "stop":
            return self._queue({"cmd": "stop", "units": parse_units(obs, args, self.groups)[:50]})
        if cmd == "harvest":
            ids = [int(a) for a in args] or [u["id"] for u in obs.get("units") or [] if u["type"] == "harv"]
            return self._queue({"cmd": "harvest", "units": ids})
        if cmd == "sell":
            return self._queue({"cmd": "sell", "building": int(args[0])})
        if cmd == "repair":
            return self._queue({"cmd": "repair", "building": int(args[0]), "on": True})
        if cmd == "rally":
            if args[:1] == ["off"]:
                self.rally_point = None
                return "rally points: the autopilot's again"
            if args[:1] == ["all"]:
                self.rally_point = (int(args[1]), int(args[2]))
                self.rallied = set()
                self._observe()
                return f"every production building (and each new one) rallies to {args[1]},{args[2]}"
            return self._queue({"cmd": "rally", "building": int(args[0]), "to": [int(args[1]), int(args[2])]})
        if cmd == "raw":
            payload = json.loads(" ".join(args))
            for o in payload if isinstance(payload, list) else [payload]:
                self._queue(o)
            return f"queued {len(payload) if isinstance(payload, list) else 1} order(s)"
        return f"unknown command {cmd!r}"


# -- the session server (one human game, in a background process) --------------------------------


def serve(args) -> int:
    """``lab human serve``: play one game with a human seat, commands over local HTTP."""
    from .arena import Arena, GameJob
    from . import bots as registry

    me = registry.get(f"human:{args.name}")
    table = registry.load()
    table[me.name] = me
    names = [args.vs, me.name] if args.seat == 1 else [me.name, args.vs]
    branch = {"of": args.from_game, "tick": args.at_tick, "label": "human"} if args.from_game else None
    job = GameJob(bots=names, map=args.map, minutes=args.minutes, seed=args.seed, label="human", branch=branch,
                  run_id=args.run_id)
    arena = Arena(record=False)
    seat_box: dict = {}

    def factory(platform, sid, pos, bot):
        seat = HumanSeat(platform, sid, pos, bot, autopilot=args.autopilot, db=arena.db)
        seat_box["seat"] = seat
        return seat

    state = {"game": None, "result": None}

    def play():
        try:
            state["result"] = arena.play(job, human_factory=factory, bots=table,
                                         on_created=lambda sid, m: state.__setitem__("game", sid))
        except Exception as exc:  # noqa: BLE001
            state["result"] = {"error": repr(exc)}
        finally:
            arena.db.end_run(args.run_id) if args.run_id else None

    threading.Thread(target=play, name="human-game", daemon=True).start()
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            argv = body.get("argv") or []
            if argv[:1] == ["quit"]:
                text = "bye"
                stop.set()
            elif "seat" not in seat_box:
                text = f"starting... {state['result'] or ''}"
            elif state["result"] is not None and argv[:1] not in (["state"], ["map"], ["events"], ["log"], ["units"],
                                                                 ["enemies"], ["note"], ["info"], ["help"]):
                text = f"the game is over: {json.dumps({k: state['result'].get(k) for k in ('id', 'termination', 'winner', 'outcome')})}\n" \
                       f"('lab show {state['game']}' for the record, 'lab h quit' to close this session)"
            elif state["result"] is not None:
                text = seat_box["seat"]._command(argv)  # (the game's thread has ended: looking and notes only)
            else:
                text = seat_box["seat"].command(argv)
            raw = json.dumps({"text": text, "game": state["game"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    import os

    SESSION_FILE.write_text(json.dumps({"port": server.server_address[1], "pid": os.getpid(), "started": time.time(),
                                        "vs": args.vs, "seat": args.seat, "map": args.map}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[lab] human session on port {server.server_address[1]}", flush=True)
    idle_limit = 3600.0
    last = time.monotonic()
    while not stop.is_set():
        stop.wait(1.0)
        if time.monotonic() - last > idle_limit and state["result"] is not None:
            break
    seat = seat_box.get("seat")
    if seat is not None and state["result"] is None:
        try:
            seat.game.resign()
        except Exception:  # noqa: BLE001
            pass
        for _ in range(100):
            if state["result"] is not None:
                break
            time.sleep(0.1)
    if seat is not None:
        seat.join(2)
    arena.close()
    server.shutdown()
    try:
        SESSION_FILE.unlink()
    except OSError:
        pass
    return 0


def client(argv: list[str], timeout: float = 900.0) -> str:
    import urllib.request

    try:
        info = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return "no human session: start one with 'lab human start --vs BOT'"
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/", data=json.dumps({"argv": argv}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["text"]
    except OSError as exc:
        return f"the session does not answer ({exc}); is it running? ('lab human status')"
