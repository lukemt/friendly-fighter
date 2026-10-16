"""friendly-fighter's Honor of Kings bot (bots/hok): the runtime contract, the
safety layer, the team policy, routing, and the runner's team-game handling.

States come from upstream's fixtures (tests/test_hok.py, which mirror GameAPI's
hok_adapter) plus variants: fog, dead heroes, missing or mistyped fields,
lockstep and real time, refused orders, the step and time limits.
"""

from __future__ import annotations

import copy

import pytest

from altruagent import WAIT, DecisionContext, LegalAction, WithReasoning
from altruagent.models import GameState
from altruagent.runner import DecisionError, run_game
from bots.hok import create_agent
from bots.hok.agent import _legal
from bots.hok.policy import Knobs, TeamPolicy
from bots.hok.view import label_distance, label_target, read_view
from tests.test_hok import SEATS, _entries, _summary, hok_payload
from tests.test_runner import FakeMCPGameSession, result_dict, terminal_state

HOK = DecisionContext(session_id="h", tournament_id=None, game_type="honor_of_kings_5v5", agent_id="a")
NO_TYPE = DecisionContext(session_id="h", tournament_id=None, game_type=None, agent_id="a")


def agent(**knobs):
    lines: list[str] = []
    a = create_agent(knobs or None, sleep=lambda s: None, log=lines.append)
    a.lines = lines  # type: ignore[attr-defined]
    return a


def unwrap(decision):
    return decision.action if isinstance(decision, WithReasoning) else decision


def state(payload: dict) -> GameState:
    return GameState.from_mcp_state(payload)


def entries_for(seat, macros, labels=None):
    """Entries with chosen labels: labels maps macro -> label suffix."""
    out = _entries(seat, macros=())
    for m in macros:
        label = (labels or {}).get(m, f"[seat {seat}] {m} (nearest: enemy minion, 3000 away)")
        out.append({"action_id": f"s{seat}:{m}", "label": label, "input": {"action": {"macro": m, "seat": seat}}})
    return out


ALL = ("macro_advance", "macro_retreat", "macro_attack")


# -- labels and parsing -------------------------------------------------------------------------


def test_labels_give_distance_and_target():
    label = "[seat 3] Basic attack (nearest: enemy hero 169, 4123 away)"
    assert label_distance(label) == 4123
    assert label_target(label) == "enemy hero 169"
    assert label_distance("Retreat") == float("inf") and label_target("Retreat") == ""


def test_view_reads_the_fixture():
    view = read_view(state(hok_payload()))
    assert view.camp == 1 and view.seats == SEATS and view.realtime
    assert set(view.offers) == set(SEATS)
    assert [e.hero_id for e in view.visible_enemies()] == [140]
    assert any(not e.visible for e in view.enemies)  # the fogged one is known, without a position
    assert {s.kind for s in view.structures} == {"tower", "crystal"}


def test_view_survives_garbage():
    p = hok_payload()
    p["observation"]["seats"]["0"]["summary"]["my_hero"]["hp"] = "abc"
    p["observation"]["seats"]["1"]["summary"]["other_heroes"] = "garbage"
    p["observation"]["seats"]["2"] = None
    p["observation"]["seats"]["3"]["summary"]["structures"] = [None, 5, {"type": None}]
    p["simultaneous"]["standing_orders"] = {"x": 1, "0": {"macro": "macro_attack"}, "1": "s1:macro_advance"}
    p["seats"] = ["0", 1, None, 2.0, 3, 4]
    view = read_view(state(p))
    assert view.standing == {0: "macro_attack", 1: "macro_advance"}
    assert view.seats == [0, 1, 2, 3, 4]


# -- real time ------------------------------------------------------------------------------------


def test_realtime_sends_one_change_per_call_then_waits():
    a = agent()
    first = unwrap(a.choose_action(state(hok_payload()), HOK))
    assert first == {"macro": "macro_advance", "seat": 0}
    a.on_action_result({"accepted": True}, HOK)
    standing = {"0": "macro_advance"}
    second = unwrap(a.choose_action(state(hok_payload(pending=[1, 2, 3, 4], standing=standing)), HOK))
    assert second == {"macro": "macro_advance", "seat": 1}
    done = {str(s): "macro_advance" for s in SEATS}
    assert a.choose_action(state(hok_payload(standing=done)), HOK) is WAIT


def test_realtime_does_not_resend_an_order_the_server_has_not_reported_yet():
    clock = [0.0]
    a = create_agent(sleep=lambda s: None, log=lambda _l: None, clock=lambda: clock[0])
    assert unwrap(a.choose_action(state(hok_payload(pending=[0], entries=_entries(0))), HOK))["seat"] == 0
    a.on_action_result({"accepted": True}, HOK)
    # The next view still shows no standing order for hero 0 (the report lags a step).
    assert a.choose_action(state(hok_payload(pending=[0], entries=_entries(0))), HOK) is WAIT
    clock[0] = 5.0  # much later and still not reported: send it again
    assert unwrap(a.choose_action(state(hok_payload(pending=[0], entries=_entries(0))), HOK)) == \
        {"macro": "macro_advance", "seat": 0}


def with_enemy_near(summary, dx=3000, hp=3000):
    """The summary with a visible enemy hero ``dx`` from this hero."""
    me = summary["my_hero"]
    summary["other_heroes"] = [{"runtime_id": 77, "hero_id": 177, "camp": 2, "visible": True, "hp": hp,
                                "max_hp": 3000, "x": me["x"] + dx, "z": me["z"]}]
    return summary


def test_realtime_urgent_change_goes_first():
    summaries = {s: _summary(s) for s in SEATS}
    summaries[3] = with_enemy_near(_summary(3, hp=300))  # hero 3 is nearly dead, an enemy at hand
    standing = {str(s): "macro_advance" for s in SEATS}
    a = agent()
    decision = unwrap(a.choose_action(state(hok_payload(standing=standing, summaries=summaries)), HOK))
    assert decision == {"macro": "macro_retreat", "seat": 3}


def test_realtime_waits_with_nothing_listed_and_skips_dead_heroes():
    a = agent()
    assert a.choose_action(state(hok_payload(pending=[], entries=[])), HOK) is WAIT
    entries = _entries(0, macros=()) + _entries(1)
    assert unwrap(a.choose_action(state(hok_payload(pending=[0, 1], entries=entries)), HOK)) == \
        {"macro": "macro_advance", "seat": 1}


def test_realtime_pacing_sleeps_only_when_waiting():
    slept: list[float] = []
    a = create_agent(sleep=slept.append, log=lambda _l: None, pace_s=0.3)
    a.choose_action(state(hok_payload()), HOK)
    assert slept == []
    done = {str(s): "macro_advance" for s in SEATS}
    a.choose_action(state(hok_payload(standing=done)), HOK)  # the first idle look goes at once
    a.choose_action(state(hok_payload(standing=done)), HOK)  # the next one waits out the pace
    assert slept and 0 < slept[0] <= 0.3


# -- lockstep -----------------------------------------------------------------------------------


def test_lockstep_answers_every_pending_hero_even_on_its_standing_order():
    a = agent()
    standing = {str(s): "macro_advance" for s in SEATS}
    st = state(hok_payload(mode="lockstep", pending=[2, 3], standing=standing))
    assert unwrap(a.choose_action(st, HOK)) == {"macro": "macro_advance", "seat": 2}


def test_lockstep_dead_hero_plays_the_do_nothing_button():
    a = agent()
    st = state(hok_payload(mode="lockstep", pending=[0], entries=_entries(0, macros=())))
    decision = unwrap(a.choose_action(st, HOK))
    assert isinstance(decision, LegalAction) and decision.action_id == "s0:button_0"


def test_lockstep_hero_with_only_a_button_with_fields_gets_a_filled_one():
    entries = [e for e in _entries(0, macros=()) if e["action_id"] != "s0:button_0"]
    a = agent()
    decision = unwrap(a.choose_action(state(hok_payload(mode="lockstep", pending=[0], entries=entries)), HOK))
    assert decision == {"which_button": 2, "move_x": 4, "move_z": 7, "seat": 0}


def test_lockstep_does_not_answer_a_seat_twice_in_one_step():
    a = agent()
    p = hok_payload(mode="lockstep", pending=[0, 1])
    assert unwrap(a.choose_action(state(p), HOK))["seat"] == 0
    a.on_action_result({"accepted": True}, HOK)
    # The server still lists seat 0 as pending (an odd answer): move on to seat 1.
    assert unwrap(a.choose_action(state(p), HOK))["seat"] == 1


# -- the safety layer -----------------------------------------------------------------------------


def test_policy_exception_falls_back_to_a_listed_attack():
    a = agent()

    def boom(view):
        raise RuntimeError("bug")

    a.policy.plan = boom  # type: ignore[assignment]
    decision = a.choose_action(state(hok_payload()), HOK)
    assert isinstance(decision, LegalAction) and decision.action_id == "s0:macro_attack"
    assert a.stats["exception"] == 1 and any("exception #1" in line for line in a.lines)


def test_fallback_order_attack_then_advance_then_first_entry():
    a = agent()
    a._decide = lambda st: (_ for _ in ()).throw(ValueError("x"))  # type: ignore[assignment]
    only_advance = _entries(0, macros=("macro_advance",))
    d = a.choose_action(state(hok_payload(pending=[0], entries=only_advance)), HOK)
    assert d.action_id == "s0:macro_advance"
    buttons = _entries(0, macros=())
    d = a.choose_action(state(hok_payload(mode="lockstep", pending=[0], entries=buttons)), HOK)
    assert d.action_id == "s0:button_0"
    assert a.choose_action(state(hok_payload(pending=[], entries=[])), HOK) is WAIT


def test_unlisted_answer_is_replaced():
    a = agent()
    a._decide = lambda st: {"macro": "macro_skill3", "seat": 0}  # type: ignore[assignment]
    d = a.choose_action(state(hok_payload()), HOK)
    assert isinstance(d, LegalAction) and d.action_id == "s0:macro_attack"
    assert a.stats["illegal"] == 1


@pytest.mark.parametrize("decision,ok", [
    ({"macro": "macro_attack", "seat": 0}, True),
    ({"macro": "macro_attack", "seat": 9}, False),             # not our seat / not listed
    ({"macro": "macro_skill1", "seat": 0}, False),             # not on offer
    ({"macro": "macro_attack", "seat": True}, False),
    ({"macro": "macro_attack", "seat": 0, "x": 1}, False),     # extra field
    ({"which_button": 2, "move_x": 4, "move_z": 7, "seat": 0}, True),
    ({"which_button": 2, "move_x": 9, "move_z": 7, "seat": 0}, False),   # not a legal value
    ({"which_button": 2, "move_x": 4, "seat": 0}, False),               # a field missing
    ({"which_button": 0, "seat": 0}, True),
    ("s0:button_0", True), ("s0:button_7", False), (None, False), (3, False),
])
def test_legality_check(decision, ok):
    assert _legal(decision, state(hok_payload())) is ok


def test_refused_order_rests_and_the_next_choice_is_used():
    summaries = {s: _summary(s) for s in SEATS}
    summaries[0] = with_enemy_near(_summary(0, hp=300))
    a = agent()
    st = state(hok_payload(pending=[0], entries=_entries(0), summaries=summaries))
    assert unwrap(a.choose_action(st, HOK)) == {"macro": "macro_retreat", "seat": 0}
    a.on_action_result({"error": "INVALID_ACTION", "detail": "macro_retreat is not available"}, HOK)
    assert unwrap(a.choose_action(st, HOK)) == {"macro": "macro_attack", "seat": 0}
    assert a.stats["refused:INVALID_ACTION"] == 1


def test_repeated_refusals_of_macro_dicts_switch_to_action_ids():
    clock = [0.0]
    a = create_agent(sleep=lambda s: None, log=lambda _l: None, clock=lambda: clock[0])
    for _ in range(3):
        clock[0] += 2.0
        a.choose_action(state(hok_payload(pending=[0], entries=_entries(0))), HOK)
        a.on_action_result({"error": "INVALID_ACTION", "detail": "bad action"}, HOK)
    clock[0] += 2.0
    d = unwrap(a.choose_action(state(hok_payload(pending=[0], entries=_entries(0))), HOK))
    assert isinstance(d, LegalAction) and d.action_id.startswith("s0:macro_")


def test_on_action_result_never_raises():
    a = agent()
    for junk in (None, [], "x", {"error": None}, {"accepted": False}, {"error": "STALE_STATE"}):
        a.on_action_result(junk, HOK)  # type: ignore[arg-type]


# -- the team policy -----------------------------------------------------------------------------


def view_with(summaries=None, entries=None, mode="realtime", **kw):
    return read_view(state(hok_payload(mode=mode, summaries=summaries, entries=entries, **kw)))


def test_low_hero_escapes_only_while_threatened():
    pol = TeamPolicy()
    calm = {s: _summary(s) for s in SEATS}
    for s in SEATS:
        calm[s]["other_heroes"] = []
    calm[0] = _summary(0, hp=500)
    calm[0]["other_heroes"] = []
    # Low but nobody near: home heals ~0.25%/s live, so it keeps playing.
    assert pol.plan(view_with(calm, version=1))[0].base != "macro_retreat"
    hot = dict(calm)
    hot[0] = with_enemy_near(_summary(0, hp=500))
    assert pol.plan(view_with(hot, version=50))[0].base == "macro_retreat"
    # Still escaping 2 s later with the enemy gone, back to work after 3 s of calm.
    assert pol.plan(view_with(calm, version=60))[0].base == "macro_retreat"
    assert pol.plan(view_with(calm, version=70))[0].base != "macro_retreat"


def test_heal_skill_when_low_and_threatened():
    entries = _entries(0) + [{"action_id": "s0:button_11", "label": "[seat 0] HealSkill",
                              "input": {"action": {"which_button": 11, "legal_offset_x": [16, 17, 18, 19, 20],
                                                   "legal_offset_z": [16, 17, 18, 19, 20], "legal_target": [0],
                                                   "seat": 0}}}]
    summaries = {s: _summary(s) for s in SEATS}
    summaries[0] = with_enemy_near(_summary(0, hp=900))
    a = agent()
    d = unwrap(a.choose_action(state(hok_payload(pending=[0], entries=entries, summaries=summaries)), HOK))
    assert d == {"which_button": 11, "offset_x": 18, "offset_z": 18, "target": 0, "seat": 0}
    assert _legal(d, state(hok_payload(pending=[0], entries=entries, summaries=summaries)))


def test_live_shapes_noop_button_and_skill_labels():
    """Live (e5f159ed): the do-nothing button is button_1 "NoOp"; skills read "toward ..."."""
    from bots.hok.view import offers_by_seat
    acts = [LegalAction.from_dict(a) for a in [
        {"action_id": "s0:button_1", "label": "[seat 0] NoOp", "input": {"action": {"which_button": 1, "seat": 0}}},
        {"action_id": "s0:button_9", "label": "[seat 0] Recall",
         "input": {"action": {"which_button": 9, "legal_offset_x": [16], "legal_offset_z": [16],
                              "legal_target": [0], "seat": 0}}},
        {"action_id": "s0:macro_skill1", "label": "[seat 0] Cast skill 1 toward enemy hero 133 (6120 away)",
         "input": {"action": {"macro": "macro_skill1", "seat": 0}}}]]
    offers = offers_by_seat(acts)[0]
    assert offers.hold().base == "button_1"
    assert offers.macros["macro_skill1"].target == "enemy hero 133" and offers.macros["macro_skill1"].dist == 6120
    assert offers.raw_for("heal") is None


def test_retreat_with_hysteresis_when_healing_at_home():
    pol = TeamPolicy(Knobs(heal_at_home=True, retreat_hp=0.3, return_hp=0.85))
    low = {s: _summary(s) for s in SEATS}
    low[0] = _summary(0, hp=600)
    assert pol.plan(view_with(low, version=1))[0].base == "macro_retreat"
    half = dict(low)
    half[0] = _summary(0, hp=1500)
    assert pol.plan(view_with(half, version=2))[0].base == "macro_retreat"  # not yet healed
    full = dict(low)
    full[0] = _summary(0, hp=2900)
    assert pol.plan(view_with(full, version=3))[0].base != "macro_retreat"


def _near_enemy_summaries(enemy_hp=2000, n_enemies=1, our_hp=3000):
    out = {}
    for s in SEATS:
        sm = _summary(s, hp=our_hp)
        sm["other_heroes"] = [{"runtime_id": 70 + i, "hero_id": 140 + i, "camp": 2, "visible": True, "hp": enemy_hp,
                               "max_hp": 3000, "x": -30000 + 1000 * s + 4000, "z": -30000} for i in range(n_enemies)]
        out[s] = sm
    return out


def test_skills_go_on_heroes_first():
    labels = {"macro_skill1": "[seat 0] Skill 1 (nearest: enemy hero 140, 4000 away)",
              "macro_attack": "[seat 0] Basic attack (nearest: enemy hero 140, 4000 away)"}
    ent = entries_for(0, ALL + ("macro_skill1",), labels)
    plan = TeamPolicy().plan(view_with(_near_enemy_summaries(), ent, pending=[0]))
    assert plan[0].base == "macro_skill1"


def test_skill_target_found_by_distance_when_the_label_is_opaque():
    labels = {"macro_skill2": "[seat 0] Skill 2 (nearest: unit 77, 4000 away)",
              "macro_attack": "[seat 0] Basic attack (nearest: unit 77, 4000 away)"}
    ent = entries_for(0, ALL + ("macro_skill2",), labels)
    plan = TeamPolicy().plan(view_with(_near_enemy_summaries(), ent, pending=[0]))
    assert plan[0].base == "macro_skill2"


def test_outnumbered_hero_backs_off():
    summaries = _near_enemy_summaries(n_enemies=5)
    for s in SEATS:
        summaries[s]["structures"] = []  # nowhere near our own tower or crystal
    for s in (1, 2, 3, 4):  # our others are far away
        summaries[s]["my_hero"]["x"] = 40000
    labels = {"macro_attack": "[seat 0] Basic attack (nearest: enemy hero 140, 4000 away)"}
    plan = TeamPolicy().plan(view_with(summaries, entries_for(0, ALL, labels), pending=[0]))
    assert plan[0].base == "macro_retreat" and "outnumbered" in plan[0].reason


def test_no_lone_dive_into_an_enemy_tower():
    summaries = {s: _summary(s) for s in SEATS}
    for s in SEATS:
        summaries[s]["other_heroes"] = []
        summaries[s]["structures"] = [{"camp": 2, "type": "ACTOR_TOWER", "hp": 6000, "max_hp": 6000,
                                       "x": 0, "z": 0}]
    summaries[0]["my_hero"].update(x=5000, z=0)       # hero 0 inside the tower's range, alone
    for s in (1, 2, 3, 4):
        summaries[s]["my_hero"].update(x=-40000, z=-40000)
    labels = {"macro_attack": "[seat 0] Basic attack (nearest: enemy tower, 5000 away)"}
    plan = TeamPolicy().plan(view_with(summaries, entries_for(0, ALL, labels), pending=[0]))
    assert plan[0].base == "macro_retreat"
    # With three heroes there, it hits the tower.
    for s in (1, 2):
        summaries[s]["my_hero"].update(x=7000, z=1000)
    plan = TeamPolicy().plan(view_with(summaries, entries_for(0, ALL, labels), pending=[0]))
    assert plan[0].base == "macro_attack"


def test_hero_far_ahead_waits_for_the_team():
    summaries = {s: _summary(s) for s in SEATS}
    for s in SEATS:
        summaries[s]["other_heroes"] = []
        summaries[s]["my_hero"].update(x=-40000, z=-40000)
    summaries[0]["my_hero"].update(x=-5000, z=-5000)   # far ahead toward the tower at (10000, 10000)
    plan = TeamPolicy().plan(view_with(summaries))
    assert plan[0].base == "hold" and plan[1].base == "macro_advance"


def test_endgame_protects_a_lead_and_goes_all_in_when_behind():
    def summaries(our_tower_hp, their_tower_hp):
        out = {}
        for s in SEATS:
            sm = _summary(s)
            sm["other_heroes"] = []
            sm["structures"] = [
                {"camp": 2, "type": "ACTOR_TOWER", "hp": their_tower_hp, "max_hp": 6000, "x": 10000, "z": 10000},
                {"camp": 1, "type": "ACTOR_TOWER", "hp": our_tower_hp, "max_hp": 6000, "x": -10000, "z": -10000}]
            sm["my_hero"].update(x=2000, z=2000)
            out[s] = sm
        return out
    pol = TeamPolicy()
    pol.plan(view_with(summaries(6000, 6000), version=1))
    late = dict(version=2980)  # 20 steps = 4 s left
    plan = pol.plan(view_with(summaries(6000, 1000), **late))
    assert pol._posture == "protect" and plan[0].base == "macro_retreat"
    pol2 = TeamPolicy()
    pol2.plan(view_with(summaries(6000, 6000), version=1))
    plan = pol2.plan(view_with(summaries(1000, 6000), **late))
    assert pol2._posture == "allin" and plan[0].base in ("macro_advance", "macro_attack")


def test_destroyed_structures_count_once_they_leave_the_list():
    pol = TeamPolicy()
    sm = {s: _summary(s) for s in SEATS}
    pol.observe(view_with(sm, version=1))
    low = copy.deepcopy(sm)
    for s in SEATS:
        low[s]["structures"][0]["hp"] = 500
    pol.observe(view_with(low, version=2))
    gone = copy.deepcopy(sm)
    for s in SEATS:
        gone[s]["structures"] = gone[s]["structures"][1:]
    v = view_with(gone, version=3)
    pol.observe(v)
    assert pol.score(v) == (6000.0, 0.0)


def test_fogged_and_missing_heroes_do_not_break_the_plan():
    summaries = {s: _summary(s) for s in SEATS}
    summaries[2] = {}
    summaries[3]["my_hero"] = {"hp": None}
    for s in SEATS:
        if summaries[s]:
            summaries[s]["other_heroes"] = [{"runtime_id": 9, "camp": 2, "visible": False}]
    plan = TeamPolicy().plan(view_with(summaries))
    assert set(plan) == set(SEATS)


# -- routing ---------------------------------------------------------------------------------------


def test_friendly_fighter_routes_hok_to_the_team_bot(monkeypatch):
    from agent.agent import FriendlyFighter

    ff = FriendlyFighter(record=False, controls=False)
    d = unwrap(ff.choose_action(state(hok_payload()), HOK))
    assert d == {"macro": "macro_advance", "seat": 0}
    assert ff._bot is None  # the Red Alert bot was never built


def test_untyped_realtime_hok_state_never_reaches_red_alert():
    from agent.agent import FriendlyFighter

    ff = FriendlyFighter(record=False, controls=False)
    p = hok_payload()
    p.pop("game_type")
    d = unwrap(ff.choose_action(state(p), NO_TYPE))
    assert d == {"macro": "macro_advance", "seat": 0}
    assert ff._bot is None and ff._hok_bot is not None


def test_untyped_realtime_red_alert_state_still_goes_to_red_alert():
    from agent.agent import FriendlyFighter

    ff = FriendlyFighter(record=False, controls=False)
    ra = {"session_id": "r", "status": "in_progress", "phase": "moving", "is_current_actor": True,
          "is_terminal": False, "state_version": 3, "pacing": {"mode": "realtime"},
          "observation": {"tick": 10, "units": [], "buildings": []}}
    ff.choose_action(state(ra), NO_TYPE)
    assert ff._bot is not None and ff._hok_bot is None


# -- the runner's team-game handling ----------------------------------------------------------------


def test_runner_lets_hok_lockstep_wait_and_recover_a_refusal():
    from altruagent.mcp_transport import MCPToolError

    game = FakeMCPGameSession("h")
    lock = hok_payload(mode="lockstep", pending=[0], entries=_entries(0))
    game.queue_state(state(lock), state(lock), state(lock), terminal_state())
    game.queue_play_action(MCPToolError("INVALID_ACTION: gone", status_code=None, error_code="INVALID_ACTION"),
                           {"accepted": True, "session_id": "h", "state_version": 10})
    game.queue_result(result_dict())
    calls: list = []
    decisions = iter([{"macro": "macro_advance", "seat": 0}, WAIT, {"macro": "macro_advance", "seat": 0}])

    class Agent:
        def choose_action(self, st, ctx):
            return next(decisions)

        def on_action_result(self, result, ctx):
            calls.append(result.get("error"))

    run_game(game, HOK, Agent(), sleep=lambda s: None)
    assert "INVALID_ACTION" in calls  # recovered, reported, and the match went on
    assert game.wait_calls  # WAIT waited instead of raising


def test_runner_still_rejects_wait_in_other_turn_based_games():
    game = FakeMCPGameSession("t")
    game.queue_state(state({"session_id": "t", "status": "in_progress", "phase": "moving",
                            "is_current_actor": True, "is_terminal": False, "state_version": 1,
                            "legal_actions": {"actions": [{"action_id": "0", "label": "x", "input": {}}]}}))
    ctx = DecisionContext(session_id="t", tournament_id=None, game_type="tic_tac_toe", agent_id="a")
    with pytest.raises(DecisionError):
        run_game(game, ctx, lambda s, c: WAIT, sleep=lambda s: None)


# -- the mock driver and the sim ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
@pytest.mark.parametrize("camp", [1, 2])
def test_mock_driver_short_match_is_clean(mode, camp):
    from bots.hok.harness.mock_driver import run_mock

    r = run_mock(camp=camp, mode=mode, opponent="upstream", chaos=0.05, steps=150, seed=3)
    assert r.crashed is None
    assert r.counts.illegal == 0
    assert not any(k.endswith("exception") for k in r.agent_stats)
    assert r.steps == 150 and r.counts.accepted > 0


def test_sim_is_deterministic_and_our_bot_beats_the_placeholder():
    from bots.hok.sim.baselines import make_agent
    from bots.hok.sim.engine import SimConfig
    from bots.hok.sim.match import play_match

    def run():
        specs = {1: "ff-hok", 2: "button0"}
        return play_match(lambda clock, sleep, camp: make_agent(specs[camp], clock, sleep), names=specs,
                          seed=5, mode="realtime", config=SimConfig(max_steps=900))
    a, b = run(), run()
    assert (a.winner, a.structure_damage, a.kills) == (b.winner, b.structure_damage, b.kills)
    assert a.winner == 1 and a.structure_damage[1] > 0
    assert a.sides[1].illegal == 0 and a.sides[1].crashes == 0


def test_knobs_from_env_and_unknown_keys(monkeypatch):
    monkeypatch.setenv("FF_HOK_KNOBS", '{"retreat_hp": 0.4, "nonsense": 1}')
    a = create_agent(sleep=lambda s: None, log=lambda _l: None)
    assert a.policy.knobs.retreat_hp == 0.4
    monkeypatch.setenv("FF_HOK_KNOBS", "not json")
    assert create_agent(sleep=lambda s: None, log=lambda _l: None).policy.knobs == Knobs()


def test_recorded_mock_match_replays_cleanly(tmp_path, monkeypatch, capsys):
    """The live entry point (agent.agent, recorder on) plays a mock match; the replay tool reads it."""
    from agent.agent import FriendlyFighter
    from agent.recorder import Recorder
    from bots.hok.harness import replay
    from bots.hok.harness.mock_driver import SimSession
    from bots.hok.sim.baselines import make_agent
    from bots.hok.sim.engine import Sim, SimConfig

    monkeypatch.setenv("FF_HOK_PACE_S", "0")
    sim = Sim(7, SimConfig(max_steps=120))
    holder = {}
    session = SimSession(sim, 2, "realtime", make_agent("upstream", lambda: holder["s"].vt, lambda d: None),
                         chaos=0.02, seed=7)
    holder["s"] = session
    ff = FriendlyFighter(record=True, controls=False)
    ff._recorder = Recorder(root=tmp_path, enabled=True, bot="ff-default")
    ctx = DecisionContext(session_id="rec", tournament_id=None, game_type="honor_of_kings_5v5", agent_id="a",
                          seat_position=1)
    run_game(session, ctx, ff, sleep=session.sleep, now=session.clock, log=lambda _l: None)
    ff._recorder.close()
    path = tmp_path / "rec" / "seat1.jsonl.gz"
    assert path.exists() and session.counts.illegal == 0
    assert replay.main([str(path), "--show", "0"]) == 0
    out = capsys.readouterr().out
    assert "label shapes" in out and "nearest-shape" in out
    assert " 0 illegal" in out


def test_jungle_monsters_are_left_alone_by_default():
    summaries = {s: _summary(s) for s in SEATS}
    for s in SEATS:
        summaries[s]["other_heroes"] = []
    labels = {"macro_attack": "[seat 0] Basic attack (nearest: neutral monster 7, 3000 away)"}
    plan = TeamPolicy().plan(view_with(summaries, entries_for(0, ALL, labels), pending=[0]))
    assert plan[0].base == "macro_advance"
    plan = TeamPolicy(Knobs(attack_monsters=True)).plan(view_with(summaries, entries_for(0, ALL, labels), pending=[0]))
    assert plan[0].base == "macro_attack"


# -- live payloads (scrubbed from the first live game, e5f159ed) ---------------------------------------


def live_states():
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "data" / "hok_live_states.json").read_text())


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("mode", ["realtime", "lockstep"])
def test_live_state_gets_a_legal_answer(index, mode):
    raw = copy.deepcopy(live_states()[index])
    if mode == "lockstep":
        raw["pacing"] = {"mode": "lockstep"}
    st = state(raw)
    a = agent()
    for _ in range(6):  # several calls on the same state: each answer legal, never an exception
        d = a.choose_action(st, HOK)
        assert d is WAIT or _legal(d, st)
        a.on_action_result({"accepted": True}, HOK)
    assert not any(k.endswith("exception") for k in a.stats) and a.stats.get("illegal", 0) == 0


def test_live_view_reads_the_real_shapes():
    view = read_view(state(live_states()[0]))
    assert view.camp == 1 and view.seats == [0, 1, 2, 3, 4] and view.realtime
    kinds = sorted({(s.camp, s.kind) for s in view.structures})
    assert (1, "spawn") in kinds and (2, "tower") in kinds and (2, "crystal") in kinds
    assert all(o.hold() is not None and o.hold().base == "button_1" for o in view.offers.values())
    dead = [s for s, h in view.heroes.items() if h.dead]
    assert dead and all(view.heroes[s].revive_time for s in dead)
