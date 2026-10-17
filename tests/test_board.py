"""The board (lab/board.py): one schedule for every entry, entries per family, reuse, paired standings."""

from __future__ import annotations

import json

import pytest

from lab import board
from lab import bots as registry
from lab.db import LabDB

CFG = {"reference": "ref", "fixed": ["old"], "baselines": ["ai-rush", "ai-normal"], "games": 2, "seed": 9,
       "minutes": 20, "maps": ["trapped", "sidestep"], "entries_per_family": 1}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from lab import moments

    monkeypatch.setattr(moments, "_engine", lambda: "eng")  # (the board counts games of the current engine)
    d = LabDB(tmp_path / "lab.db")
    yield d
    d.close()


def test_every_entry_gets_the_same_games():
    a, b = board.schedule("a", CFG), board.schedule("b", CFG)
    assert len(a) == 4
    assert [(j["map"], j["seed"], j["bots"].index("a")) for j in a] == [(j["map"], j["seed"], j["bots"].index("b")) for j in b]
    assert {j["extra"]["baseline"] for j in a} == {"ai-rush", "ai-normal"} and {j["label"] for j in a} == {"board"}


def _bot(name, source="", tags=()):
    return registry.Bot(name=name, kind="ff", source=source, tags=list(tags))


def test_entries_of_a_family_and_of_the_main_checkout():
    table = {"ref": _bot("ref"), "old": _bot("old"), "rush-a": _bot("rush-a", "rush", ["board", "frozen"]),
             "eco-a": _bot("eco-a", "eco", ["board", "frozen"])}
    assert board.entries(table, CFG, family="rush") == ["rush-a"]
    assert board.entries(table, CFG, family=None) == ["ref", "old", "rush-a", "eco-a"]
    table["rush-b"] = _bot("rush-b", "rush", ["board", "frozen"])
    with pytest.raises(ValueError, match="at most 1"):
        board.entries(table, CFG, family="rush")
    table["rush-b"] = registry.Bot(name="rush-b", kind="module", source="rush", tags=["board"])
    table["rush-a"].tags = []
    with pytest.raises(ValueError, match="isn't frozen"):
        board.entries(table, CFG, family="rush")
    table["rush-b"] = _bot("rush-b", "rush", ["board"])  # (a knobs bot, kind ff, can't be frozen: entered as is)
    assert board.entries(table, CFG, family="rush") == ["rush-b"]


def _game(db, gid, job, results, *, version="1", engine="eng", finished=1.0):
    """A recorded board game: ``results`` is (points-result, net) for each seat."""
    game = {"id": gid, "run_id": None, "created": 1.0, "finished": finished, "map": job["map"], "minutes": 20,
            "seed": job["seed"], "time_mode": "virtual", "game_seconds": 1200.0, "wall_seconds": 1.0,
            "termination": "time_limit", "winner": None, "outcome": "decisive", "branch_of": None, "branch_tick": None,
            "branch_label": None, "trace": None, "engine": engine,
            "info": json.dumps({"start_tick": 0, "label": "board", **job["extra"]})}
    seats = [{"game_id": gid, "position": p, "bot": name, "version": f"{name}@{version}", "kind": "ff",
              "result": results[p][0], "net": results[p][1]} for p, name in enumerate(job["bots"])]
    db.save_game(game, seats, [], [], {})


def test_a_board_game_played_the_same_way_is_not_played_again(db, monkeypatch):
    from lab import moments

    monkeypatch.setattr(moments, "_versions", lambda: (lambda name: name + "@1"))
    monkeypatch.setattr(moments, "_engine", lambda: "eng")
    first = board.schedule("ref", CFG)[0]
    me = first["bots"].index("ref")
    _game(db, "g1", first, [("win", 100), ("loss", -100)] if me == 0 else [("loss", -100), ("win", 100)])
    todo, reused = board.plan(db, ["ref"], CFG)
    assert reused == 1 and len(todo) == 3
    monkeypatch.setattr(moments, "_engine", lambda: "another-build")
    assert board.plan(db, ["ref"], CFG) == (board.schedule("ref", CFG), 0)


def test_standings_pair_each_entry_with_the_reference(db):
    for name, outcome in (("ref", "draw"), ("rush-a", "win")):
        for k, job in enumerate(board.schedule(name, CFG)):
            me = job["bots"].index(name)
            res = [None, None]
            res[me] = (outcome, 500 if outcome == "win" else 0)
            res[1 - me] = ({"win": "loss", "draw": "draw"}[outcome], -res[me][1])
            _game(db, f"{name}-{k}", job, res)
    rows = {r["bot"]: r for r in board.standings(db, CFG)}
    assert rows["rush-a"]["points"] == 1.0 and rows["ref"]["points"] == 0.5
    assert rows["rush-a"]["diff"] == 0.5 and rows["rush-a"]["pairs"] == 4 and rows["ref"]["diff"] is None
    assert rows["rush-a"]["by_baseline"] == {"ai-rush": (2.0, 2), "ai-normal": (2.0, 2)}
    text = board.report(db, CFG)
    assert text.splitlines()[3].split()[1] == "rush-a" and "(reference)" in text


def test_only_the_latest_version_counts(db):
    for k, job in enumerate(board.schedule("rush-a", CFG)):
        me = job["bots"].index("rush-a")
        res = [("loss", -1), ("loss", -1)]
        res[me] = ("loss", -100)
        _game(db, f"old-{k}", job, res, version="1")
    job = board.schedule("rush-a", CFG)[0]
    me = job["bots"].index("rush-a")
    res = [("loss", 0), ("loss", 0)]
    res[me] = ("win", 100)
    _game(db, "new-0", job, res, version="2", finished=2.0)
    row = next(r for r in board.standings(db, CFG) if r["bot"] == "rush-a")
    assert row["version"] == "rush-a@2" and row["games"] == 1 and row["points"] == 1.0


def test_snapshots_keep_the_history(db):
    job = board.schedule("ref", CFG)[0]
    me = job["bots"].index("ref")
    res = [("loss", 0), ("loss", 0)]
    res[me] = ("win", 10)
    _game(db, "g", job, res)
    assert board.save_snapshot(db, CFG) == 1
    assert "ref" in board.history(db) and "ref" in board.history(db, "ref")
