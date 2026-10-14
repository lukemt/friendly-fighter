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
play:   go [SECONDS]  (runs the game; default 5, at most 120; the game waits between commands)
orders: build ITEM | train ITEM [N] | place ITEM [X Y] | deploy [ID] | harvest [IDS]
        attack X Y [UNITS] (attack-move) | move X Y UNITS | target ENEMY_ID [UNITS] | stop UNITS
        sell ID | repair ID | rally BUILDING_ID X Y | raw '<order JSON or list>' | clear
units:  all (combat units, the default) | idle | a type (3tnk) | near:X,Y,R | ids (12 13 or 12,13)
help:   autopilot off|macro|full (macro: our bot runs economy and production, the army is yours)
        knobs posture=defend attack_threshold=12 ... (the autopilot's)
other:  info ITEM... (cost, time, power, needs, weapon) | note TEXT | resign | quit (closes the session)"""
MACRO_CMDS = {"deploy", "build", "place", "train", "harvest", "repair", "power_toggle", "rally", "cancel", "sell"}
MAX_ORDERS = 20
GO_MAX_SECONDS = 120


def parse_units(obs: dict, words: list[str]) -> list[int]:
    units = [u for u in obs.get("units") or [] if not u.get("wreck")]
    combat = [u for u in units if is_combat(u)]
    if not words or words == ["all"]:
        return [u["id"] for u in combat]
    out: list[int] = []
    for w in words:
        if w == "idle":
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

    def _advance(self, seconds: float = 0.0, *, until_playing: bool = False) -> None:
        target = self._seconds() + seconds
        state = self.state
        while True:
            if state.is_terminal:
                break
            playing = state.phase == "playing"
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
            return enemies_table(obs)
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
            self._advance(seconds)
            lines = [self._status_line()]
            mine = [h for h in self.history[sent_before:] if h.get("mine")]
            for h in mine:
                lines.append(f"  sent {len(h['mine'])} order(s) at tick {h['tick']}: {summarize_orders(h['mine'])}"
                             + (f" -> {h['error']}" if h.get("error") else ""))
            problems = [p for p in self.state.raw.get("recent_order_problems") or []
                        if (p.get("batch"), p.get("index")) not in self.problems_seen]
            for p in problems:
                self.problems_seen.add((p.get("batch"), p.get("index")))
                lines.append(f"  !! order {p.get('cmd')} {p.get('status')}: {p.get('reason')} {p.get('detail') or ''}".rstrip())
            lines += self._event_lines(self.new_events, 20)
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
            return self._queue({"cmd": "build", "item": args[0]})
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
            ids = parse_units(obs, args[2:])
            if not ids:
                return "no units match"
            verb = "attack_move" if cmd == "attack" else "move"
            for i in range(0, len(ids), 50):
                self._queue({"cmd": verb, "units": ids[i:i + 50], "to": [int(args[0]), int(args[1])]})
            return f"queued {verb} of {len(ids)} unit(s) to {args[0]},{args[1]}"
        if cmd == "target":
            ids = parse_units(obs, args[1:])
            return self._queue({"cmd": "attack", "units": ids[:50], "target": int(args[0])})
        if cmd == "stop":
            return self._queue({"cmd": "stop", "units": parse_units(obs, args)[:50]})
        if cmd == "harvest":
            ids = [int(a) for a in args] or [u["id"] for u in obs.get("units") or [] if u["type"] == "harv"]
            return self._queue({"cmd": "harvest", "units": ids})
        if cmd == "sell":
            return self._queue({"cmd": "sell", "building": int(args[0])})
        if cmd == "repair":
            return self._queue({"cmd": "repair", "building": int(args[0]), "on": True})
        if cmd == "rally":
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
