"""Claude as a bot's advisor: the bot's model requests become questions at a desk, the game waits.

    lab advise start --bot ext-ryan-claude --vs ai-normal [--map trapped] [--seat 0] [--seed N]
    lab adv wait [SECONDS]    # until a question comes (or the game ends): prints it
    lab adv brief [LAYER]     # the layer's system prompt (the bot's playbook and instructions): read once
    lab adv answer '{...}'    # the answer, checked against the question's JSON schema
    lab adv skip | detach     # no answer this time (the bot keeps its last instructions) | none from now on
    lab adv show | look | log [N] | note TEXT | status | quit

A bot asks through its model client: for a contestant's bot (``bots/external.py``) the ``anthropic``
stub sends each request here, with the bot's own system prompt, payload and answer schema, so the
bot plays exactly as with its model, Claude in the model's place. The request holds the seat's
decision, and the seat's think time is ``paused`` (``ffsim.vclock``), so no game time passes while
Claude thinks: the answer counts in the same decision, an advisor without latency (an upper bound).
Every question and answer goes to ``sim/lab/advise/<game>.jsonl``; the game is a normal lab game.

``Desk`` is generic (``ask(system, payload, schema, seat)``): any bot that can put a question as a
JSON schema can be advised this way.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import FAMILY, LAB_DIR

ADVISE_DIR = LAB_DIR / "advise"
SESSION_FILE = ADVISE_DIR / (FAMILY or "main") / "session.json"  # (one per session: AGENTS.md)
# The layer a question comes from, by a field only that layer's schema has (contestants' advisors).
LAYERS = (("posture", "strategy"), ("main_action", "tactical"), ("focus_target_id", "fast"))

_DESK: "Desk | None" = None


def current_desk() -> "Desk | None":
    return _DESK


class Skipped(Exception):
    """No answer to this question (skipped, detached, or the game is over)."""


@dataclass
class Question:
    id: int
    layer: str
    seat_name: str
    position: int | None
    game_seconds: float
    system: str
    payload: object
    schema: dict
    model: str
    new_brief: bool
    asked: float = field(default_factory=time.time)
    answer: dict | None = None
    skipped: str = ""


def layer_of(schema: dict) -> str:
    props = schema.get("properties") or {}
    return next((label for key, label in LAYERS if key in props), "question")


def validate(value, schema: dict, path: str = "answer") -> list[str]:
    """What is wrong with ``value`` for this JSON schema (the subset structured outputs use)."""
    errors: list[str] = []
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if kind == "object":
        if not isinstance(value, dict):
            return errors + [f"{path}: expected an object"]
        props = schema.get("properties") or {}
        errors += [f"{path}: missing {k!r}" for k in schema.get("required") or [] if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unknown field {k!r}" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                errors += validate(value[k], sub, f"{path}.{k}")
    elif kind == "array":
        if not isinstance(value, list):
            return errors + [f"{path}: expected a list"]
        for i, item in enumerate(value):
            errors += validate(item, schema.get("items") or {}, f"{path}[{i}]")
    elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        errors.append(f"{path}: expected an integer")
    elif kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        errors.append(f"{path}: expected a number")
    elif kind == "string" and not isinstance(value, str):
        errors.append(f"{path}: expected a string")
    elif kind == "boolean" and not isinstance(value, bool):
        errors.append(f"{path}: expected true or false")
    return errors


def skeleton(schema: dict):
    """An answer of the right shape to fill in."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {k: skeleton(v) for k, v in (schema.get("properties") or {}).items()}
    return {"array": [], "integer": 0, "number": 0, "string": "", "boolean": False}.get(kind)


def fields_text(schema: dict) -> str:
    out = []
    for k, sub in (schema.get("properties") or {}).items():
        kind = "|".join(map(str, sub["enum"])) if "enum" in sub else sub.get("type", "?")
        if sub.get("type") == "array" and (sub.get("items") or {}).get("type") == "object":
            kind = "[" + ", ".join(f"{ik}:{iv.get('type')}" for ik, iv in sub["items"].get("properties", {}).items()) + "]"
        out.append(f"{k}: {kind}")
    return "  ".join(out)


def pretty(obj, indent: int = 0, width: int = 110) -> str:
    """JSON with small objects and lists on one line."""
    flat = json.dumps(obj, separators=(", ", ": "), ensure_ascii=False)
    if len(flat) + indent <= width or not isinstance(obj, (dict, list)) or not obj:
        return flat
    pad = " " * (indent + 1)
    if isinstance(obj, dict):
        items = [f"{pad}{json.dumps(k)}: {pretty(v, indent + 1 + len(json.dumps(k)) + 2, width)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    return "[\n" + ",\n".join(pad + pretty(v, indent + 1, width) for v in obj) + "\n" + " " * indent + "]"


def clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


class Desk:
    """Questions from advised seats, answered one at a time; each asking seat waits for its answer."""

    def __init__(self, folder: Path = ADVISE_DIR) -> None:
        self.folder = folder
        self.cond = threading.Condition()
        self.queue: list[Question] = []
        self.seats: list = []
        self.briefs: dict[str, str] = {}
        self.records: list[dict] = []
        self.ids = itertools.count(1)
        self.detached = False
        self.closed = False
        self.result: str | None = None
        self.game_id: str | None = None
        self.map_id = ""

    # -- the bot's side --

    def attach(self, seat) -> None:
        with self.cond:
            if seat not in self.seats:
                self.seats.append(seat)

    def ask(self, *, system: str, payload, schema: dict, seat=None, model: str = "") -> dict:
        layer = layer_of(schema)
        with self.cond:
            if self.detached or self.closed:
                raise Skipped("detached" if self.detached else "the session is closed")
            q = Question(id=next(self.ids), layer=layer, seat_name=getattr(seat, "name", ""),
                         position=getattr(seat, "position", None),
                         game_seconds=float(getattr(getattr(seat, "clock", None), "seconds", 0.0)),
                         system=system, payload=payload, schema=schema, model=model,
                         new_brief=self.briefs.get(layer) != system)
            self.briefs[layer] = system
            self.queue.append(q)
            self.cond.notify_all()
            while q.answer is None and not q.skipped:
                if self.closed:
                    q.skipped = "the session is closed"
                    break
                self.cond.wait(1.0)
            self.queue.remove(q)
            self.cond.notify_all()
        self._record(q)
        if q.skipped:
            raise Skipped(q.skipped)
        return q.answer

    # -- Claude's side --

    def _open(self) -> list[Question]:
        """Questions still waiting for an answer (an answered one stays queued until its seat takes it)."""
        return [q for q in self.queue if q.answer is None and not q.skipped]

    def pending(self) -> Question | None:
        with self.cond:
            open_ = self._open()
            return open_[0] if open_ else None

    def wait(self, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        with self.cond:
            while not self._open() and self.result is None and time.monotonic() < deadline:
                self.cond.wait(min(1.0, max(0.0, deadline - time.monotonic())))
            open_ = self._open()
            q = open_[0] if open_ else None
        if q is not None:
            return self.describe(q)
        if self.result is not None:
            return f"the game is over: {self.result}"
        return f"no question in {seconds:.0f} s (game {self._game_clock()}); 'lab adv look' to see the board"

    def describe(self, q: Question) -> str:
        head = (f"question {q.id} · {q.layer} · {q.seat_name} seat {q.position} · game {clock(q.game_seconds)}"
                + (f" · its model: {q.model}" if q.model else ""))
        lines = [head]
        if q.new_brief:
            lines.append(f"(new instructions for {q.layer}: 'lab adv brief {q.layer}', {len(q.system):,} chars)")
        lines += ["", pretty(q.payload), "", f"fields: {fields_text(q.schema)}",
                  f"answer: lab adv answer '{json.dumps(skeleton(q.schema))}'   (or: lab adv skip)"]
        return "\n".join(lines)

    def answer(self, text: str) -> str:
        with self.cond:
            open_ = self._open()
            if not open_:
                return "no question is waiting ('lab adv wait')"
            q = open_[0]
            try:
                value = json.loads(text)
            except ValueError as exc:
                return f"not JSON: {exc}"
            errors = validate(value, q.schema)
            if errors:
                return "not accepted:\n  " + "\n  ".join(errors)
            q.answer = value
            self.cond.notify_all()
        return f"answered question {q.id} ({q.layer}); the game plays on"

    def skip(self, why: str = "skipped") -> str:
        with self.cond:
            open_ = self._open()
            if not open_:
                return "no question is waiting"
            q = open_[0]
            q.skipped = why
            self.cond.notify_all()
        return f"skipped question {q.id} ({q.layer}): the bot keeps its last instructions"

    def detach(self) -> str:
        with self.cond:
            self.detached = True
            for q in self.queue:
                q.skipped = "detached"
            self.cond.notify_all()
        return "detached: no more questions, the bot plays on its own rules"

    def brief(self, layer: str = "") -> str:
        if not self.briefs:
            return "no question yet"
        if not layer:
            return "\n".join(f"{k}: {len(v):,} chars ('lab adv brief {k}')" for k, v in self.briefs.items())
        return self.briefs.get(layer) or f"no brief for {layer!r}; known: {', '.join(self.briefs)}"

    def look(self) -> str:
        from .views import minimap, summary

        out = []
        for seat in self.seats:
            raw = seat.last_raw or {}
            obs = raw.get("observation") if isinstance(raw, dict) else None
            if not isinstance(obs, dict):
                continue
            out.append(summary(obs, time_info=raw.get("time"), seat=f"{seat.name} seat {seat.position}"))
            if self.map_id:
                try:
                    out.append(minimap(obs, self.map_id))
                except Exception as exc:  # noqa: BLE001  (a map we can't draw: the summary is enough)
                    out.append(f"(no minimap: {exc})")
        return "\n\n".join(out) or "no view yet"

    def log(self, n: int = 40) -> str:
        out = []
        for seat in self.seats:
            out.append(f"-- {seat.name} seat {seat.position}")
            out += list(seat.lines)[-n:]
        return "\n".join(out) or "no log yet"

    def status(self) -> str:
        answered = sum(1 for r in self.records if r.get("answer") is not None)
        q = self.pending()
        return (f"game {self.game_id} on {self.map_id}, {self._game_clock()}; {answered} answered, "
                f"{len(self.records) - answered} skipped; " + (f"waiting: question {q.id} ({q.layer})" if q else "no question waiting")
                + ("; detached" if self.detached else "") + (f"; over: {self.result}" if self.result else ""))

    # -- the session's side --

    def finish(self, result: str) -> None:
        with self.cond:
            self.result = result
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            for q in self.queue:
                q.skipped = "the session is closed"
            self.cond.notify_all()

    def transcript(self) -> Path:
        return self.folder / f"{self.game_id or 'game'}.jsonl"

    def _game_clock(self) -> str:
        seconds = max((s.clock.seconds for s in self.seats), default=0.0)
        return clock(seconds)

    def _record(self, q: Question) -> None:
        row = {"id": q.id, "layer": q.layer, "seat": q.position, "bot": q.seat_name, "game_seconds": q.game_seconds,
               "asked": q.asked, "answered": time.time(), "model": q.model, "payload": q.payload,
               "answer": q.answer, "skipped": q.skipped or None}
        self.records.append(row)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.transcript(), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass


# -- the session: one game, the desk over local HTTP ---------------------------------------------


def serve(args) -> int:
    """``lab advise serve``: play one game with an advised bot; Claude answers over local HTTP."""
    global _DESK
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from . import bots as registry
    from .arena import Arena, GameJob

    table = registry.load()
    names = [args.vs, args.bot] if args.seat == 1 else [args.bot, args.vs]
    job = GameJob(bots=names, map=args.map, minutes=args.minutes, seed=args.seed, label="advise", run_id=args.run_id)
    desk = Desk()
    desk.map_id = args.map
    _DESK = desk
    arena = Arena(record=False)
    state: dict = {"game": None, "result": None}

    def play():
        try:
            game = arena.play(job, bots=table, on_created=lambda sid, m: setattr(desk, "game_id", sid))
            state["result"] = game
            seats = ", ".join(f"{s['bot']} {s['result']} (net {s['net']})" for s in game.get("seats", []))
            answered = sum(1 for r in desk.records if r.get("answer") is not None)
            arena.db.add_note(f"advised game: {answered} answers, {len(desk.records) - answered} skipped; "
                              f"transcript {desk.transcript()}", game_id=game["id"], run_id=args.run_id)
            desk.finish(f"{game['termination']}: {seats} ('lab show {game['id']}')")
        except Exception as exc:  # noqa: BLE001
            state["result"] = {"error": repr(exc)}
            desk.finish(f"failed: {exc!r}")
        finally:
            if args.run_id:
                arena.db.end_run(args.run_id)

    threading.Thread(target=play, name="advise-game", daemon=True).start()
    stop = threading.Event()

    def command(argv: list[str]) -> str:
        word, rest = (argv[0] if argv else "status"), argv[1:]
        if word == "wait":
            return desk.wait(float(rest[0]) if rest else 600.0)
        if word == "show":
            q = desk.pending()
            return desk.describe(q) if q else "no question is waiting"
        if word == "answer":
            return desk.answer(" ".join(rest))
        if word == "skip":
            return desk.skip()
        if word == "detach":
            return desk.detach()
        if word == "brief":
            return desk.brief(rest[0] if rest else "")
        if word == "look":
            return desk.look()
        if word == "log":
            return desk.log(int(rest[0]) if rest else 40)
        if word == "note":
            if not desk.game_id:
                return "no game yet"
            arena.db.add_note(" ".join(rest), game_id=desk.game_id, run_id=args.run_id)
            return "noted"
        if word == "status":
            return desk.status()
        if word == "quit":
            stop.set()
            return "bye"
        return f"unknown command {word!r}: wait, show, brief, answer, skip, detach, look, log, note, status, quit"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            try:
                text = command(body.get("argv") or [])
            except Exception as exc:  # noqa: BLE001
                text = f"error: {exc!r}"
            raw = json.dumps({"text": text, "game": desk.game_id}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"port": server.server_address[1], "pid": os.getpid(), "started": time.time(),
                                        "bot": args.bot, "vs": args.vs, "seat": args.seat, "map": args.map}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[lab] advise session on port {server.server_address[1]}", flush=True)
    over_since = None
    while not stop.wait(1.0):  # (until 'quit', or an hour after the game ended)
        if state["result"] is not None:
            over_since = over_since or time.monotonic()
            if time.monotonic() - over_since > 3600:
                break
    desk.detach()  # (a game still running is played to its end by the bot's own rules: a complete record)
    deadline = time.monotonic() + 300
    while state["result"] is None and time.monotonic() < deadline:
        time.sleep(0.2)
    desk.close()
    arena.close()
    server.shutdown()
    SESSION_FILE.unlink(missing_ok=True)
    return 0


def client(argv: list[str], timeout: float = 3600.0) -> str:
    import urllib.request

    try:
        info = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return "no advise session: start one with 'lab advise start --bot BOT --vs BOT'"
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/", data=json.dumps({"argv": argv}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["text"]
    except OSError as exc:
        return f"the session does not answer ({exc}); is it running? ('lab advise status')"
