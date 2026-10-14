"""The local platform on the real engine (slow, opt-in: FF_SIM_ENGINE=1, needs the
patched fork built by sim/openra-fork/setup.sh).

One real game, both seats driven through the GameAPI layer (no HTTP, no runtime),
checked against what the hosted platform did in our probe matches: the engine's final
verdicts for the orders the runner passes on, the view clock, deploy timing, placement
and power_toggle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ffsim.engine_openra import ENGINE_DIR, OpenRAEngine
from ffsim.gameapi import Match, SeatConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("FF_SIM_ENGINE") != "1" or not (ENGINE_DIR / "bin" / "OpenRA.dll").exists(),
    reason="set FF_SIM_ENGINE=1 with the patched engine built to run real-engine tests")

MATRIX = json.loads((Path(__file__).parent / "data" / "platform_order_matrix.json").read_text())


def wait_for(cond, timeout=60.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = cond()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


@pytest.fixture(scope="module")
def game(tmp_path_factory):
    out = tmp_path_factory.mktemp("ffsim")
    seats = [SeatConfig("agent", "agent-0", "P0", "t0"), SeatConfig("agent", "agent-1", "P1", "t1")]
    match = Match("sim-test", "trapped", seats,
                  lambda m: OpenRAEngine(m.session_id, m.map_id, ["agent", "agent"], log_dir=out / "engine"),
                  max_minutes=20)
    for s in match.seats:
        match.check_in(s)
    wait_for(lambda: match.phase == "playing", 120)
    yield match
    match.fail("cancelled")
    if match.engine:
        match.engine.stop()


def play(match, seat, orders):
    st = match.state(seat)
    ans = match.play_action(seat, st["state_version"], {"type": "orders", "orders": orders})
    batch = match.seats[seat.position].batch
    wait_for(lambda: (seat.last_orders or {}).get("batch") == batch and not seat.last_orders["pending"], 10)
    return ans, seat.last_orders


def test_view_clock(game):
    seat = game.seats[0]
    a = game.state(seat)["state_version"]
    time.sleep(1.0)
    b = game.state(seat)["state_version"]
    assert a % 6 == 5 and b % 6 == 5 and 18 <= b - a <= 36  # 25 ticks/s, a view every 6 ticks (5, 11, 17...)


def test_engine_verdicts_match_the_platform(game):
    seat = game.seats[1]
    mcv = game.state(seat)["observation"]["units"][0]["id"]
    for case in MATRIX["cases"]:
        if not case["immediate"] or case["immediate"][1] != "pending":
            continue
        order = json.loads(json.dumps(case["order"]).replace("328", str(mcv)))
        ans, lo = play(game, seat, [order])
        got = lo["results"][0]
        want = case["final"]
        assert [got["cmd"], got["status"], got["reason"]] == want[:3], case["name"]
        if want[4]:
            assert [f["reason"] for f in got["failed"]] == [f["reason"] for f in want[4]], case["name"]
        time.sleep(0.25)
    play(game, seat, [{"cmd": "stop", "units": [mcv]}])


def test_deploy_build_place_and_power_toggle(game):
    seat = game.seats[0]
    st = game.state(seat)
    mcv = st["legal_actions"]["deploy"][0]
    ans, lo = play(game, seat, [{"cmd": "deploy", "units": [mcv]}])
    arrival = ans["state_version"]
    assert lo["applied_at_tick"] == arrival + 12 and lo["results"][0]["status"] == "ok"
    fact = wait_for(lambda: next((e for e in game.state(seat)["observation"]["events"]
                                  if e["kind"] == "building_created" and e["type"] == "fact"), None), 10)
    assert fact["tick"] - arrival <= 30  # the platform showed it 24 ticks after arrival
    wait_for(lambda: "powr" in game.state(seat)["observation"]["available_production"], 10)
    play(game, seat, [{"cmd": "build", "item": "powr"}])
    wait_for(lambda: "powr" in game.state(seat)["legal_actions"]["place"], 30)
    ans, lo = play(game, seat, [{"cmd": "place", "item": "powr", "at": [49, 49]}])  # far away: auto-placed
    assert lo["results"][0]["status"] == "ok"
    powr = wait_for(lambda: next((b for b in game.state(seat)["observation"]["buildings"] if b["type"] == "powr"), None), 10)
    assert [powr["x"], powr["y"]] != [49, 49]
    ans, lo = play(game, seat, [{"cmd": "power_toggle", "building": powr["id"], "on": False}])
    assert (lo["results"][0]["status"], lo["results"][0]["reason"]) == ("rejected", "invalid")  # as on the platform
    ans, lo = play(game, seat, [{"cmd": "power_toggle", "building": powr["id"], "on": True}])
    assert lo["results"][0]["detail"] == "already_on"
    ans, lo = play(game, seat, [{"cmd": "set_primary", "building": fact["id"]}])
    assert (lo["results"][0]["status"], lo["results"][0]["reason"]) == ("rejected", "invalid")
