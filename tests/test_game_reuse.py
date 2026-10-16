"""Whole games played the same way are linked into a run, not played again (lab/reuse.py)."""

from __future__ import annotations

import json

import pytest

from lab import bots as registry
from lab import report, reuse
from lab.db import LabDB


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from lab import moments

    monkeypatch.setattr(moments, "_versions", lambda: (lambda name: name + "@1"))
    monkeypatch.setattr(moments, "_engine", lambda: "eng")
    d = LabDB(tmp_path / "lab.db")
    yield d
    d.close()


TABLE = {"a": registry.Bot(name="a", kind="ff", think=0.005), "ai-rush": registry.Bot(name="ai-rush", kind="ai"),
         "llm": registry.Bot(name="llm", kind="module", think="measured"),
         "adv": registry.Bot(name="adv", kind="module", think=0.005, tags=["advisor"])}
JOB = {"bots": ["a", "ai-rush"], "map": "trapped", "seed": 7, "minutes": 20, "label": "compare:ai-rush", "extra": {}}


def _game(db, gid, job, *, run_id=99, info=None, outcome="decisive", version="1"):
    game = {"id": gid, "run_id": run_id, "created": 1.0, "finished": 1.0, "map": job["map"], "minutes": job["minutes"],
            "seed": job["seed"], "time_mode": "virtual", "game_seconds": 1200.0, "wall_seconds": 1.0,
            "termination": "time_limit", "winner": 0, "outcome": outcome, "branch_of": None, "branch_tick": None,
            "branch_label": None, "trace": None, "engine": "eng", "info": json.dumps({"label": job["label"], **(info or {})})}
    seats = [{"game_id": gid, "position": p, "bot": n, "version": f"{n}@{version}", "kind": TABLE[n].kind,
              "result": "win" if p == 0 else "loss", "net": 100 if p == 0 else -100} for p, n in enumerate(job["bots"])]
    db.save_game(game, seats, [], [], {})


def no_jev(name):
    return False


def test_a_game_played_the_same_way_is_reused_and_counts_for_the_new_run(db):
    _game(db, "g1", JOB)
    todo, found = reuse.split(db, [JOB, {**JOB, "seed": 8}], TABLE, no_jev)
    assert found == ["g1"] and [j["seed"] for j in todo] == [8]
    run = db.start_run("compare", "test", {})
    reuse.link(db, run, found)
    assert [r["id"] for r in report.perspective(db, "a", {"run": run})] == ["g1"]
    assert db.run_games(run) == 1


def test_what_is_not_reused(db):
    _game(db, "g1", JOB)
    assert reuse.split(db, [{**JOB, "bots": ["ai-rush", "a"]}], TABLE, no_jev)[1] == []  # other seats
    assert reuse.split(db, [{**JOB, "minutes": 10}], TABLE, no_jev)[1] == []
    assert reuse.split(db, [{**JOB, "extra": {"stop_quiet": 45}}], TABLE, no_jev)[1] == []
    assert reuse.split(db, [{**JOB, "branch": {"of": "g0", "tick": 5}}], TABLE, no_jev)[1] == []
    assert reuse.split(db, [JOB], TABLE, lambda name: name == "a")[1] == []  # asks Jev
    for bad in ("llm", "adv"):
        _game(db, f"g-{bad}", {**JOB, "bots": [bad, "ai-rush"]})
        assert reuse.split(db, [{**JOB, "bots": [bad, "ai-rush"]}], TABLE, no_jev)[1] == []


def test_failed_or_erroring_games_are_played_again(db):
    _game(db, "g1", JOB, info={"errors": {"0": "boom"}})
    _game(db, "g2", JOB, outcome="no_result")
    assert reuse.split(db, [JOB], TABLE, no_jev)[1] == []


def test_two_identical_jobs_do_not_share_one_game(db):
    _game(db, "g1", JOB)
    todo, found = reuse.split(db, [JOB, dict(JOB)], TABLE, no_jev)
    assert found == ["g1"] and len(todo) == 1
