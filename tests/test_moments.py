"""Moment sets and probes (lab/moments.py): finding moments, sets, probe jobs, the paired report."""

from __future__ import annotations

import json

import pytest

from lab import moments
from lab.db import LabDB

TPS = 25


@pytest.fixture(autouse=True)
def versions(monkeypatch):
    """The test bots aren't registered: every bot is at version NAME@1, the engine build 'eng'."""
    monkeypatch.setattr(moments, "_versions", lambda: (lambda name: name + "@1"))
    monkeypatch.setattr(moments, "_engine", lambda: "eng")


@pytest.fixture()
def db(tmp_path):
    d = LabDB(tmp_path / "lab.db")
    yield d
    d.close()


def _game(db, gid, *, bot="ff-a", opp="ai-rush", opp_kind="ai", result="draw", termination="time_limit",
          seconds=1200.0, run_id=1, series=(), events=(), branch=None, info=None):
    """A recorded game: seat 0 is our bot (its score series as (seconds, kills, deaths)), seat 1 the opponent."""
    other = {"loss": "win", "win": "loss"}.get(result, result)
    game = {"id": gid, "run_id": run_id, "created": 1.0, "finished": 2.0, "map": "trapped", "minutes": 20, "seed": 1,
            "time_mode": "virtual", "game_seconds": seconds, "wall_seconds": 1.0, "termination": termination,
            "winner": None, "outcome": "decisive", "branch_of": None, "branch_tick": None, "branch_label": None,
            "trace": "/nonexistent.trace.gz", "engine": "eng", "info": json.dumps({"start_tick": 0, **(info or {})})}
    if branch:
        game.update(branch)
    seats = [{"game_id": gid, "position": 0, "bot": bot, "version": bot + "@1", "kind": "module", "result": result},
             {"game_id": gid, "position": 1, "bot": opp, "version": opp + "@1", "kind": opp_kind, "result": other}]
    timeline = [(gid, 0, int(t * TPS)) + (0,) * 9 + (k, d, 0, 0.0, 0, 0) for t, k, d in series]
    db.save_game(game, seats, timeline, [(gid, 0, int(t * TPS), kind, typ, 0, 0, 1) for t, kind, typ in events], {})


def test_wiped_moments_lead_the_end_and_skip_games_not_lost(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    _game(db, "g2", result="win", termination="time_limit")
    _game(db, "g3", result="loss", termination="victory", seconds=100.0)  # too early for a 120 s lead
    found = moments.find(db, "wiped", {})
    assert [(m["game_id"], m["seat"], m["seconds"]) for m in found] == [("g1", 0, 880.0)]
    assert "wiped at 16:40" in found[0]["why"]


def test_filters_by_run_bot_and_opponent(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0, run_id=1)
    _game(db, "g2", result="loss", termination="victory", seconds=1000.0, run_id=2, opp="ai-normal")
    _game(db, "g3", result="loss", termination="victory", seconds=1000.0, run_id=2, bot="ff-b")
    assert [m["game_id"] for m in moments.find(db, "wiped", {"runs": [2]})] == ["g2", "g3"]
    assert [m["game_id"] for m in moments.find(db, "wiped", {"vs": "ai-normal"})] == ["g2"]
    assert [m["game_id"] for m in moments.find(db, "wiped", {"bot": "ff-b"})] == ["g3"]


def test_an_ai_seat_is_never_a_moment(db):
    _game(db, "g1", bot="ai-turtle", result="loss", termination="victory", seconds=1000.0)
    db.conn.execute("UPDATE seats SET kind = 'ai' WHERE game_id = 'g1' AND position = 0")
    assert [m["seat"] for m in moments.find(db, "wiped", {})] == []


def test_swing_finds_the_first_fall_of_the_score(db):
    series = [(t, 1000, 0) for t in range(0, 600, 5)] + [(600, 1000, 3000), (630, 1000, 7000), (700, 1000, 7000)]
    _game(db, "g1", series=series)
    found = moments.find(db, "swing", {}, drop=5000)
    assert len(found) == 1 and found[0]["seconds"] == pytest.approx(595 - 15 + 0.0, abs=5.1)
    assert moments.find(db, "swing", {}, drop=8000) == []


def test_building_lost_and_at(db):
    _game(db, "g1", events=[(300, "building_lost", "powr"), (700, "building_lost", "fix")])
    assert [m["seconds"] for m in moments.find(db, "building-lost", {})] == [270.0]
    assert [m["seconds"] for m in moments.find(db, "building-lost", {}, after=600)] == [670.0]
    assert [m["seconds"] for m in moments.find(db, "at", {}, at=780.0)] == [780.0]
    with pytest.raises(ValueError):
        moments.find(db, "at", {})


def test_sets_keep_a_moment_once_and_drop(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    found = moments.find(db, "wiped", {})
    assert moments.save(db, "late", found) == 1
    assert moments.save(db, "late", found) == 0
    assert [r["n"] for r in moments.sets(db)] == [1]
    assert len(moments.moments(db, "@late")) == 1
    moments.drop(db, "late")
    assert moments.moments(db, "late") == []


def test_probe_jobs_branch_each_moment_with_the_control_and_variants(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    jobs, reused = moments.probe_jobs(db, "late", ["ff-b", "ff-c"], stop_after=180, spread=2)
    assert len(jobs) == 2 * 3 and reused == 0
    assert [j["branch"]["label"] for j in jobs[:3]] == ["control:ff-a", "ff-b", "ff-c"]
    assert {j["bots"][1] for j in jobs} == {"ai-rush"}
    first, later = jobs[0], jobs[3]
    assert first["extra"]["probe"] == "late" and first["extra"]["stop_at_seconds"] == 880 + 180
    assert later["branch"]["tick"] - first["branch"]["tick"] == 2 * TPS and later["extra"]["offset"] == 2.0
    with pytest.raises(ValueError):
        moments.probe_jobs(db, "empty", ["ff-b"])


def _branch(db, gid, mid, label, start_net, end_net, *, result="draw", termination="horizon", offset=0.0,
            of="g1", tick=880 * TPS, seed=1, stop_after=300.0):
    _game(db, gid, bot=label.removeprefix("control:"), result=result, termination=termination, seconds=1060.0,
          series=[(880, start_net, 0), (1060, end_net, 0)],
          branch={"branch_of": of, "branch_tick": tick, "branch_label": label, "seed": seed},
          info={"moment": mid, "probe": "late", "offset": offset, "branch_seat": 0, "stop_after": stop_after,
                "stop_quiet": None})


def test_the_report_pairs_each_variant_with_the_control_at_the_same_moments(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    _game(db, "g2", result="loss", termination="victory", seconds=1100.0)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    m1, m2 = (m["id"] for m in moments.moments(db, "late"))
    _branch(db, "b1", m1, "control:ff-a", 1000, 0, result="loss", termination="victory")
    _branch(db, "b2", m1, "ff-b", 1000, 3000)
    _branch(db, "b3", m2, "control:ff-a", 0, -2000)
    _branch(db, "b4", m2, "ff-b", 0, 500)
    data = moments.outcomes(db, "late")
    assert data[(m1, 0.0)]["ff-a"]["control"] and data[(m1, 0.0)]["ff-a"]["wiped"]
    assert data[(m1, 0.0)]["ff-b"]["delta"] == 2000
    text = moments.probe_report(db, "late")
    control_row = next(line for line in text.splitlines() if line.startswith("control "))
    variant_row = next(line for line in text.splitlines() if line.startswith("ff-b"))
    assert "-1500" in control_row.replace(",", "") and "ff-a" in text.splitlines()[-4]
    assert "+2750" in variant_row.replace(",", "") and "2/2" in variant_row and "too few" in variant_row


def test_three_pairs_give_a_verdict(db):
    for k in range(3):
        _game(db, f"g{k}", result="loss", termination="victory", seconds=1000.0 + 10 * k)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    for k, m in enumerate(moments.moments(db, "late")):
        _branch(db, f"c{k}", m["id"], "control:ff-a", 0, -2000 - 100 * k)
        _branch(db, f"v{k}", m["id"], "ff-b", 0, 1000 + 50 * k)
    assert "BETTER" in next(line for line in moments.probe_report(db, "late").splitlines() if line.startswith("ff-b"))


def test_mean_ci_widens_with_few_values():
    m, lo, hi = moments.mean_ci([100.0, 200.0, 300.0])
    assert m == 200.0 and lo < 0 < hi  # 3 values: t(2) = 4.30
    m, lo, hi = moments.mean_ci([100.0, 110.0, 90.0, 105.0, 95.0, 100.0])
    assert lo > 0


def test_a_branch_played_the_same_way_is_reused(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    jobs, _ = moments.probe_jobs(db, "late", ["ff-b"], stop_after=300)
    control = jobs[0]
    mid = control["extra"]["moment"]
    _branch(db, "c1", mid, "control:ff-a", 0, -100, tick=control["branch"]["tick"], seed=control["seed"])
    jobs, reused = moments.probe_jobs(db, "late", ["ff-b"], stop_after=300)
    assert reused == 1 and [j["branch"]["label"] for j in jobs] == ["ff-b"]
    jobs, reused = moments.probe_jobs(db, "late", ["ff-b"], stop_after=120)  # another stop rule: not the same game
    assert reused == 0 and len(jobs) == 2
    jobs, reused = moments.probe_jobs(db, "late", ["ff-b"], stop_after=300, reuse=False)
    assert reused == 0 and len(jobs) == 2


def test_a_changed_bot_is_not_reused_and_its_old_branches_are_left_out(db, monkeypatch):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    jobs, _ = moments.probe_jobs(db, "late", ["ff-b"], stop_after=300)
    mid = jobs[0]["extra"]["moment"]
    for j in jobs:
        _branch(db, "x" + j["branch"]["label"], mid, j["branch"]["label"], 0, 100, tick=j["branch"]["tick"], seed=j["seed"])
    assert moments.probe_jobs(db, "late", ["ff-b"], stop_after=300)[1] == 2
    monkeypatch.setattr(moments, "_versions", lambda: (lambda name: name + ("@2" if name == "ff-b" else "@1")))
    jobs, reused = moments.probe_jobs(db, "late", ["ff-b"], stop_after=300)
    assert reused == 1 and [j["branch"]["label"] for j in jobs] == ["ff-b"]
    counts: dict = {}
    data = moments.outcomes(db, "late", counts=counts)
    assert counts["stale"] == 1 and list(next(iter(data.values()))) == ["ff-a"]


def test_halving_keeps_the_better_half_and_drops_the_clearly_worse():
    stats = {"a": {"mean": 900.0, "verdict": "BETTER"}, "b": {"mean": 100.0, "verdict": "unclear"},
             "c": {"mean": -50.0, "verdict": "unclear"}, "d": {"mean": -800.0, "verdict": "WORSE"}}
    assert moments.survivors(stats) == ["a", "b"]
    assert moments.survivors({"d": stats["d"]}) == ["d"]  # (never none)
    stats["b"]["verdict"] = "WORSE"
    assert moments.survivors(stats) == ["a"]


def test_halving_order_is_shuffled_but_fixed(db):
    for k in range(6):
        _game(db, f"g{k}", result="loss", termination="victory", seconds=1000.0 + k)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    ids = [m["id"] for m in moments.moments(db, "late")]
    order = moments.halving_order(db, "late", seed=3)
    assert sorted(order) == ids and order == moments.halving_order(db, "late", seed=3)


def test_a_quiet_branch_ends_on_the_score():
    from lab.arena import _Quiet

    class Match:
        phase, seconds, ended = "playing", 0.0, None

        def game_seconds(self):
            return self.seconds

        def _tiebreak(self, reason):
            self.ended, self.phase = reason, "finished"

    class Seat:
        position = 0

    match, quiet = Match(), _Quiet(30)
    for t, k in [(0, 100), (10, 200), (35, 200), (39, 200)]:
        match.seconds = t
        quiet.on_view(match, Seat(), {"military": {"kills_cost": k, "deaths_cost": 0}})
    assert match.ended is None  # quiet since 10 s: 29 s
    match.seconds = 41
    quiet.on_view(match, Seat(), {"military": {"kills_cost": 200, "deaths_cost": 0}})
    assert match.ended == "quiet"


def test_a_moment_played_by_hand_is_the_probe_branch_with_a_person(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    mid = moments.moments(db, "late")[0]["id"]
    mine, control = moments.human_job(db, mid, "claude", stop_after=180, stop_quiet=45)
    assert mine["bots"][0] == "human-claude" and mine["bots"][1] == control["bots"][1] == "ai-rush"
    assert mine["branch"]["tick"] == control["branch"]["tick"] and mine["seed"] == control["seed"]
    assert mine["extra"]["moment"] == mid and mine["extra"]["stop_quiet"] == 45 and control["branch"]["label"] == "control:ff-a"
    with pytest.raises(ValueError):
        moments.human_job(db, 999, "claude")


def test_a_person_has_no_version_to_be_stale(db):
    _game(db, "g1", result="loss", termination="victory", seconds=1000.0)
    moments.save(db, "late", moments.find(db, "wiped", {}))
    mid = moments.moments(db, "late")[0]["id"]
    _branch(db, "c", mid, "control:ff-a", 0, -2000)
    _branch(db, "h", mid, "human-claude", 0, 1500)
    db.conn.execute("UPDATE seats SET kind = 'human', version = 'human-claude@zzz' WHERE game_id = 'h' AND position = 0")
    counts: dict = {}
    data = moments.outcomes(db, "late", counts=counts)
    assert counts["stale"] == 0 and data[(mid, 0.0)]["human-claude"]["delta"] == 1500
