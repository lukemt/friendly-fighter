"""Any moment of a recorded lab game, rebuilt from its engine trace: to look at, or to play on from.

- ``snapshot(db, game, tick)``: replays the game's engine trace up to ``tick`` on an engine of its
  own and returns each agent seat's observation there (as the seat saw it; its event log comes from
  the database). A few seconds per look; the replay is checked step by step against the trace.
- ``branch_jobs(...)``: games that start at that moment, one per variant (a bot for the branched
  seat), repeated with different seeds: the world is the same at the start of every branch, the
  timing of the game after it differs with the seed. The branched seat's bot starts with a fresh
  memory (whatever bot played there before); so does the control branch (the original bot), which
  makes it the fair comparison.

A branch keeps the players of the original game: an agent seat stays an agent seat (any bot) and an
OpenRA AI the same AI (it lives in the engine's world).
"""

from __future__ import annotations

import json

from ffsim import observe
from ffsim.rules import Rules
from ffsim.trace import prefix_until, read_trace

from . import LAB_DIR

TICKS_PER_SECOND = 25


def parse_time(text: str | int | float) -> int:
    """``4:30`` / ``270`` (seconds) / ``t6750`` (a tick) -> a tick."""
    s = str(text).strip()
    if s.startswith("t"):
        return int(s[1:])
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(round((int(m) * 60 + float(sec)) * TICKS_PER_SECOND))
    return int(round(float(s) * TICKS_PER_SECOND))


def game_tick(game_row, seconds: float) -> int:
    """The engine tick of ``seconds`` of game time (counted from the game's first view, as the clock shows)."""
    start = (json.loads(game_row["info"] or "{}")).get("start_tick") or 0
    return start + int(round(seconds * TICKS_PER_SECOND))


def snapshot(db, game_ref: str, seconds: float) -> dict:
    from ffsim.engine_openra import HostPool, OpenRAEngine

    game = db.find_game(game_ref)
    if game is None or not game["trace"]:
        raise ValueError(f"no game {game_ref!r} with a trace")
    head, steps = read_trace(game["trace"])
    tick = game_tick(game, seconds)
    prefix = prefix_until(steps, tick)
    kinds = ["agent" if b.split(":", 1)[1] == "rl-agent" else "ai:" + b.split(":", 1)[1] for b in head["bots"].split(",")]
    pool = HostPool(LAB_DIR / "engines", 1, persist=True)
    try:
        engine = OpenRAEngine("look-" + game["id"], game["map"], kinds, lobby=head["lobby"], lockstep=True,
                              host=pool.take(), seed=head["seed"], prefix=prefix)
        engine.map_file = head["map_file"]
        engine.start(None, None)
        views = engine.prefix_views or []
        diff = engine.prefix_diff
        engine.stop()
    finally:
        pool.stop()
    m = Rules.load().map(game["map"])
    start = (json.loads(game["info"] or "{}")).get("start_tick") or 0
    out = {"game": game["id"], "map": game["map"], "tick": engine.tick, "seconds": (engine.tick - start) / TICKS_PER_SECOND,
           "check": "ok" if diff is None else f"replay differs from step {diff}", "obs": {}, "events": {}}
    for pos, view in views:
        events = [dict(zip(("tick", "kind", "type", "x", "y", "count"), r[2:])) for r in db.q(
            "SELECT * FROM events WHERE game_id = ? AND position = ? AND tick <= ? ORDER BY rowid DESC LIMIT 20",
            game["id"], pos, view["tick"])][::-1]
        out["obs"][pos] = observe.observation(view, game["map"], (m["width"], m["height"]), events)
        out["events"][pos] = events
    return out


def branch_jobs(db, game_ref: str, seconds: float, variants: list[str], *, seat: int, opponent: str | None = None,
                repeats: int = 1, seed: int = 1, stop_after: float | None = None, control: bool = True) -> list[dict]:
    game = db.find_game(game_ref)
    if game is None or not game["trace"]:
        raise ValueError(f"no game {game_ref!r} with a trace")
    seats = {r["position"]: r for r in db.q("SELECT * FROM seats WHERE game_id = ?", game["id"])}
    if seats[seat]["kind"] == "ai":
        raise ValueError(f"seat {seat} is OpenRA's AI ({seats[seat]['bot']}): it can't be replaced; branch the other seat")
    if seats[seat]["kind"] == "human":
        control = False  # (no one to replay a person's play: the variants are compared among themselves)
    other = 1 - seat
    opp = opponent or seats[other]["bot"]
    if seats[other]["kind"] == "human" and not opponent:
        raise ValueError(f"seat {other} was played by a person: name a bot for it with --opponent")
    names = list(dict.fromkeys(([seats[seat]["bot"]] if control else []) + variants))
    tick = game_tick(game, seconds)
    jobs = []
    for r in range(repeats):
        for name in names:
            bots = [None, None]
            bots[seat], bots[other] = name, opp
            label = ("control:" if control and name == seats[seat]["bot"] else "") + name
            extra = {"branch_seat": seat}
            if stop_after:
                extra["stop_at_seconds"] = seconds + stop_after
            jobs.append({"bots": bots, "map": game["map"], "minutes": game["minutes"], "seed": seed * 7919 + r,
                         "label": f"branch:{label}", "branch": {"of": game["id"], "tick": tick, "label": label},
                         "extra": extra})
    return jobs


def timelapse(db, game_ref: str, every: float = 60.0, start: float = 0.0, end: float | None = None):
    """The game's agent seats every ``every`` seconds from ``start`` to ``end``, from one replay of the
    trace: yields ``(seconds, {position: observation})``."""
    from ffsim.engine_openra import HostPool, view_from_pb

    game = db.find_game(game_ref)
    if game is None or not game["trace"]:
        raise ValueError(f"no game {game_ref!r} with a trace")
    head, steps = read_trace(game["trace"])
    first = (json.loads(game["info"] or "{}")).get("start_tick") or 0
    m = Rules.load().map(game["map"])
    size = (m["width"], m["height"])
    end = game["game_seconds"] if end is None else min(end, game["game_seconds"] or end)
    marks = []
    t = start
    while t <= (end or 0) + 1e-9:
        marks.append(first + int(round(t * TICKS_PER_SECOND)))
        t += every
    pool = HostPool(LAB_DIR / "engines", 1, persist=True)
    host = pool.take()
    session = host.create(head["map_file"], head["bots"], head["seed"], head["lobby"])
    try:
        latest: dict = {}
        k = 0
        for request, _, _ in steps:
            if k >= len(marks):
                break
            request.session_id = session
            reply = host.step(request)
            for o in reply.observations:
                latest[int(o.player[5:])] = view_from_pb(o.observation)
            while k < len(marks) and latest and reply.tick >= marks[k]:
                obs = {pos: observe.observation(v, game["map"], size, []) for pos, v in latest.items()}
                yield (marks[k] - first) / TICKS_PER_SECOND, obs
                k += 1
    finally:
        try:
            host.destroy(session)
        finally:
            pool.stop()
