"""Play lab games: one at a time per process, in virtual time, everything written to the database.

``Arena.play(GameJob)`` creates the match on an in-process ``ffsim.server.Platform``, seats each bot
(``lab.driver.BotSeat`` for agents, nothing for an OpenRA AI, which plays inside the engine,
``lab.human.HumanSeat`` for a person), watches it (``lab.capture.Capture``), waits for the end and
saves the game with its engine trace. A ``GameJob`` with ``branch`` starts from a moment of an
earlier game instead of from the start (its trace replayed up to that tick).
"""

from __future__ import annotations

import gzip
import json
import random
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ffsim.server import Platform

from . import LAB_DIR
from . import bots as registry
from .capture import Capture
from .db import LabDB
from .driver import BotSeat

TRACES_DIR = LAB_DIR / "traces"
GAME_TIMEOUT_SECONDS = 1800.0  # wall time; a 10-minute game takes ~15-40 s


class _Horizon:
    """Ends the game on the score at a set game time (a branch measured over a set time)."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def on_view(self, match, seat, view) -> None:
        if match.phase == "playing" and match.game_seconds() >= self.seconds:
            match._tiebreak("horizon")


@dataclass
class GameJob:
    bots: list[str]
    map: str = "trapped"
    minutes: int = 10
    seed: int | None = None
    label: str = ""
    branch: dict | None = None  # {"of": game id, "tick": T, "label": "..."}
    run_id: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Arena:
    def __init__(self, *, db: LabDB | None = None, engine_factory=None, persist: bool = True,
                 record: bool = False, prefetch: bool = True, keep_traces: bool = True) -> None:
        """``engine_factory``: ``(match, kinds, lobby, options) -> engine`` (tests use the fake engine);
        by default an OpenRA engine on a lane of its own (``ffsim.engine_openra.HostPool``)."""
        self.db = db or LabDB()
        self.record = record
        self.keep_traces = keep_traces
        self.persist = persist
        self._custom_factory = engine_factory
        self.pool = None
        self.options: dict[str, dict] = {}  # session id -> engine options (trace, snapshot)
        self.engines: dict[str, object] = {}
        self.platform = Platform(self._make_engine, rtt_ms="platform", time_mode="virtual",
                                 think=registry.DEFAULT_THINK, prefetch=prefetch)
        self.lock = threading.Lock()

    # -- engines ------------------------------------------------------------------------------

    def _make_engine(self, match, kinds, lobby):
        opts = self.options.get(match.session_id, {})
        if self._custom_factory is not None:
            engine = self._custom_factory(match, kinds, lobby, opts)
        else:
            from ffsim.engine_openra import HostPool, OpenRAEngine

            with self.lock:
                if self.pool is None:
                    self.pool = HostPool(LAB_DIR / "engines", 1, persist=self.persist)
            host = self.pool.take()
            seed = opts.get("seed") or match.rng.randrange(1, 2 ** 31)
            engine = OpenRAEngine(match.session_id, match.map_id, kinds, lobby=lobby, lockstep=True, host=host,
                                  seed=seed, trace_path=opts.get("trace"), prefix=opts.get("prefix"))
        self.engines[match.session_id] = engine
        return engine

    def close(self) -> None:
        if self.pool is not None:
            self.pool.stop()
            self.pool = None

    # -- one game -------------------------------------------------------------------------------

    def create(self, job: GameJob, bots: dict | None = None) -> tuple[str, list]:
        """Create the match (not yet started: it starts once every agent seat has checked in)."""
        table = registry.load() if bots is None else bots
        seat_bots = [registry.get(n, table) for n in job.bots]
        if len(seat_bots) != 2:
            raise ValueError("a game has two seats")
        if not any(b.is_agent for b in seat_bots):
            raise ValueError("at least one seat must be an agent (the engine steps for agents only)")
        session_id = "lab-" + str(uuid.uuid4())
        seed = job.seed if job.seed is not None else random.randrange(1, 2 ** 31)
        opts: dict = {}
        if self.keep_traces and self._custom_factory is None:
            TRACES_DIR.mkdir(parents=True, exist_ok=True)
            opts["trace"] = TRACES_DIR / f"{session_id}.trace"
        start_tick = None
        if job.branch:
            from ffsim.trace import prefix_until, read_trace

            origin = self.db.find_game(job.branch["of"])
            if origin is None or not origin["trace"]:
                raise ValueError(f"no game {job.branch['of']!r} with a trace to branch from")
            head, steps = read_trace(origin["trace"])
            kinds = [f"Multi{i}:{'rl-agent' if b.is_agent else b.seat()['kind'][3:]}" for i, b in enumerate(seat_bots)]
            if ",".join(kinds) != head["bots"]:
                raise ValueError(f"a branch keeps the players of {origin['id']} ({head['bots']}): an agent seat "
                                 f"stays an agent and an OpenRA AI the same AI (asked for {','.join(kinds)})")
            opts["prefix"] = prefix_until(steps, int(job.branch["tick"]))
            opts["seed"] = head["seed"]
            start_tick = (json.loads(origin["info"] or "{}")).get("start_tick")
            job.map = origin["map"]
            job.branch["of_id"] = origin["id"]
        self.options[session_id] = opts
        self.platform.create_match(map_id=job.map, max_minutes=job.minutes, seats=[b.seat() for b in seat_bots],
                                   label=job.label, seed=seed, session_id=session_id)
        match = self.platform.matches[session_id]
        if job.branch:
            match.start_tick = start_tick
            match.last_accepted_at = int(job.branch["tick"]) / 25.0
        job.seed = seed
        return session_id, seat_bots

    def play(self, job: GameJob, *, human_factory=None, bots: dict | None = None, on_created=None) -> dict:
        """Play one game to its end and save it; returns its ``games`` row (as a dict)."""
        created = time.time()
        table = registry.load() if bots is None else bots
        agents = {}
        for pos, name in enumerate(job.bots):  # (every bot made before the match: one that can't start fails here)
            bot = registry.get(name, table)
            if bot.kind == "human" and human_factory is None:
                raise ValueError("a human seat needs 'lab human'")
            if bot.is_agent and bot.kind != "human":
                agents[pos] = bot.make_agent(record=self.record)
        session_id, seat_bots = self.create(job, table)
        match = self.platform.matches[session_id]
        capture = Capture(session_id).attach(match)
        if (job.extra or {}).get("stop_at_seconds") is not None:
            match.watchers.append(_Horizon(float(job.extra["stop_at_seconds"])))
        seats = []
        for pos, bot in enumerate(seat_bots):
            if bot.kind == "human":
                seats.append(human_factory(self.platform, session_id, pos, bot))
            elif bot.is_agent:
                seats.append(BotSeat(self.platform, session_id, pos, agents[pos], name=bot.name))
        if on_created is not None:
            on_created(session_id, match)
        wall0 = time.monotonic()
        for s in seats:
            s.start()
        timeout = None if any(b.kind == "human" for b in seat_bots) else GAME_TIMEOUT_SECONDS
        while match.phase != "finished":
            time.sleep(0.05)
            if timeout is not None and time.monotonic() - wall0 > timeout:
                match.fail("lab_timeout", f"no end after {timeout:.0f} s of wall time")
            if all(getattr(s, "error", None) for s in seats) and seats and match.phase != "finished":
                match.fail("agent_error", "every agent seat failed")
        wall = time.monotonic() - wall0
        for s in seats:
            s.join(10)
        return self._save(job, session_id, match, seat_bots, seats, capture, created, wall)

    # -- the record --------------------------------------------------------------------------

    def _save(self, job, session_id, match, seat_bots, seats, capture, created, wall) -> dict:
        engine = self.engines.pop(session_id, None)
        trace = self._close_trace(engine)
        errors = {s.position: s.error for s in seats if getattr(s, "error", None)}
        scores = dict(getattr(match, "scores", None) or {})
        if (match.termination_reason in ("time_limit", "idle_timeout", "horizon") and not all(b.is_agent for b in seat_bots)
                and len({tuple(match.tiebreak_score(s))[0] for s in match.seats}) == 1 and scores):
            # Level on kills against an OpenRA AI: the platform would compare assets, but the AI's are
            # unknown here (it has no view), so the agent would always win: a draw is the honest record.
            scores = {p: 0.5 for p in scores}
            job.extra = {**(job.extra or {}), "tie": "level on kills; an OpenRA AI's assets are unknown: a draw"}
        winner = next((p for p, v in scores.items() if v == 1.0), None)
        outcome = ("no_result" if (match.result or {}).get("outcome") == "no_result"
                   else "draw" if scores and winner is None else "decisive")
        info = {"start_tick": match.start_tick, "clock": match.vclock.report() if match.vclock else None,
                "errors": errors or None, "label": job.label, **(job.extra or {})}
        if engine is not None and getattr(engine, "prefix", None):
            info["snapshot_check"] = "ok" if engine.prefix_diff is None else f"differs from step {engine.prefix_diff}"
        if match.failure_reason:
            info["failure"] = match.log[-3:]
        game = {"id": session_id, "run_id": job.run_id, "created": created, "finished": time.time(), "map": match.map_id,
                "minutes": job.minutes, "seed": job.seed, "time_mode": "virtual",
                "game_seconds": round(match.game_seconds(), 2), "wall_seconds": round(wall, 2),
                "termination": match.termination_reason, "winner": winner, "outcome": outcome,
                "branch_of": (job.branch or {}).get("of_id"), "branch_tick": (job.branch or {}).get("tick"),
                "branch_label": (job.branch or {}).get("label"), "trace": str(trace) if trace else None,
                "engine": registry._engine_hash() if self._custom_factory is None else "fake",
                "info": json.dumps(info, default=str)}
        seat_rows = []
        for pos, bot in enumerate(seat_bots):
            other = 1 - pos
            mil = capture.final_military(pos, other)
            score = scores.get(pos)
            result = "none" if score is None else "win" if score == 1.0 else "loss" if score == 0.0 else "draw"
            batches, orders, rejected = capture.counts(pos)
            version = bot.version()
            self.db.note_version(version, bot.name, bot.kind, bot.spec(), bot.code_hash())
            kills, deaths = mil.get("kills_cost"), mil.get("deaths_cost")
            seat_rows.append({
                "game_id": session_id, "position": pos, "bot": bot.name, "version": version, "kind": bot.kind,
                "faction": match.seats[pos].faction or (capture.last_obs.get(other) or {}).get("enemy_faction"),
                "result": result, "points": score,
                "kills_cost": kills, "deaths_cost": deaths, "assets_value": mil.get("assets_value"),
                "net": (kills - deaths) if kills is not None and deaths is not None else None,
                "army_value": mil.get("army_value"), "units_killed": mil.get("units_killed"),
                "units_lost": mil.get("units_lost"), "buildings_killed": mil.get("buildings_killed"),
                "buildings_lost": mil.get("buildings_lost"), "batches": batches, "orders": orders,
                "rejected": rejected, "think": str(bot.seat().get("think")) if bot.is_agent else None})
        self.db.save_game(game, seat_rows, capture.timeline, capture.events, capture.decisions)
        self.platform.matches.pop(session_id, None)
        self.options.pop(session_id, None)
        game["seats"] = seat_rows
        return game

    def _close_trace(self, engine) -> Path | None:
        if engine is None or getattr(engine, "trace_path", None) is None:
            return None
        engine.stop()
        deadline = time.monotonic() + 60
        while engine.trace is not None and not engine.trace.closed and time.monotonic() < deadline:
            time.sleep(0.05)
        path = engine.trace_path
        if not path.exists():
            return None
        gz = path.with_suffix(".trace.gz")
        with open(path, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
        return gz
