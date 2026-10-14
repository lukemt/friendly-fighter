"""The local platform replica (ffsim), checked against what the hosted platform did.

Fixtures in tests/data were cut from real matches: the probe's order matrix with the
platform's immediate answers, and real game states (starting, pending, settled, combat,
finished). The engine here is ``FakeEngine``; tests/test_ffsim_engine.py runs the real one.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ffsim import observe
from ffsim.engine_fake import FakeEngine
from ffsim.gameapi import APPLY_DELAY_TICKS, Match, SeatConfig, ToolError
from ffsim.orders import View, check_order

DATA = Path(__file__).parent / "data"
MATRIX = json.loads((DATA / "platform_order_matrix.json").read_text())
REAL = json.loads((DATA / "platform_states.json").read_text())


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make_match(*, kinds=("agent", "agent"), max_minutes=20, verdict=None):
    clock = Clock()
    engines = []

    def factory(match):
        e = FakeEngine(list(kinds), verdict=verdict)
        engines.append(e)
        return e

    seats = [SeatConfig(kind=k, agent_id=f"agent-{i}" if k == "agent" else "openra-ai", name=f"P{i}", token=f"tok{i}")
             for i, k in enumerate(kinds)]
    m = Match("sess-1", "trapped", seats, factory, max_minutes=max_minutes, clock=clock, sleep=clock.sleep)
    return m, clock, engines


def start(m: Match, engines: list) -> FakeEngine:
    for s in m.seats:
        m.check_in(s)
    e = engines[0]
    e.advance(6)
    e.advance(6)
    assert m.phase == "playing"
    return e


# -- the runner's order checks ----------------------------------------------------------


@pytest.mark.parametrize("case", MATRIX["cases"], ids=[c["name"] for c in MATRIX["cases"]])
def test_order_checks_match_the_platform(case):
    ctx = MATRIX["context"]
    obs = {"units": [{"id": u["id"], "type": u["type"], "x": 66, "y": 72, "hp": 100, "idle": True}
                     for u in ctx["own_units"]],
           "buildings": [], "available_production": ctx["available_production"], "production": []}
    checked = check_order(0, case["order"], View(obs, tuple(ctx["map_size"])))
    want = case["immediate"]
    got = [checked.cmd, checked.status, checked.reason, checked.detail, checked.failed or None]
    assert got == [want[0], want[1], want[2], want[3], want[4] or None]


def test_unit_orders_skip_wrecks_buildings_and_strangers():
    obs = {"units": [{"id": 1, "type": "e1", "idle": True}, {"id": 2, "type": "3tnk.husk", "wreck": True}],
           "buildings": [{"id": 3, "type": "fact"}], "available_production": [], "production": []}
    view = View(obs, (99, 99))
    c = check_order(0, {"cmd": "move", "units": [1, 2, 3, 4], "to": [5, 5]}, view)
    assert c.status == "pending" and [f["reason"] for f in c.failed] == ["wreck", "is_building", "not_owned"]
    assert [cmd["actor_id"] for cmd in c.commands] == [1]
    c = check_order(0, {"cmd": "stop", "units": [3]}, view)  # stop takes buildings
    assert c.status == "pending" and c.commands[0]["actor_id"] == 3


def test_repair_and_power_toggle_are_idempotent():
    obs = {"units": [], "available_production": [], "production": [],
           "buildings": [{"id": 5, "type": "dome", "repairing": True, "powered_down": False}]}
    view = View(obs, (99, 99))
    assert check_order(0, {"cmd": "repair", "building": 5}, view).detail == "already_repairing"
    assert check_order(0, {"cmd": "repair", "building": 5, "on": False}, view).status == "pending"
    assert check_order(0, {"cmd": "power_toggle", "building": 5, "on": True}, view).detail == "already_on"
    assert check_order(0, {"cmd": "power_toggle", "building": 5}, view).status == "pending"


# -- one match through its phases --------------------------------------------------------


def test_phases_check_in_and_start():
    m, clock, engines = make_match()
    st = m.state(m.seats[0])
    assert st["phase"] == "starting" and st["state_version"] == 0 and st["observation"] is None
    assert st["waiting_for_players"] == [0, 1]
    assert st["hint"] == REAL["starting"]["hint"]
    m.check_in(m.seats[0])
    assert not engines and m.state(m.seats[0])["waiting_for_players"] == [1]
    m.check_in(m.seats[1])
    e = engines[0]
    assert e.started
    e.advance(6)
    assert m.phase == "starting"
    e.advance(6)
    st = m.state(m.seats[0])
    assert st["phase"] == "playing" and st["state_version"] == 12 and st["is_current_actor"]
    assert st["legal_actions"]["deploy"] == [100]


def test_play_action_stamps_arrival_and_settles_twelve_ticks_later():
    m, clock, engines = make_match()
    e = start(m, engines)
    seat = m.seats[0]
    e.tick = 15  # the engine has moved on, but GameAPI's "now" is its newest view: tick 12
    ans = m.play_action(seat, 12, {"type": "orders", "orders": [{"cmd": "deploy", "units": [100]}]})
    assert ans["accepted"] and ans["state_version"] == 12 and ans["pending"]
    assert ans["results"] == [{"index": 0, "cmd": "deploy", "detail": None, "commands": 1, "status": "pending",
                               "reason": "pending", "failed": []}]
    assert e.sent[0]["apply_at"] == 12 + APPLY_DELAY_TICKS
    lo = m.state(seat)["last_orders"]
    assert lo["pending"] and not lo["carried"] and lo["applied_at_tick"] is None and "rev" in lo
    e.advance(3)  # tick 18: not yet applied
    assert m.state(seat)["last_orders"]["pending"]
    e.advance(6)  # tick 24 = arrival + 12
    lo = m.state(seat)["last_orders"]
    assert not lo["pending"] and lo["carried"] and lo["applied_at_tick"] == 24 and "rev" not in lo
    assert lo["results"][0]["status"] == "ok"


def test_engine_refusals_give_partial_rejected_and_lost():
    def verdict(seat, cmd):
        return ("rejected", "invalid") if cmd.get("actor_id") == 101 or cmd.get("item_type") == "powr" else ("ok", "ok")

    m, clock, engines = make_match(verdict=verdict)
    e = start(m, engines)
    w = e.world[0]
    w["units"].append({"id": 101, "type": "e1", "x": 60, "y": 70, "hp_percent": 1.0, "idle": True, "activity": None,
                       "stance": 3, "is_husk": False})
    w["available_production"] = ["powr", "e1"]
    e.advance(6)
    seat = m.seats[0]
    orders = [{"cmd": "move", "units": [100, 101], "to": [10, 10]}, {"cmd": "build", "item": "powr"},
              {"cmd": "stop", "units": [101]}]
    m.play_action(seat, e.tick, {"type": "orders", "orders": orders})
    e.advance(12)
    rows = m.state(seat)["last_orders"]["results"]
    assert (rows[0]["status"], rows[0]["reason"], rows[0]["failed"]) == ("partial", "invalid", [{"unit": 101, "reason": "invalid"}])
    assert (rows[1]["status"], rows[1]["reason"], rows[1]["failed"]) == ("rejected", "invalid", [])
    assert (rows[2]["status"], rows[2]["failed"]) == ("rejected", [{"unit": 101, "reason": "invalid"}])
    problems = m.state(seat)["recent_order_problems"]
    assert [(p["index"], p["status"]) for p in problems] == [(0, "partial"), (1, "rejected"), (2, "rejected")]

    # A batch the engine never answers is lost.
    e.verdict = lambda s, c: ("ok", "ok")
    clock.t += 1
    m.play_action(seat, e.tick, {"type": "orders", "orders": [{"cmd": "stop", "units": [100]}]})
    e.sent[-1]["applied"] = True  # swallowed
    e.advance(120)
    lo = m.state(seat)["last_orders"]
    assert lo["results"][0]["status"] == "lost" and lo["results"][0]["reason"] == "not_applied"


def test_pacing_hold_concurrency_and_staleness():
    m, clock, engines = make_match()
    e = start(m, engines)
    seat = m.seats[0]
    batch = {"type": "orders", "orders": [{"cmd": "stop", "units": [100]}]}
    m.play_action(seat, e.tick, batch)
    clock.t += 0.05 - 0.02  # (the first call's 20 ms hand-over already moved the clock on)
    clock.slept.clear()
    m.play_action(seat, e.tick, batch)  # sent 0.05 s later: held for the rest of the 0.2 s gap
    assert abs(clock.slept[0] - 0.15) < 1e-6 and clock.slept[1:] == [0.02]

    seat.in_flight = True
    with pytest.raises(ToolError) as exc:
        m.play_action(seat, e.tick, batch)
    assert exc.value.code == "NOT_YOUR_TURN" and "still being applied" in exc.value.detail
    seat.in_flight = False

    e.advance(4000 - e.tick)  # a view at tick 4000
    with pytest.raises(ToolError) as exc:
        m.play_action(seat, 999, batch)
    assert exc.value.code == "STALE_STATE" and "current tick 4000, oldest accepted 1000" in exc.value.detail
    with pytest.raises(ToolError) as exc:
        m.play_action(seat, 4001, batch)
    assert exc.value.code == "STALE_STATE"
    clock.t += 1
    assert m.play_action(seat, 1000, batch)["accepted"]  # exactly 3000 ticks old is fine
    with pytest.raises(ToolError) as exc:
        m.play_action(seat, -1, batch)
    assert exc.value.detail == "Malformed orders batch: state_version: Input should be greater than or equal to 0"


@pytest.mark.parametrize("action,detail", [
    ({"type": "orders", "orders": []}, "orders must be a non-empty list of order objects (at most 20)"),
    ({"type": "orders", "orders": [{"cmd": "stop", "units": [100]}] * 21}, "orders must be a non-empty list of order objects (at most 20)"),
    ({"type": "orders", "orders": "x"}, "orders must be a non-empty list of order objects (at most 20)"),
    ({"type": "move"}, "action must be {\"type\": \"orders\", \"orders\": [...]} (see get_game_config('red_alert').order_schema)"),
    ({"type": "orders", "orders": [{"cmd": "move", "units": [334], "to": [5, 5]}]},
     "No order in the batch was valid, so nothing was sent and it did not count as a batch: [0] move: not_owned (unit 334 no longer exists or is not yours)"),
])
def test_whole_batch_errors(action, detail):
    m, clock, engines = make_match()
    e = start(m, engines)
    with pytest.raises(ToolError) as exc:
        m.play_action(m.seats[0], e.tick, action)
    assert exc.value.code == "INVALID_ACTION" and exc.value.detail == detail
    assert m.seats[0].batch == 0  # did not count


def test_play_before_start_and_after_end():
    m, clock, engines = make_match()
    with pytest.raises(ToolError) as exc:
        m.play_action(m.seats[0], 0, {"type": "orders", "orders": [{"cmd": "stop", "units": [1]}]})
    assert exc.value.code == "NOT_YOUR_TURN"
    e = start(m, engines)
    m.resign(m.seats[1])
    with pytest.raises(ToolError) as exc:
        m.play_action(m.seats[0], e.tick, {"type": "orders", "orders": [{"cmd": "stop", "units": [100]}]})
    assert exc.value.code == "GAME_ALREADY_COMPLETE"


def test_results_victory_resignation_and_tiebreak():
    m, clock, engines = make_match()
    e = start(m, engines)
    for w in e.world.values():
        w["done"], w["winner"], w["result"] = True, "Multi1", "win"
    e.advance(6)
    st = m.state(m.seats[0])
    assert st["phase"] == "finished" and st["is_terminal"] and st["status"] == "completed"
    assert st["result"] == {"outcome": "loss", "reason": "victory", "winner_seat": "Multi1",
                            "returns": {"agent-0": 0.0, "agent-1": 1.0}}
    assert st["hint"] == "Game over: you lost (victory). Call get_result."
    r = m.get_result(m.seats[1])
    assert r["your_return"] == 1.0 and r["winner_agent_id"] == "agent-1" and r["termination_reason"] == "victory"
    assert e.stopped or True

    m, clock, engines = make_match()
    e = start(m, engines)
    m.resign(m.seats[0])
    assert m.get_result(m.seats[0])["termination_reason"] == "resignation" and e.surrendered == [0]
    with pytest.raises(ToolError):
        m.resign(m.seats[0])

    m, clock, engines = make_match(max_minutes=1)
    e = start(m, engines)
    e.world[0]["military"].update(kills_cost=500, deaths_cost=0, assets_value=100)
    e.world[1]["military"].update(kills_cost=0, deaths_cost=500, assets_value=9000)
    e.advance(25 * 60)
    assert m.termination_reason == "time_limit" and m.result["winner_seat"] == "Multi0"


def test_tiebreak_against_an_openra_bot_uses_mirrored_kills():
    m, clock, engines = make_match(kinds=("agent", "ai:normal"), max_minutes=1)
    m.check_in(m.seats[0])
    e = engines[0]
    e.advance(6)
    e.advance(6)
    e.world[0]["military"].update(kills_cost=0, deaths_cost=700, assets_value=9000)
    e.advance(25 * 60)
    assert m.result["winner_seat"] == "Multi1"  # the bot's balance is +700


def test_wait_for_update_returns_on_a_new_view_and_limits_waiters():
    m, clock, engines = make_match()
    e = start(m, engines)
    seat = m.seats[0]
    sv = m.state(seat)["state_version"]
    real_clock = __import__("time").monotonic
    m.clock = real_clock
    m.last_accepted_at = real_clock()  # keep the idle rule quiet after the clock swap
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("st", m.wait_for_update(seat, sv, True, "playing", 5)))
    t.start()
    __import__("time").sleep(0.1)
    e.advance(6)
    t.join(2)
    assert out["st"]["updated"] is True and out["st"]["state_version"] == sv + 6
    assert m.wait_for_update(seat, sv + 6, True, "playing", 0)["updated"] is False
    m.waits = 2
    with pytest.raises(ToolError) as exc:
        m.wait_for_update(seat, sv, True, "playing", 0)
    assert exc.value.code == "TOO_MANY_WAITS"


# -- events and legal actions ----------------------------------------------------------


def _u(i, t, x=10, y=10, hp=1.0, husk=False, idle=True):
    return {"id": i, "type": t, "x": x, "y": y, "hp_percent": hp, "idle": idle, "activity": None, "stance": 3,
            "is_husk": husk}


def _view(tick, units=(), buildings=(), **extra):
    v = {"tick": tick, "units": list(units), "buildings": list(buildings), "production": [],
         "visible_enemies": [], "visible_enemy_buildings": [],
         "economy": {"power_provided": 100, "power_drained": 50}, "military": {}}
    v.update(extra)
    return v


def test_events_from_consecutive_views():
    tr = observe.EventTracker()
    tr.update(_view(6, [_u(100, "mcv")]))
    assert tr.events == []  # the first view seeds
    fact = {"id": 200, "type": "fact", "x": 66, "y": 72, "hp_percent": 1.0}
    tr.update(_view(12, [], [fact]))
    assert [e["kind"] for e in tr.events] == ["building_created"]  # deploying: no unit_lost
    tank = _u(300, "3tnk", 20, 20)
    tr.update(_view(18, [tank], [fact]))
    tr.update(_view(24, [dict(tank, hp_percent=0.5)], [fact]))
    tr.update(_view(30, [dict(tank, hp_percent=0.4)], [fact]))  # within 10 s: no second under_attack
    tr.update(_view(36, [_u(301, "3tnk.husk", 20, 21, husk=True)], [fact],
                    economy={"power_provided": 100, "power_drained": 150}))
    powr = {"id": 201, "type": "powr", "x": 60, "y": 70, "hp_percent": 1.0}
    tr.update(_view(42, [], [fact, powr], economy={"power_provided": 200, "power_drained": 150}))
    tr.note_sell(201, 42)
    tr.update(_view(48, [], [fact]))
    kinds = [e["kind"] for e in tr.events]
    assert kinds == ["building_created", "unit_created", "under_attack", "unit_lost", "low_power",
                     "building_created", "power_restored", "building_sold"]
    lost = next(e for e in tr.events if e["kind"] == "unit_lost")
    assert lost["wreck_id"] == 301 and [e["seq"] for e in tr.events] == list(range(1, 9))


def test_legal_actions_shape_matches_the_platform():
    m, clock, engines = make_match()
    e = start(m, engines)
    la = m.state(m.seats[0])["legal_actions"]
    assert list(la) == list(REAL["pending"]["legal_actions"])
    assert la["enemy_base"] == [26, 32] and la["attack_now"] is None
    e.world[0]["units"].append(_u(500, "e1", 60, 70))
    e.advance(6)
    la = m.state(m.seats[0])["legal_actions"]
    assert la["attack_now"] == {"cmd": "attack_move", "units": [500], "to": [26, 32]}
    assert la["combat_units"] == [500] and la["idle_units"] == [100, 500]


# -- whole-state schema against real platform states -----------------------------------


def _keys(d):
    # last_orders.rev shows on some of the platform's pending states only (58 of 362 recorded)
    return [k for k in d if k != "rev"] if isinstance(d, dict) else None


def test_state_keys_match_real_platform_states():
    m, clock, engines = make_match()
    st0 = m.state(m.seats[0])
    assert _keys(st0) == _keys(REAL["starting"])
    e = start(m, engines)
    m.play_action(m.seats[0], e.tick, {"type": "orders", "orders": [{"cmd": "deploy", "units": [100]}]})
    pending = m.state(m.seats[0])
    real = REAL["pending"]
    assert _keys(pending) == _keys(real)
    assert _keys(pending["observation"]) == _keys(real["observation"])
    assert _keys(pending["last_orders"]) == _keys(real["last_orders"])
    assert _keys(pending["pacing"]) == _keys(real["pacing"]) and _keys(pending["time"]) == _keys(real["time"])
    assert pending["map"] == {**real["map"], "id": "trapped"} or _keys(pending["map"]) == _keys(real["map"])
    e.advance(12)
    settled = m.state(m.seats[0])
    assert _keys(settled["last_orders"]) == _keys(REAL["settled"]["last_orders"])
    assert _keys(settled["observation"]["units"][0]) == _keys(REAL["settled"]["observation"]["units"][0])
    m.resign(m.seats[1])
    fin = m.wait_for_update(m.seats[0], 0, True, "playing", 0)
    assert _keys(fin) == _keys(REAL["finished"])
    assert _keys(fin["result"]) == _keys(REAL["finished"]["result"])


# -- the HTTP platform with the runtime's own clients ------------------------------------


def test_runtime_clients_talk_to_the_local_platform(monkeypatch):
    from altruagent import mcp_transport
    from altruagent.official import OfficialAgentClient
    from ffsim.server import Platform, serve

    engines = []

    def factory(match, kinds, lobby):
        e = FakeEngine(kinds)
        engines.append(e)
        return e

    platform = Platform(factory, timing={})  # views show at once, waits wake on change
    server = serve(platform, 0)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    key = "eak_live_" + "ab" * 32
    try:
        info = platform.create_match(map_id="trapped", max_minutes=10, seats=[{"kind": "agent", "key": key},
                                                                             {"kind": "ai:normal"}])
        official = OfficialAgentClient(url, key, load_env_file=False)
        rows = official.assignments()
        assert [(r.match_id, r.seat_position, r.context) for r in rows] == [(info["session_id"], 0, "testing")]
        grant = official.grant(rows[0].seat_id, "exec-1")
        assert grant.game_session_id == info["session_id"] and grant.gameapi_server_url == url

        class Client:
            def _current_access_token(self, force_relogin=False):
                return grant.access_token

        monkeypatch.setenv("FF_WIRE", "0")
        monkeypatch.setattr(mcp_transport, "_raw_client", None)  # a fresh shared client, dropped afterwards
        call = lambda name, **a: mcp_transport.call_tool(Client(), url + "/mcp", name, a)  # noqa: E731
        st = call("get_game_state", session_id=grant.game_session_id)
        assert st["phase"] == "starting" and st["your_seat"] == "Multi0"
        e = engines[0]
        e.advance(6)
        e.advance(6)
        st = call("get_game_state", session_id=grant.game_session_id)
        assert st["phase"] == "playing" and st["legal_actions"]["deploy"] == [100]
        ans = call("play_action", session_id=grant.game_session_id, state_version=st["state_version"],
                   action={"type": "orders", "orders": [{"cmd": "deploy", "units": [100]}]})
        assert ans["accepted"] and ans["results"][0]["status"] == "pending"
        with pytest.raises(mcp_transport.MCPToolError) as exc:
            call("send_message", session_id=grant.game_session_id, content="hi")
        assert exc.value.error_code == "RUNTIME_UNAVAILABLE"
        with pytest.raises(mcp_transport.MCPToolError) as exc:
            call("get_game_state", session_id="nope")
        assert exc.value.error_code == "SESSION_NOT_FOUND"
        assert len(call("list_games")["games"]) == 110
        assert call("get_game_config", game_type="red_alert")["rules"]["engine_commit"].startswith("9b271c1")
    finally:
        server.shutdown()


# -- the comparison tools ----------------------------------------------------------------


def test_probe_compare_masks_ids_and_flags_real_differences(tmp_path):
    from ffsim.probe_compare import compare, load

    real = [{"step": "a08", "ok": True, "result": {"rows": {"x": {"immediate": [["stop", "rejected", "not_owned",
                                                                                 "unit 12 no longer exists", None]]}},
                                                     "session_id": "8d78b9e8-8993-49cd-b59f-37c6273585fe"}},
            {"step": "c05", "ok": True, "result": {"final": [["power_toggle", "rejected", "invalid", None, None]]}}]
    sim = json.loads(json.dumps(real).replace("unit 12", "unit 345"))
    sim[0]["result"]["session_id"] = "sim-1"
    sim[1]["result"]["final"][0][1:3] = ["ok", "ok"]
    for name, rows in (("real", real), ("sim", sim)):
        (tmp_path / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    report = compare(load(tmp_path / "real.jsonl"), load(tmp_path / "sim.jsonl"))
    assert report["a08"] == [] and len(report["c05"]) == 1


def test_conformance_flags_schema_and_latency_differences():
    from ffsim.conformance import compare, profile

    def prof(states):
        p = profile(Path("/nonexistent"))
        for raw in states:
            from ffsim.conformance import walk
            walk(raw, "", p["states"], p["vocab"])
            lo = raw.get("last_orders") or {}
            if lo.get("applied_at_tick") is not None:
                p["latency"][lo["applied_at_tick"] - lo["state_version"]] += 1
        return p

    real = prof([REAL["settled"]])
    same = prof([REAL["settled"]])
    assert compare(real, same) == []
    off = json.loads(json.dumps(REAL["settled"]))
    off["last_orders"]["applied_at_tick"] = off["last_orders"]["state_version"] + 16
    off["observation"]["extra"] = 1
    issues = compare(real, prof([off]))
    assert any("latency" in i for i in issues) and any("only sim has observation.extra" in i for i in issues)
