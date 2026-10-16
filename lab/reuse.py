"""Whole games already played the same way are not played again (lab play/compare/league/gate/exploit).

Lab games are deterministic: the same bot versions in the same seats, on the same map, seed and
length, on the same engine build, play the same game move for move (runs 124, 126-127; and e.g.
32768c8c and fc790b41, runs 150 and 155). So a run links such a game into itself (``run_games``)
instead of playing it, and reports count it as the run's own. Every family puts the champion first
in ``lab compare``: without this, its reference games were played again in every run.

Not reused: games of bots whose games don't replay (Jev's answers come over the network, a language
model's even more so, and ``think: measured`` charges real time), branches, games with a stop rule
(a probe's), and games that failed or had a bot error. ``--no-reuse`` plays everything.
"""

from __future__ import annotations

import json

NONDETERMINISTIC_TAGS = {"llm", "advisor", "needs-openai"}
STOP_RULES = ("stop_at_seconds", "stop_quiet")


def replays(bot, uses_jev) -> bool:
    """Does this bot play a seeded game the same way every time?"""
    if bot.kind == "ai":
        return True
    if bot.kind == "human" or not isinstance(bot.think, (int, float)):
        return False  # ("measured": real decision time; "paused": a person or an advisor)
    if NONDETERMINISTIC_TAGS & set(bot.tags or []):
        return False
    return not uses_jev(bot.name)


def reusable(job: dict, table: dict, uses_jev) -> bool:
    if job.get("branch") or any((job.get("extra") or {}).get(k) is not None for k in STOP_RULES):
        return False
    return all(name in table and replays(table[name], uses_jev) for name in job["bots"])


def existing_game(db, job: dict, version, engine: str, exclude=()) -> str | None:
    """A whole game already played the same way: the same bots (versions) in the same seats, map,
    seed, length and engine build, that ended normally."""
    rows = db.q("SELECT id, info FROM games WHERE branch_of IS NULL AND map = ? AND seed = ? AND minutes = ? "
                "AND engine = ? AND outcome != 'no_result' ORDER BY finished DESC",
                job["map"], job["seed"], job["minutes"], engine)
    for r in rows:
        if r["id"] in exclude:
            continue
        info = json.loads(r["info"] or "{}")
        if info.get("errors") or info.get("failure") or any(info.get(k) is not None for k in STOP_RULES):
            continue
        seats = {s["position"]: s["version"] for s in db.q("SELECT position, version FROM seats WHERE game_id = ?", r["id"])}
        if len(seats) == len(job["bots"]) and all(seats.get(pos) == version(name) for pos, name in enumerate(job["bots"])):
            return r["id"]
    return None


def split(db, jobs: list[dict], table: dict, uses_jev) -> tuple[list[dict], list[str]]:
    """The jobs still to play, and the recorded games that stand for the others."""
    from .moments import _engine, _versions

    version, engine = _versions(), _engine()
    todo, found = [], []
    for job in jobs:
        gid = existing_game(db, job, version, engine, exclude=set(found)) if reusable(job, table, uses_jev) else None
        if gid:
            found.append(gid)
        else:
            todo.append(job)
    return todo, found


def link(db, run_id: int, game_ids: list[str]) -> None:
    for gid in game_ids:
        db.conn.execute("INSERT OR IGNORE INTO run_games (run_id, game_id) VALUES (?, ?)", (run_id, gid))


def run_clause(alias: str, runs: list[int]) -> tuple[str, list]:
    """SQL for "the games of these runs": played in them, or reused into them."""
    marks = ",".join("?" * len(runs))
    return (f"({alias}.run_id IN ({marks}) OR {alias}.id IN (SELECT game_id FROM run_games WHERE run_id IN ({marks})))",
            [*runs, *runs])
