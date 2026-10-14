"""The lab (lab/): games in-process on the fake engine, the database, the human seat, ratings, reports."""

from __future__ import annotations

import threading
import time

import pytest

from ffsim.engine_fake import FakeEngine
from lab import bots as registry
from lab import league, ratings, report
from lab.arena import Arena, GameJob
from lab.branch import parse_time
from lab.capture import summarize_orders
from lab.db import LabDB
from lab.human import HumanSeat, parse_units


@pytest.fixture()
def db(tmp_path):
    d = LabDB(tmp_path / "lab.db")
    yield d
    d.close()


def fake_arena(db):
    return Arena(db=db, engine_factory=lambda match, kinds, lobby, opts: FakeEngine(kinds))


def test_a_game_is_played_and_recorded(db):
    arena = fake_arena(db)
    run = db.start_run("test", "pytest", {})
    game = arena.play(GameJob(bots=["ff-default", "ai-normal"], minutes=1, seed=3, run_id=run))
    db.end_run(run)
    assert game["termination"] == "time_limit" and game["outcome"] == "draw"
    seats = db.q("SELECT * FROM seats WHERE game_id = ? ORDER BY position", game["id"])
    assert [s["bot"] for s in seats] == ["ff-default", "ai-normal"]
    assert seats[0]["version"] == registry.get("ff-default").version()
    assert seats[0]["batches"] >= 1 and seats[0]["result"] == "draw"
    assert db.one("SELECT COUNT(*) AS n FROM timeline WHERE game_id = ?", game["id"])["n"] >= 12  # every 5 s
    decisions = db.decisions(game["id"], 0)
    assert decisions[0]["orders"] == [{"cmd": "deploy", "units": [100]}] and decisions[0]["verdicts"] == ["ok"]
    assert db.one("SELECT games FROM runs WHERE id = ?", run)["games"] == 1
    assert db.one("SELECT * FROM bot_versions WHERE bot = 'ff-default'") is not None


def test_two_bots_in_one_process_play_the_same_game_twice(db):
    arena = fake_arena(db)
    a = arena.play(GameJob(bots=["ff-default", "ff-turtle"], minutes=1, seed=9))
    b = arena.play(GameJob(bots=["ff-default", "ff-turtle"], minutes=1, seed=9))
    for pos in (0, 1):
        da, dbb = db.decisions(a["id"], pos), db.decisions(b["id"], pos)
        assert [(d["tick"], d["orders"]) for d in da] == [(d["tick"], d["orders"]) for d in dbb] and da


def test_a_bot_that_does_nothing_still_gets_a_game(db):
    game = fake_arena(db).play(GameJob(bots=["idle", "ai-rush"], minutes=0.5, seed=1))
    seat = db.one("SELECT * FROM seats WHERE game_id = ? AND position = 0", game["id"])
    assert seat["bot"] == "idle" and seat["batches"] == 0 and game["termination"] == "time_limit"


def test_the_human_seat_holds_the_game_between_commands(db):
    arena = fake_arena(db)
    box = {}

    def factory(platform, sid, pos, bot):
        box["seat"] = HumanSeat(platform, sid, pos, bot, autopilot="off", db=db)
        return box["seat"]

    table = registry.load()
    me = registry.get("human:test")
    table[me.name] = me
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("game", arena.play(
        GameJob(bots=[me.name, "ai-normal"], minutes=1, seed=2), human_factory=factory, bots=table)), daemon=True)
    t.start()
    for _ in range(200):
        if "seat" in box and box["seat"].state is not None and box["seat"].state.phase == "playing":
            break
        time.sleep(0.05)
    seat = box["seat"]
    start = seat._seconds()
    time.sleep(0.5)  # nobody calls: no game time passes
    assert seat.command(["state"]).startswith("0:0")
    assert abs(seat._seconds() - start) < 1e-9
    assert "queued" in seat.command(["deploy"])
    out = seat.command(["go", "5"])
    assert "sent 1 order(s)" in out
    assert 5.0 <= seat._seconds() - start <= 6.0
    out = seat.command(["go", "120"])
    assert "GAME OVER" in out
    t.join(30)
    game = done["game"]
    assert db.one("SELECT bot FROM seats WHERE game_id = ? AND position = 0", game["id"])["bot"] == "human-test"
    assert [r["command"] for r in db.q("SELECT command FROM human_log WHERE game_id = ? ORDER BY wall", game["id"])] == [
        "state", "deploy", "go 5", "go 120"]


def test_units_are_picked_by_word():
    obs = {"units": [{"id": 1, "type": "3tnk", "x": 10, "y": 10, "idle": True},
                     {"id": 2, "type": "e1", "x": 30, "y": 30, "idle": False},
                     {"id": 3, "type": "harv", "x": 10, "y": 11, "idle": True}]}
    assert parse_units(obs, ["all"]) == [1, 2]
    assert parse_units(obs, ["idle"]) == [1]
    assert parse_units(obs, ["e1"]) == [2]
    assert parse_units(obs, ["near:10,10,3"]) == [1]
    assert parse_units(obs, ["5,6", "7"]) == [5, 6, 7]


def test_ratings_order_the_bots_and_widen_with_few_games():
    games = [("a", "b", 1.0)] * 9 + [("a", "b", 0.0)] + [("b", "c", 1.0)] * 9 + [("b", "c", 0.5)]
    r = ratings.fit(games)
    assert r["a"]["rating"] > r["b"]["rating"] > r["c"]["rating"]
    few = ratings.fit([("a", "b", 1.0)])
    assert few["a"]["sd"] > r["a"]["sd"]
    assert ratings.expected(1600, 1600) == 0.5
    lo, hi = ratings.wilson(5, 10)
    assert lo < 0.5 < hi


def test_a_league_evens_out_the_pairs_and_skips_ai_against_ai():
    kinds = {"x": "ff", "y": "ff", "ai-normal": "ai", "ai-rush": "ai"}
    jobs = league.league(["x", "y", "ai-normal", "ai-rush"], 25, kinds=kinds)
    pairs = [tuple(sorted(j["bots"])) for j in jobs]
    assert ("ai-normal", "ai-rush") not in pairs
    counts = {p: pairs.count(p) for p in set(pairs)}
    assert len(counts) == 5 and max(counts.values()) - min(counts.values()) <= 1
    gate = league.gate("cand", "champ", ["ai-normal"], 4)
    paired = [j for j in gate if j["label"] == "gate:cand:ai-normal"]
    assert [j["seed"] for j in paired[0::2]] == [j["seed"] for j in paired[1::2]]


def test_versions_follow_the_settings():
    a = registry.Bot(name="v", kind="ff", params={"knobs": {"posture": "defend"}})
    b = registry.Bot(name="v", kind="ff", params={"knobs": {"posture": "attack"}})
    assert a.version() != b.version() and a.version().startswith("v@")
    assert registry.get("ai:rush").seat() == {"kind": "ai:rush", "label": "ai-rush"}
    assert registry.get("human:me").seat()["think"] == "paused"


def test_times_and_orders_read_well():
    assert parse_time("4:30") == 6750 and parse_time("90") == 2250 and parse_time("t100") == 100
    assert summarize_orders([{"cmd": "train", "item": "e1", "count": 3},
                             {"cmd": "attack_move", "units": [1, 2], "to": [40, 52]},
                             {"cmd": "attack", "units": [1], "target": 77}]) == "train:e1x3 attack_move:2u>(40,52) attack:1u>#77"


def test_the_reports_read_the_database(db):
    arena = fake_arena(db)
    for seed in (1, 2):
        arena.play(GameJob(bots=["ff-default", "ai-normal"], minutes=0.5, seed=seed, label="r"))
    f = {"failed": True}
    assert "ff-default" in report.games_table(db, f)
    assert "games" in report.stats(db, "ff-default", f, "map")
    assert "ff-default" in report.observer(db, f)
    assert "rating" in report.ratings_table(db, f)
    assert "ended by" in report.game_detail(db, "last")


def test_a_league_spreads_the_maps():
    kinds = {b: "ff" for b in "abcdef"}
    jobs = league.league(list("abcdef"), 15, kinds=kinds)
    maps = [j["map"] for j in jobs]
    assert {maps.count(m) for m in league.MAPS} == {5}


def _recording(folder, seat, players, *, result=None):
    import gzip
    import json as _json

    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, tick in enumerate(range(17, 17 + 6 * 60, 6)):
        obs = {"tick": tick, "economy": {"cash": 1000}, "military": {"kills_cost": 100 * i, "deaths_cost": 50 * i},
               "units": [], "buildings": [], "events": [{"seq": i + 1, "tick": tick, "kind": "unit_created", "type": "e1"}]}
        raw = {"your_seat": f"Multi{seat}", "players": players, "map": {"id": "trapped"},
               "time": {"game_seconds": (tick - 17) / 25, "max_minutes": 10}, "observation": obs}
        if result and i == 59:
            raw["result"] = result
        rows.append({"t": 1000.0 + i, "kind": "state", "raw": raw})
        rows.append({"t": 1000.0 + i, "kind": "action", "move": {"action": {"type": "orders", "orders": [
            {"cmd": "train", "item": "e1", "count": 1}]}, "reasoning": "train"}})
        rows.append({"t": 1000.0 + i, "kind": "result", "result": {"results": [{"status": "ok", "reason": "ok"}]}})
    with gzip.open(folder / f"seat{seat}.jsonl.gz", "wt") as f:
        for r in rows:
            f.write(_json.dumps(r) + "\n")


def test_platform_recordings_are_imported(db, tmp_path):
    from lab.importer import import_recordings, states_at

    players = [{"seat": "Multi0", "name": "Luke (Player0, Self-hosted)", "faction": "england"},
               {"seat": "Multi1", "name": "Quicksilver (Player1)", "faction": "russia"}]
    gid = "12345678-1234-1234-1234-123456789abc"
    _recording(tmp_path / "rec" / gid, 0, players, result={"outcome": "win", "reason": "time_limit", "winner_seat": "Multi0"})
    (tmp_path / "rec" / "sim-skipped").mkdir()
    assert import_recordings(db, tmp_path / "rec") == [gid]
    assert import_recordings(db, tmp_path / "rec") == []  # (once)
    seats = db.q("SELECT * FROM seats WHERE game_id = ? ORDER BY position", gid)
    assert [s["bot"] for s in seats] == ["ff-live", "platform:Quicksilver"]
    assert [s["result"] for s in seats] == ["win", "loss"] and seats[0]["net"] == 59 * 50 and seats[1]["net"] == -59 * 50
    assert db.one("SELECT time_mode FROM games WHERE id = ?", gid)["time_mode"] == "platform"
    assert len(db.decisions(gid, 0)) == 60 and db.one("SELECT COUNT(*) AS n FROM events WHERE game_id = ?", gid)["n"] == 60
    assert states_at(gid, 10.0, tmp_path / "rec")[0]["time"]["game_seconds"] <= 10.0


def test_the_dashboard_is_one_page(db):
    from lab import dashboard

    fake_arena(db).play(GameJob(bots=["ff-default", "ai-normal"], minutes=0.5, seed=1))
    page = dashboard.build(db)
    assert page.startswith("<!doctype html>") and "const D={" in page and "ff-default" in page
