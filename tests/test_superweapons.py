"""Superweapon hits in AI games are flagged (lab/superweapons.py); lab games are played without them."""

from __future__ import annotations

import json

import pytest

from lab import superweapons
from lab.db import LabDB


@pytest.fixture()
def db(tmp_path):
    d = LabDB(tmp_path / "lab.db")
    yield d
    d.close()


def _game(db, gid, losses, *, kinds=("ff", "ai"), result="loss", net_before=500, end=-9000):
    game = {"id": gid, "run_id": None, "created": 1.0, "finished": 1.0, "map": "trapped", "minutes": 20, "seed": 1,
            "time_mode": "virtual", "game_seconds": 1200.0, "wall_seconds": 1.0, "termination": "time_limit",
            "winner": 1, "outcome": "decisive", "branch_of": None, "branch_tick": None, "branch_label": None,
            "trace": None, "engine": "eng", "info": "{}"}
    seats = [{"game_id": gid, "position": p, "bot": f"b{p}", "version": f"b{p}@1", "kind": k,
              "result": result if p == 0 else "win", "net": end if p == 0 else -end} for p, k in enumerate(kinds)]
    timeline = [(gid, 0, 24000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1000 + net_before, 1000, 0, 0.0, 0, 0)]
    events = [(gid, 0, t, "building_lost", "powr", 1, 1, None) for t in losses]
    db.save_game(game, seats, timeline, events, {})


def test_a_burst_of_building_losses_against_an_ai_is_flagged_and_marked_decided(db):
    _game(db, "g", [25000, 25010, 25020, 25030, 25040, 25050])
    hit = superweapons.flag(db, "g")
    assert hit["buildings"] == 6 and hit["at"] == "16:40" and hit["net_before"] == 500 and hit["decided"]
    assert json.loads(db.one("SELECT info FROM games WHERE id = 'g'")["info"])["superweapon"]["buildings"] == 6


def test_losses_seconds_apart_or_against_an_agent_are_not(db):
    _game(db, "slow", [25000 + 100 * k for k in range(8)])
    _game(db, "agents", [25000, 25001, 25002, 25003, 25004, 25005], kinds=("ff", "ff"))
    assert superweapons.detect(db, "slow") is None and superweapons.detect(db, "agents") is None


def test_behind_before_the_hit_is_not_decided_by_it(db):
    _game(db, "g", [25000 + k for k in range(5)], net_before=-4000)
    assert superweapons.detect(db, "g")["decided"] is False


def test_the_lab_lobby_is_part_of_the_engine_id(monkeypatch):
    import lab
    from lab import bots

    bots._engine_hash.cache_clear()
    monkeypatch.setattr(lab, "LAB_LOBBY", {"techlevel": "nosuperweapons"})
    with_lobby = bots._engine_hash()
    bots._engine_hash.cache_clear()
    monkeypatch.setattr(lab, "LAB_LOBBY", {})
    try:
            assert with_lobby == bots._engine_hash() + "+techlevel=nosuperweapons"
    finally:
        bots._engine_hash.cache_clear()
