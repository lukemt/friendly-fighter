"""friendly-fighter's Red Alert controller, commander and agent entry point.

States are built from the starter's Red Alert fixture (tests/test_realtime.py);
the clock is fake and no model is ever called.
"""

from __future__ import annotations

import copy
import json

from altruagent import WAIT, DecisionContext, WithReasoning
from altruagent.models import GameState
from agent.agent import FriendlyFighter, create_agent
from agent.commander import Commander, commander_from_env, knobs_from_answer
from agent.redalert_bot import MAX_COMMANDS, MAX_ORDERS, Knobs, RedAlertBot
from tests.test_realtime import CONFIG, ra_payload

CTX = DecisionContext(session_id="ra", tournament_id=None, game_type="red_alert", agent_id="a", game_config=CONFIG)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_bot(**kwargs) -> tuple[RedAlertBot, Clock]:
    clock = Clock()
    return RedAlertBot(log=lambda _: None, clock=clock, **kwargs), clock


def state(time=None, **obs_changes) -> GameState:
    payload = ra_payload()
    if time is not None:
        payload["time"] = time
    payload["observation"] = {**copy.deepcopy(payload["observation"]), **obs_changes}
    return GameState.from_mcp_state(payload)


def orders_of(answer) -> list[dict]:
    assert isinstance(answer, WithReasoning)
    return answer.action["orders"]


def cmds(orders, cmd) -> list[dict]:
    return [o for o in orders if o["cmd"] == cmd]


def test_deploys_the_mcv_first():
    bot, _ = make_bot()
    s = state(units=[{"id": 7, "type": "mcv", "x": 12, "y": 16, "hp": 100, "idle": True}], buildings=[],
              available_production=[])
    orders = orders_of(bot.choose_action(s, CTX))
    assert cmds(orders, "deploy") == [{"cmd": "deploy", "units": [7]}]
    assert not cmds(orders, "build")


def test_builds_power_then_does_not_repeat_while_pending():
    bot, clock = make_bot()
    s = state(economy={"cash": 5000, "power_provided": 0, "power_drained": 0}, units=[])
    first = orders_of(bot.choose_action(s, CTX))
    assert {"cmd": "build", "item": "powr"} in first
    # The next view doesn't show it queued yet: no second build.
    clock.t += 0.3
    again = bot.choose_action(s, CTX)
    assert again is WAIT or not cmds(orders_of(again), "build")


def test_places_ready_buildings():
    bot, _ = make_bot()
    s = state(production=[{"queue": "Building", "item": "powr", "progress": 1, "ready": True}], units=[])
    orders = orders_of(bot.choose_action(s, CTX))
    assert {"cmd": "place", "item": "powr"} in orders
    assert not cmds(orders, "build")  # the building queue is busy until it is placed


def test_low_power_builds_a_power_plant_before_the_build_order():
    bot, _ = make_bot()
    s = state(
        economy={"cash": 5000, "power_provided": 100, "power_drained": 90},
        buildings=[{"id": 1, "type": "fact", "x": 12, "y": 16}, {"id": 2, "type": "powr", "x": 14, "y": 16},
                   {"id": 3, "type": "tent", "x": 16, "y": 16}],
        available_production=["powr", "proc", "tent", "e1"],
        units=[],
    )
    assert {"cmd": "build", "item": "powr"} in orders_of(bot.choose_action(s, CTX))


def test_follows_the_build_order_refinery_before_barracks():
    bot, _ = make_bot()
    s = state(
        economy={"cash": 5000, "power_provided": 200, "power_drained": 20},
        buildings=[{"id": 1, "type": "fact", "x": 12, "y": 16}, {"id": 2, "type": "powr", "x": 14, "y": 16}],
        available_production=["powr", "tent", "proc", "e1"],
        units=[],
    )
    assert {"cmd": "build", "item": "proc"} in orders_of(bot.choose_action(s, CTX))


def test_launches_a_wave_at_the_threshold_and_respects_limits():
    bot, _ = make_bot(knobs=Knobs(attack_threshold=4))
    units = [{"id": 200 + i, "type": "e1", "x": 14, "y": 17, "hp": 100, "idle": True} for i in range(120)]
    s = state(units=units, time={"game_seconds": 400, "remaining_seconds": 800})
    orders = orders_of(bot.choose_action(s, CTX))
    attacks = cmds(orders, "attack_move")
    assert attacks and all(o["to"] == [95, 11] for o in attacks)  # legal_actions.enemy_base
    assert all(len(o["units"]) <= 50 for o in attacks)
    assert len(orders) <= MAX_ORDERS
    assert sum(len(o.get("units", [])) or o.get("count", 1) for o in orders) <= MAX_COMMANDS


def test_small_army_gathers_instead_of_attacking():
    bot, _ = make_bot()
    orders = orders_of(bot.choose_action(state(), CTX))
    assert not cmds(orders, "attack_move")


def test_defends_against_enemies_near_the_base():
    bot, _ = make_bot()
    s = state(visible_enemies=[{"id": 900, "type": "3tnk", "x": 18, "y": 18, "hp": 100}])
    attacks = cmds(orders_of(bot.choose_action(s, CTX)), "attack_move")
    assert attacks and attacks[0]["to"] == [18, 18]
    assert set(attacks[0]["units"]) == {120, 121}


def test_refused_order_is_cooled_down():
    bot, clock = make_bot()
    s = state(economy={"cash": 5000, "power_provided": 0, "power_drained": 0}, units=[],
              available_production=["e1"])  # powr not available: the build is refused
    bot._last_sent = [{"cmd": "build", "item": "powr"}]
    bot.on_action_result({"results": [{"index": 0, "cmd": "build", "status": "rejected", "reason": "not_available"}]}, CTX)
    clock.t += 3.5
    answer = bot.choose_action(s, CTX)
    assert answer is WAIT or {"cmd": "build", "item": "powr"} not in orders_of(answer)


def test_knobs_from_answer_validates():
    current = Knobs()
    knobs = knobs_from_answer({"posture": "defend", "attack_threshold": 99, "unit_focus": "x", "target": "harvesters",
                               "reasoning_summary": "hold"}, current)
    assert knobs.posture == "defend" and knobs.attack_threshold == 30
    assert knobs.unit_focus == current.unit_focus and knobs.target == "harvesters"


def test_commander_step_applies_knobs():
    class FakeProvider:
        model = "fake"

        def complete_structured(self, messages, schema_name, schema, *, timeout=None):
            return {"posture": "eco", "attack_threshold": 12, "unit_focus": "vehicles", "target": "enemy_base",
                    "reasoning_summary": "boom"}

    bot, _ = make_bot()
    bot.choose_action(state(), CTX)
    Commander(bot, FakeProvider(), log=lambda _: None).step()
    assert bot.knobs.posture == "eco" and bot.knobs.attack_threshold == 12


def test_commander_is_off_without_an_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    bot, _ = make_bot()
    assert commander_from_env(bot) is None


def test_create_agent_plays_red_alert_without_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    agent = create_agent()
    assert isinstance(agent, FriendlyFighter)
    answer = agent.choose_action(state(), CTX)
    assert answer is WAIT or isinstance(answer, WithReasoning)


def test_other_games_play_the_first_legal_action_without_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    legal = [{"action_id": f"vote:{i}", "label": f"Vote {i}", "input": {"action_id": f"vote:{i}"}} for i in (1, 2)]
    s = GameState.from_mcp_state({
        "session_id": "w", "status": "in_progress", "phase": "voting", "is_current_actor": True,
        "is_terminal": False, "state_version": 3, "observation": {},
        "legal_actions": {"session_id": "w", "state_version": 3, "actions": legal},
    })
    ctx = DecisionContext(session_id="w", tournament_id=None, game_type="werewolf", agent_id="a")
    assert create_agent().choose_action(s, ctx) == s.legal_actions[0]


def test_first_wave_waits_for_tanks_early_in_the_game():
    bot, _ = make_bot(knobs=Knobs(attack_threshold=4))
    units = [{"id": 200 + i, "type": "e1", "x": 14, "y": 17, "hp": 100, "idle": True} for i in range(10)]
    early = state(units=units, time={"game_seconds": 90, "remaining_seconds": 1110})
    answer = bot.choose_action(early, CTX)
    assert answer is WAIT or not cmds(orders_of(answer), "attack_move")
    tanks = units + [{"id": 300 + i, "type": "2tnk", "x": 14, "y": 17, "hp": 100, "idle": True} for i in range(3)]
    with_tanks = state(units=tanks, time={"game_seconds": 95, "remaining_seconds": 1105})
    assert cmds(orders_of(bot.choose_action(with_tanks, CTX)), "attack_move")


def test_units_wait_while_buildings_are_owed_money():
    bot, _ = make_bot()
    s = state(
        economy={"cash": 1500, "power_provided": 200, "power_drained": 50},
        buildings=[{"id": 1, "type": "fact", "x": 12, "y": 16}, {"id": 2, "type": "powr", "x": 14, "y": 16},
                   {"id": 3, "type": "proc", "x": 16, "y": 16}, {"id": 4, "type": "tent", "x": 18, "y": 16}],
        production=[{"queue": "Building", "item": "weap", "progress": 0.3, "ready": False}],
        available_production=["powr", "e1", "e3"],
        units=[],
    )
    answer = bot.choose_action(s, CTX)
    assert answer is WAIT or not cmds(orders_of(answer), "train")


def test_recorder_writes_config_states_actions_and_results(tmp_path, monkeypatch):
    import gzip
    import json

    from agent.recorder import Recorder

    monkeypatch.setenv("FF_RECORD", "1")
    rec = Recorder(root=tmp_path)
    ctx = DecisionContext(session_id="s/1", tournament_id=None, game_type="red_alert", agent_id="a",
                          seat_position=0, game_config=CONFIG)
    rec.state(state(), ctx)
    rec.action(WithReasoning({"type": "orders", "orders": []}, "x"), ctx)
    rec.result({"accepted": True}, ctx)
    rec.close()
    lines = [json.loads(x) for x in gzip.open(tmp_path / "s_1" / "seat0.jsonl.gz").read().splitlines()]
    assert [r["kind"] for r in lines] == ["config", "state", "action", "result"]
    assert lines[1]["raw"]["observation"]["faction"] == "england"


def test_recorder_is_off_with_ff_record_0(tmp_path):
    from agent.recorder import Recorder

    rec = Recorder(root=tmp_path)
    rec.state(state(), CTX)
    assert not any(tmp_path.iterdir())


def test_remote_control_sends_queued_orders_first_and_logs_them(tmp_path, monkeypatch):
    import json as _json

    from agent import agent as agent_module
    from agent import remote

    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr(agent_module, "read_control", lambda: remote.read_control(tmp_path))
    monkeypatch.setattr(agent_module, "RemoteSeat", lambda bot, seat: remote.RemoteSeat(bot, seat, root=tmp_path))
    monkeypatch.setattr(agent_module, "CONTROL_EVERY_SECONDS", 0.0)  # (normally looked at once a second)
    (tmp_path / "control.json").write_text(_json.dumps({"enabled": True, "seat": 0, "autopilot": True}))
    inbox = tmp_path / "seat0" / "inbox"
    inbox.mkdir(parents=True)
    mine = {"cmd": "attack_move", "units": [120], "to": [50, 20]}
    (inbox / "1.json").write_text(_json.dumps({"orders": [mine], "knobs": {"posture": "defend"}}))

    ctx = DecisionContext(session_id="ra", tournament_id=None, game_type="red_alert", agent_id="a",
                          seat_position=0, game_config=CONFIG)
    ff = create_agent()
    orders = orders_of(ff.choose_action(state(), ctx))
    assert orders[0] == mine and len(orders) > 1          # mine first, autopilot fills the rest
    assert ff._bot.knobs.posture == "defend"
    assert (tmp_path / "seat0" / "done" / "1.json").exists()
    summary = _json.loads((tmp_path / "seat0" / "state.json").read_text())
    assert summary["faction"] == "england" and summary["combat_unit_ids"] == [120, 121]

    ff.on_action_result({"accepted": True, "results": [
        {"index": 0, "cmd": "attack_move", "status": "pending", "reason": "pending"},
        {"index": 1, "cmd": "build", "status": "rejected", "reason": "not_available"}]}, ctx)
    log = [_json.loads(x) for x in (tmp_path / "seat0" / "log.jsonl").read_text().splitlines()]
    assert log[0]["files"] == ["1.json"] and log[0]["result"]["results"][0]["cmd"] == "attack_move"
    # The bot saw only its own row, re-indexed from 0.
    assert any(r.reason == "not_available" for r in ff._bot._refusals.values())

    (tmp_path / "control.json").write_text(_json.dumps({"enabled": True, "seat": 0, "autopilot": False}))
    assert ff.choose_action(state(), ctx) is WAIT           # manual and nothing queued: send nothing


def test_the_control_file_is_read_at_most_once_a_second(monkeypatch):
    from agent import agent as agent_module

    reads = []
    monkeypatch.setattr(agent_module, "read_control", lambda: reads.append(1) or {})
    ctx = DecisionContext(session_id="ra", tournament_id=None, game_type="red_alert", agent_id="a",
                          seat_position=0, game_config=CONFIG)
    ff = create_agent()
    for _ in range(5):
        ff.choose_action(state(), ctx)
    assert len(reads) == 1


# -- spending, reinforcements, focus fire --------------------------------------------------

BASE = [{"id": 1, "type": "fact", "x": 12, "y": 16}, {"id": 2, "type": "powr", "x": 14, "y": 16},
        {"id": 3, "type": "proc", "x": 16, "y": 16}, {"id": 4, "type": "proc", "x": 16, "y": 19},
        {"id": 5, "type": "tent", "x": 18, "y": 16}, {"id": 6, "type": "weap", "x": 18, "y": 19},
        {"id": 7, "type": "fix", "x": 10, "y": 19}, {"id": 8, "type": "dome", "x": 10, "y": 16}]


def test_surplus_money_buys_a_second_factory_and_extra_units():
    bot, _ = make_bot(knobs=Knobs(unit_focus="vehicles"))
    s = state(economy={"cash": 4000, "power_provided": 400, "power_drained": 100}, buildings=BASE,
              available_production=["powr", "weap", "proc", "e1", "e3", "2tnk"], units=[])
    orders = orders_of(bot.choose_action(s, CTX))
    assert {"cmd": "build", "item": "weap"} in orders
    trains = {o["item"]: o["count"] for o in cmds(orders, "train")}
    assert trains.get("2tnk") == 2       # deep pockets: two tanks at once
    assert "e1" in trains                # and infantry despite the vehicle focus


def _wave_bot():
    bot, clock = make_bot(knobs=Knobs(attack_threshold=4))
    stage = bot._staging((12, 16), (95, 11), ra_payload())
    tanks = [{"id": 300 + i, "type": "2tnk", "x": 60, "y": 14, "hp": 100, "idle": False} for i in range(6)]
    t = {"game_seconds": 400, "remaining_seconds": 800}
    bot.choose_action(state(units=tanks, time=t), CTX)  # wave 1 launches with these six
    assert bot._wave_ids == {u["id"] for u in tanks}
    return bot, clock, stage, tanks, t


def test_newcomers_wait_at_the_staging_point_and_join_as_a_group():
    bot, clock, stage, tanks, t = _wave_bot()
    lone = {"id": 400, "type": "2tnk", "x": stage[0], "y": stage[1], "hp": 100, "idle": True}
    clock.t += 10
    orders = orders_of(bot.choose_action(state(units=tanks + [lone], time=t), CTX))
    assert all(400 not in o.get("units", []) for o in cmds(orders, "attack_move"))
    assert 400 not in bot._wave_ids
    group = [dict(lone, id=400 + i) for i in range(4)]
    clock.t += 10
    orders = orders_of(bot.choose_action(state(units=tanks + group, time=t), CTX))
    assert {400, 401, 402, 403} <= bot._wave_ids
    assert any(400 in o.get("units", []) for o in cmds(orders, "attack_move"))


def test_wave_focuses_the_construction_yard_unless_enemy_units_are_near():
    bot, clock, _stage, tanks, t = _wave_bot()
    base = [{"id": 900, "type": "pbox", "x": 62, "y": 14}, {"id": 901, "type": "fact", "x": 66, "y": 12},
            {"id": 902, "type": "brik", "x": 61, "y": 14}]
    clock.t += 10
    orders = orders_of(bot.choose_action(state(units=tanks, time=t, visible_enemy_buildings=base), CTX))
    focus = cmds(orders, "attack")
    assert focus and focus[0]["target"] == 901 and set(focus[0]["units"]) == {u["id"] for u in tanks}
    enemy = [{"id": 950, "type": "3tnk", "x": 63, "y": 15, "hp": 100}]
    clock.t += 10
    orders = orders_of(bot.choose_action(state(units=tanks, time=t, visible_enemy_buildings=base,
                                               visible_enemies=enemy), CTX))
    assert not cmds(orders, "attack") and cmds(orders, "attack_move")[0]["to"] == [63, 15]


# -- probe seat (agent/probe.py) against a fake GameAPI ----------------------------------------

class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class _FakeGame:
    """Just enough of GameAPI for every probe step to run."""

    def __init__(self):
        self.tick = 11
        self.last = None
        self.production = []
        self.calls = []

    def state(self):
        self.tick += 6
        units = [{"id": 100, "type": "mcv", "x": 20, "y": 20, "hp": 100, "idle": True},
                 {"id": 130, "type": "harv", "x": 22, "y": 22, "hp": 100, "idle": False}]
        units += [{"id": 120 + i, "type": "e1", "x": 21 + i, "y": 25, "hp": 100, "idle": True, "stance": "defend"}
                  for i in range(3)]
        buildings = [{"id": 140, "type": "fact", "x": 20, "y": 20}, {"id": 141, "type": "powr", "x": 23, "y": 20},
                     {"id": 142, "type": "barr", "x": 25, "y": 20}]
        return {"state_version": self.tick, "phase": "playing", "map": {"bounds": [1, 1, 80, 80]},
                "observation": {"tick": self.tick, "units": units, "buildings": buildings, "events": [],
                                "production": self.production, "available_production": ["e1", "powr"],
                                "economy": {"cash": 1000}},
                "last_orders": self.last, "recent_order_problems": []}

    def __call__(self, method, params, http=None):
        self.calls.append((method, params))
        if method == "tools/list":
            return {"payload": {"tools": [{"name": "get_game_state"}]}, "dt": 0.1}
        name, args = params["name"], params["arguments"]
        if name in ("get_game_state", "wait_for_update"):
            return {"payload": self.state(), "dt": 0.05}
        if name == "play_action":
            action = args.get("action") or {}
            orders = action.get("orders") if isinstance(action, dict) else None
            if not isinstance(orders, list) or not orders:
                return {"payload": {"error": "INVALID_ACTION", "detail": "bad"}, "dt": 0.05}
            if any(o.get("cmd") == "build" for o in orders if isinstance(o, dict)):
                self.production = [{"queue": "Building", "item": "powr", "ready": True}]
            if any(o.get("cmd") == "place" for o in orders if isinstance(o, dict)):
                self.production = []
            rows = [{"index": i, "cmd": o.get("cmd") if isinstance(o, dict) else None, "status": "pending",
                     "reason": "pending"} for i, o in enumerate(orders)]
            # Like the real server: the batch is stamped with the arrival tick, not the version sent.
            self.last = {"state_version": self.tick, "pending": False, "applied_at_tick": self.tick + 12,
                         "results": [dict(r, status="ok", reason="ok") for r in rows]}
            return {"payload": {"accepted": True, "state_version": self.tick, "results": rows}, "dt": 0.05}
        return {"payload": {"is_terminal": name == "resign"}, "dt": 0.05}


def test_probe_runs_every_step_then_hands_over(tmp_path, monkeypatch):
    import agent.probe as probe_mod

    monkeypatch.setattr(probe_mod, "time", _FakeClock())
    game = _FakeGame()
    bot, _ = make_bot()
    ctx = DecisionContext(session_id="s1", tournament_id=None, game_type="red_alert", agent_id="a",
                          game_config=CONFIG, seat_position=0)
    probe = probe_mod.Probe(bot, ctx, root=tmp_path)
    monkeypatch.setattr(probe, "rpc", game)
    early = state()
    late = GameState.from_mcp_state({**ra_payload(tick=4000), "observation": game.state()["observation"]})
    for _ in range(len(probe.steps) + 3):
        probe.choose_action(late if probe.done >= {s[0] for s in probe.steps if s[0].startswith("a")} else early, ctx)
    records = [json.loads(line) for line in probe.path.read_text().splitlines()]
    failed = {r["step"]: r["result"].get("exception") for r in records if not r["ok"]}
    assert not failed, failed
    assert [r["step"] for r in records] == [s[0] for s in probe.steps] + ["zz_end"]
    assert any(p.get("name") == "resign" for m, p in game.calls if m == "tools/call")
    assert probe.finished
    latency = next(r for r in records if r["step"] == "a04_latency_vs_delay")["result"]["rows"]
    assert all(row["applied_at"] for row in latency)  # verdicts found, no timeouts


def test_console_seat_serves_creds_calls_and_bot_switch(tmp_path, monkeypatch):
    import httpx
    from agent.console import ConsoleSeat
    from altruagent import mcp_transport

    class FakeClient:
        def _current_access_token(self):
            return "tok"

    monkeypatch.setattr(mcp_transport, "last_endpoint", (FakeClient(), "https://example/mcp"))

    class Caller:
        def rpc(self, method, params):
            return {"payload": {"method": method, "params": params}}

    bot, _ = make_bot()
    ctx = DecisionContext(session_id="s1", tournament_id=None, game_type="red_alert", agent_id="a",
                          game_config=CONFIG, seat_position=0)
    seat = ConsoleSeat(bot, ctx, root=tmp_path, port=0, caller=Caller())
    try:
        assert httpx.get(seat.address + "/creds").json()["token"] == "tok"
        answer = httpx.post(seat.address + "/call", json={"tool": "get_game_state"}).json()
        assert answer["payload"]["params"] == {"name": "get_game_state", "arguments": {"session_id": "s1"}}
        assert httpx.post(seat.address + "/bot", json={"on": False}).json() == {"on": False}
        assert seat.choose_action(state(), ctx) is WAIT
        assert json.loads((tmp_path / "console-seat0.json").read_text())["url"] == seat.address
    finally:
        seat.close()
