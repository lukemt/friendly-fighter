"""Honor of Kings 5v5: the example LLM agent's HoK player (examples/llm/hok.py)
and the runner driving it. Payloads mirror Agent_ACP GameAPI's HoK state for a
multi-hero agent (gameapi/src/gameapi/runtime_adapters/hok_adapter.py): seat-
tagged legal actions (``s3:macro_attack``), per-seat summaries, standing orders.
The model is always a scripted fake; nothing reaches OpenAI.
"""

from __future__ import annotations

import json

import pytest

from altruagent import WAIT, DecisionContext, WithReasoning
from altruagent.models import GameState
from altruagent.runner import run_game
from examples.llm import hok
from examples.llm.providers import ProviderError
from examples.llm_agent import LLMAgent
from tests.test_runner import FakeMCPGameSession, result_dict, terminal_state

HOK = DecisionContext(session_id="h", tournament_id=None, game_type="honor_of_kings_5v5", agent_id="a")
SEATS = [0, 1, 2, 3, 4]


class FakeProvider:
    model = "fake-model"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete_structured(self, messages, schema_name, schema):
        self.calls.append({"messages": messages, "schema_name": schema_name, "schema": schema})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _summary(seat, hp=3000, max_hp=3000):
    return {
        "my_hero": {"hero_id": 100 + seat, "camp": 1, "hp": hp, "max_hp": max_hp, "level": 2,
                    "x": -30000 + 1000 * seat, "z": -30000, "kills": 0, "deaths": 0},
        "other_heroes": [{"runtime_id": 7, "hero_id": 140, "camp": 2, "visible": True, "hp": 2000,
                          "max_hp": 3000, "x": -20000, "z": -20000},
                         {"runtime_id": 8, "hero_id": 131, "camp": 2, "visible": False}],
        "structures": [{"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "max_hp": 6000, "x": 10000, "z": 10000},
                       {"camp": 1, "type": "ACTOR_CRYSTAL", "hp": 8000, "max_hp": 8000, "x": -25000, "z": -25000}],
    }


def _entries(seat, macros=("macro_advance", "macro_retreat", "macro_attack"), near=12000):
    out = [{"action_id": f"s{seat}:button_0", "label": f"[seat {seat}] None",
            "input": {"action": {"which_button": 0, "seat": seat}}},
           {"action_id": f"s{seat}:button_2", "label": f"[seat {seat}] Move",
            "input": {"action": {"which_button": 2, "legal_move_x": [4, 5], "legal_move_z": [7], "seat": seat}}}]
    for macro in macros:
        out.append({"action_id": f"s{seat}:{macro}", "label": f"[seat {seat}] {macro} (nearest: x, {near} away)",
                    "input": {"action": {"which_button": 3, "seat": seat}}})
    return out


def hok_payload(version=10, *, mode="realtime", pending=SEATS, standing=None, entries=None, summaries=None):
    entries = entries if entries is not None else [e for s in pending for e in _entries(s)]
    return {
        "session_id": "h", "game_type": "honor_of_kings_5v5", "status": "in_progress", "phase": "moving",
        "is_current_actor": bool(pending), "is_terminal": False, "state_version": version,
        "pacing": {"mode": mode, "step_interval_s": 0.2} if mode == "realtime" else {"mode": "lockstep"},
        "seats": SEATS, "camp": 1, "max_steps": 3000, "seconds_left": 1500,
        "observation": {"seats": {str(s): {"summary": (summaries or {}).get(s, _summary(s))} for s in SEATS}},
        "simultaneous": {"your_pending_seats": list(pending), "standing_orders": standing or {}},
        "legal_actions": {"actions": entries},
    }


def hok_state(*args, **kwargs) -> GameState:
    return GameState.from_mcp_state(hok_payload(*args, **kwargs))


def _plan(**orders):
    return {"orders": [{"seat": int(k[1:]), "macro": v} for k, v in orders.items()],
            "reasoning_summary": "Push mid together."}


def _unwrap(decision):
    return decision.action if isinstance(decision, WithReasoning) else decision


# -- the rules -----------------------------------------------------------------------------


def test_rules_retreat_on_low_hp_fight_in_range_else_push():
    macros = {"macro_advance": "a", "macro_retreat": "r", "macro_attack": "x (nearest: y, 4000 away)"}
    assert hok.rule_macro(_summary(0, hp=500), macros) == "macro_retreat"
    assert hok.rule_macro(_summary(0), macros) == "macro_attack"
    assert hok.rule_macro(_summary(0), {**macros, "macro_skill2": "s (nearest: y, 3000 away)"}) == "macro_skill2"
    assert hok.rule_macro(_summary(0), {**macros, "macro_attack": "x (nearest: y, 20000 away)"}) == "macro_advance"
    assert hok.rule_macro(_summary(0), {}) is None  # a dead hero has no macros


# -- real time -----------------------------------------------------------------------------


def test_realtime_sends_each_changed_order_once_then_waits():
    provider = FakeProvider(_plan(s0="macro_attack", s1="macro_advance", s2="macro_advance",
                                  s3="macro_advance", s4="macro_retreat"))
    player = hok.HokPlayer(provider, log=lambda _: None, clock=Clock())
    standing = {"1": "macro_advance", "2": "macro_advance", "3": "macro_advance"}
    first = player.choose_action(hok_state(standing=standing), HOK)
    assert isinstance(first, WithReasoning) and first.reasoning_summary == "Push mid together."
    assert first.action == {"macro": "macro_attack", "seat": 0}
    # Seat 0 has its order now; only seat 4 still differs from its standing order.
    second = player.choose_action(hok_state(pending=[1, 2, 3, 4], standing={**standing, "0": "macro_attack"}), HOK)
    assert _unwrap(second) == {"macro": "macro_retreat", "seat": 4}
    done = {**standing, "0": "macro_attack", "4": "macro_retreat"}
    assert player.choose_action(hok_state(pending=[1, 2, 3], standing=done), HOK) is WAIT
    assert len(provider.calls) == 1  # one plan for the whole team


def test_the_model_sees_every_hero_with_its_own_options_and_answers_in_a_strict_schema():
    provider = FakeProvider(_plan(s0="macro_attack"))
    player = hok.HokPlayer(provider, log=lambda _: None, clock=Clock())
    player.choose_action(hok_state(), HOK)
    call = provider.calls[0]
    view = json.loads(call["messages"][1]["content"])
    assert [h["seat"] for h in view["heroes"]] == SEATS
    assert set(view["heroes"][0]["options"]) == {"macro_advance", "macro_retreat", "macro_attack"}
    assert view["enemies_in_sight"] == [{"hero_id": 140, "hp": 2000, "max_hp": 3000, "x": -20000, "z": -20000}]
    assert view["your_camp"] == 1 and view["max_steps"] == 3000
    order = call["schema"]["properties"]["orders"]["items"]
    assert order["properties"]["seat"]["enum"] == SEATS and order["additionalProperties"] is False


def test_heroes_the_model_skips_or_misorders_get_the_rules():
    provider = FakeProvider({"orders": [{"seat": 0, "macro": "macro_skill3"}, {"seat": 9, "macro": "macro_attack"}],
                             "reasoning_summary": ""})
    player = hok.HokPlayer(provider, log=lambda _: None, clock=Clock())
    # macro_skill3 isn't offered to seat 0, seat 9 isn't ours: everyone falls back to the
    # rules, which push (no target in range).
    sent = _unwrap(player.choose_action(hok_state(), HOK))
    assert sent == {"macro": "macro_advance", "seat": 0}


def test_provider_error_lets_the_rules_play_and_rests_the_model():
    clock = Clock()
    provider = FakeProvider(ProviderError("HTTP 500"), _plan(s0="macro_retreat"))
    lines: list[str] = []
    player = hok.HokPlayer(provider, log=lines.append, clock=clock, decide_seconds=3)
    assert _unwrap(player.choose_action(hok_state(), HOK)) == {"macro": "macro_advance", "seat": 0}
    clock.now = 4  # a re-plan is due, but the model rests for 10 s
    player.choose_action(hok_state(), HOK)
    assert len(provider.calls) == 1
    clock.now = 11
    assert _unwrap(player.choose_action(hok_state(), HOK)) == {"macro": "macro_retreat", "seat": 0}
    assert player.fallbacks["provider_error"] == 1 and "model unavailable" in lines[0]


def test_without_a_provider_the_rules_play_alone():
    player = hok.HokPlayer(None, log=lambda _: None, clock=Clock())
    assert player.choose_action(hok_state(), HOK) == {"macro": "macro_advance", "seat": 0}


def test_realtime_with_nothing_listed_waits_and_a_dead_hero_is_skipped():
    player = hok.HokPlayer(None, log=lambda _: None, clock=Clock())
    assert player.choose_action(hok_state(pending=[], entries=[]), HOK) is WAIT
    entries = _entries(0, macros=()) + _entries(1)
    assert player.choose_action(hok_state(pending=[0, 1], entries=entries), HOK) == {"macro": "macro_advance", "seat": 1}


def test_a_refused_order_triggers_a_fresh_plan():
    clock = Clock()
    provider = FakeProvider(_plan(s0="macro_attack"), _plan(s0="macro_advance"))
    player = hok.HokPlayer(provider, log=lambda _: None, clock=clock)
    player.choose_action(hok_state(), HOK)
    player.on_action_result({"error": "INVALID_ACTION", "detail": "macro_attack is not available"}, HOK)
    player.choose_action(hok_state(), HOK)
    assert len(provider.calls) == 2


# -- lockstep ------------------------------------------------------------------------------


def test_lockstep_gives_every_pending_hero_an_action_even_on_its_standing_order():
    player = hok.HokPlayer(None, log=lambda _: None, clock=Clock())
    standing = {str(s): "macro_advance" for s in SEATS}
    state = hok_state(mode="lockstep", pending=[2, 3], standing=standing)
    assert player.choose_action(state, HOK) == {"macro": "macro_advance", "seat": 2}


def test_lockstep_dead_hero_plays_the_do_nothing_button():
    player = hok.HokPlayer(None, log=lambda _: None, clock=Clock())
    state = hok_state(mode="lockstep", pending=[0], entries=_entries(0, macros=()))
    assert player.choose_action(state, HOK) == {"which_button": 0, "seat": 0}


def test_llm_agent_hands_hok_to_its_player_in_lockstep_too():
    agent = LLMAgent(FakeProvider(_plan(s0="macro_attack")), log=lambda _: None)
    decision = agent.choose_action(hok_state(mode="lockstep", pending=[0], entries=_entries(0)), HOK)
    assert _unwrap(decision) == {"macro": "macro_attack", "seat": 0}


# -- through the runner ----------------------------------------------------------------------


class HokFakeGame(FakeMCPGameSession):
    def get_game_config(self, game_type) -> dict:
        return {}


def test_runner_plays_a_realtime_hok_match_one_hero_per_call():
    accepted = {"session_id": "h", "status": "in_progress", "state_version": 10, "stepped": False}
    game = HokFakeGame("h")
    # A real-time answer carries no state, so the runner re-reads it after each send.
    for done in range(6):
        standing = {str(s): "macro_advance" for s in range(done)}
        pending = list(range(done, 5))
        game.queue_state(hok_state(10, pending=pending, standing=standing, entries=None if pending else []))
    game.queue_state(terminal_state())
    game.queue_play_action(*[accepted] * 5).queue_result(result_dict())
    agent = LLMAgent(FakeProvider(), log=lambda _: None,
                     realtime_players={"honor_of_kings_5v5": lambda provider, log: hok.HokPlayer(None, log=log)})
    run_game(game, HOK, agent, sleep=lambda s: None)
    sent = [c["action"] for c in game.play_action_calls]
    assert sent == [{"macro": "macro_advance", "seat": s} for s in SEATS]
    assert all(c["action_id"] is None for c in game.play_action_calls)


@pytest.mark.parametrize("answer", [None, [], {"orders": "x"}, {"orders": [{"seat": True, "macro": "macro_attack"}]}])
def test_parse_answer_ignores_malformed_answers(answer):
    assert hok.parse_answer(answer, SEATS) == {}
