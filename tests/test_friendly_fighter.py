"""friendly-fighter's Red Alert controller, commander and agent entry point.

States are built from the starter's Red Alert fixture (tests/test_realtime.py);
the clock is fake and no model is ever called.
"""

from __future__ import annotations

import copy

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
