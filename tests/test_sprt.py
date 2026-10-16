"""Stop a compare when it is clear (lab/sprt.py, lab compare --sprt), and seeds shared by every run."""

from __future__ import annotations

import json

import pytest

from lab import batch, league, reuse, sprt
from lab.db import LabDB


def test_the_test_needs_its_pairs_and_then_decides():
    assert sprt.decide([1.0] * 5) is None  # (fewer than MIN_PAIRS)
    assert sprt.decide([1.0, 0.0] * 10) == "better"  # +0.5 a game
    assert sprt.decide([-1.0, 0.0] * 10) == "not better"
    assert sprt.decide([0.0] * 12) is None  # all equal, early: the prior variance holds it back
    assert sprt.decide([0.0] * 100) == "not better"  # a hundred equal pairs: no gain of 0.1


def test_bounds_and_their_order():
    lo, hi = sprt.bounds(0.05, 0.05)
    assert lo == pytest.approx(-2.944, abs=1e-3) and hi == pytest.approx(2.944, abs=1e-3)
    assert sprt.parse_bounds("0,0.1") == (0.0, 0.1)
    with pytest.raises(ValueError):
        sprt.parse_bounds("0.1,0")


def test_every_command_plays_an_opponent_on_the_same_seeds():
    a = league.compare(["v10", "c"], ["ai-rush", "ai-normal"], 4, seed=2026)
    b = league.compare(["v10"], ["ai-normal", "ext-henry", "ai-rush"], 4, seed=2026)
    key = lambda j: (tuple(j["bots"]), j["map"], j["seed"])  # noqa: E731
    v10 = lambda js: {key(j) for j in js if "v10" in j["bots"] and "ai-rush" in j["bots"]}  # noqa: E731
    assert v10(a) == v10(b)  # (the baseline's place in the list no longer matters)
    board = league.matchup("v10", "ai-rush", 4, seed=2026)
    assert {key(j) for j in board} == v10(a)  # (the board's games are a compare's on its seed)
    gate = league.gate("c", "v10", ["ai-rush"], 4, seed=2026)
    assert {key(j) for j in gate if "v10" in j["bots"] and "ai-rush" in j["bots"]} == v10(a)


def test_interleaved_goes_round_the_baselines():
    jobs = league.compare(["r", "c"], ["x", "y"], 2, interleaved=True)
    assert [(j["label"], j["seed"]) for j in jobs][::2] == [("compare:x", 10007), ("compare:y", 10007),
                                                            ("compare:x", 10008), ("compare:y", 10008)]
    assert sorted(map(json.dumps, jobs)) == sorted(map(json.dumps, league.compare(["r", "c"], ["x", "y"], 2)))


def test_the_feed_skips_what_is_decided_and_stops_every_worker():
    queue = []
    stop = set()
    feed = batch.Feed([{"n": i} for i in range(6)], lambda i: i not in stop, queue.append, 2, run_id=5)
    feed(2)
    assert queue == [{"n": 0, "run_id": 5}, {"n": 1, "run_id": 5}]
    stop.update({2, 3})
    feed(1)
    assert queue[-1] == {"n": 4, "run_id": 5} and feed.skipped == 2
    feed(1)
    feed(1)
    assert queue[-3:] == [{"n": 5, "run_id": 5}, None, None] and feed.closed


@pytest.fixture()
def db(tmp_path):
    d = LabDB(tmp_path / "lab.db")
    yield d
    d.close()


def _game(db, gid, run_id, bot, opp, seed, result):
    game = {"id": gid, "run_id": run_id, "created": 1.0, "finished": 1.0, "map": "trapped", "minutes": 20, "seed": seed,
            "time_mode": "virtual", "game_seconds": 1200.0, "wall_seconds": 1.0, "termination": "time_limit",
            "winner": 0, "outcome": "decisive", "branch_of": None, "branch_tick": None, "branch_label": None,
            "trace": None, "engine": "eng", "info": "{}"}
    pts = {"win": 1.0, "loss": 0.0, "draw": 0.5}[result]
    other = {"win": "loss", "loss": "win", "draw": "draw"}[result]
    seats = [{"game_id": gid, "position": 0, "bot": bot, "version": f"{bot}@1", "kind": "module", "result": result,
              "points": pts, "net": 0},
             {"game_id": gid, "position": 1, "bot": opp, "version": f"{opp}@1", "kind": "ai", "result": other,
              "points": 1 - pts, "net": 0}]
    db.save_game(game, seats, [], [], {})


def test_a_candidate_stops_when_clear_and_the_reference_with_the_last(db):
    run = db.start_run("compare", "test", {})
    jobs = league.compare(["ref", "good", "even"], ["ai"], 40, interleaved=True)
    for i in range(20):  # (20 pairs in: good wins where ref lost on half; even plays as ref; one game reused)
        seed = league.game_seed(1, i)
        _game(db, f"r{i}", run if i else 99, "ref", "ai", seed, "loss" if i % 2 else "win")
        _game(db, f"g{i}", run, "good", "ai", seed, "win")
        _game(db, f"e{i}", run, "even", "ai", seed, "loss" if i % 2 else "win")
    reuse.link(db, run, ["r0"])
    test = sprt.Sequential(db, run, jobs, ["ref", "good", "even"], 0.0, 0.1, 0.05, 0.05)
    assert test.verdict == {"good": "better", "even": None} and test.pairs["good"] == 20
    later = [i for i, j in enumerate(jobs) if j["seed"] == league.game_seed(1, 30)]
    assert {jobs[i]["bots"][0] if "ai" != jobs[i]["bots"][0] else jobs[i]["bots"][1]: test.keep(i) for i in later} == {
        "ref": True, "good": False, "even": True}
    test.verdict["even"] = "not better"
    assert not any(test.keep(i) for i in later)  # (all decided: the reference's games aren't needed)
    assert "BETTER" in test.summary() and "after 20 pairs" in test.summary()


def test_a_sequential_gate_plays_the_same_games_round_by_round():
    plain = league.gate("c", "v10", ["x", "y"], 3, seed=4)
    rounds = league.gate("c", "v10", ["x", "y"], 3, seed=4, interleaved=True)
    assert sorted(map(json.dumps, plain)) == sorted(map(json.dumps, rounds))
    assert [j["label"] for j in rounds[:5]] == ["gate:c:h2h", "gate:c:x", "gate:c:x", "gate:c:y", "gate:c:y"]
    assert {j["seed"] for j in rounds[:5]} == {league.game_seed(4, 0)}
