"""bots/werewolf: the chat reader, the state reader, the belief, the policies, the talk, the safety layer,
the routing from agent/agent.py, the simulator's rules, and the replay tool."""

from __future__ import annotations

import gzip
import json
import random

import pytest

from agent.agent import FriendlyFighter
from agent.recorder import Recorder
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage
from bots.werewolf import agent as ww_agent
from bots.werewolf import create_agent
from bots.werewolf.agent import Unacceptable, WerewolfAgent, check_action, check_message
from bots.werewolf.belief import Belief, Memory, Result
from bots.werewolf.chat import ChatEvent, parse_message, trim
from bots.werewolf.harness import replay as replay_tool
from bots.werewolf.harness.engine import ABSTAIN, SEER, VILLAGER, WOLF, Engine, Rules
from bots.werewolf.harness.players import FirstLegal, RandomBot, RuleBot
from bots.werewolf.harness.run import layout, play
from bots.werewolf.policy import Settings
from bots.werewolf.view import read_snapshot


# -- helpers --------------------------------------------------------------------------------------------

PLAYERS = [{"position": i, "name": f"Player{i}", "agent_id": f"a{i}"} for i in range(7)]


def ctx(seat: int = 2, session: str = "s1") -> DecisionContext:
    return DecisionContext(session, None, "werewolf", f"a{seat}", seat_position=seat)


def raw_state(*, me=2, role="villager", obs_extra="", sub="day_vote", day=1, alive=range(7), dead=(), votes=(),
              legal=None, phase="moving", messages=(), eliminated=False):
    legal = [s for s in alive if s != me] + [7] if legal is None and sub == "day_vote" else (legal or [])
    obs = f"You are Player{me}. Your role: {role}.\n{obs_extra}"
    raw = {
        "session_id": "s1", "game_type": "werewolf", "status": "in_progress", "phase": phase, "is_current_actor": bool(legal),
        "is_terminal": False, "state_version": 5, "observation": obs, "your_position": me, "eliminated": eliminated,
        "players": PLAYERS,
        "game_state": {"phase": sub, "day": day, "alive": [f"Player{i}" for i in alive], "dead": list(dead),
                       "vote_history": list(votes), "winner": None},
        "new_messages": [{"seq": m[0], "sender": m[1], "recipients": list(m[3]) if len(m) > 3 else [], "content": m[2],
                          "type": "chat"} for m in messages],
    }
    if legal:
        raw["legal_actions"] = {"session_id": "s1", "state_version": 5, "actions": [
            {"action_id": str(a), "label": f"x{a}", "input": {"action_id": str(a)}} for a in legal]}
    return raw


def gs(**kw) -> GameState:
    return GameState.from_mcp_state(raw_state(**kw))


def dead(seat, role, cause, day):
    return {"day": day, "player": f"Player{seat}", "role": role, "cause": cause}


def vote_day(day, votes, eliminated=None):
    return {"day": day, "votes": {f"Player{k}": ("abstain" if v == 7 else f"Player{v}") for k, v in votes.items()},
            "eliminated": f"Player{eliminated}" if eliminated is not None else None}


def chosen(move) -> int:
    return int(move.action_id)


# -- the chat reader --------------------------------------------------------------------------------------

def kinds(events):
    return [(e.kind, e.target, e.verdict) for e in events]


def test_seer_claim_with_results():
    ev = parse_message("I'm the seer. I investigated: Player 4 is a wolf. Vote accordingly!", 0)
    assert ("claim", None, None) in kinds(ev)
    assert ("result", 4, "wolf") in kinds(ev)


def test_results_in_several_styles():
    ev = parse_message("Night 1: I checked seat 4 -> villager. Night 2: seat 1 -> WOLF", 6)
    assert ("result", 4, "villager") in kinds(ev) and ("result", 1, "wolf") in kinds(ev)
    ev = parse_message("No, I'm the seer! I checked Player 0 and they are a wolf.", 4)
    assert ("result", 0, "wolf") in kinds(ev)
    ev = parse_message("I am the seer. Player 5 is not a wolf, I checked.", 3)
    assert ("result", 5, "villager") in kinds(ev)


def test_a_wolf_word_after_a_comma_is_not_the_verdict_on_the_seat():
    ev = parse_message("I vote Player4: Player0, who claims seer, checked them as a wolf.", 3)
    assert [(e.kind, e.target) for e in ev] == [("accuse", 4)]


def test_accusations_and_defences():
    assert ("accuse", 1, None) in kinds(parse_message("I suspect Player 1. We should vote for Player 1.", 2))
    assert ("accuse", 3, None) in kinds(parse_message("Player 3 is acting really suspicious", 1))
    assert ("defend", 2, None) in kinds(parse_message("I trust Player 2, they seem fine.", 5))
    assert parse_message("I won't vote for Player 2", 5) == []
    assert parse_message("who is the seer? Player3 claimed seer earlier", 1) == []


def test_not_a_claim():
    assert not any(e.kind == "claim" for e in parse_message("I am not the seer, I'm just a villager", 2))
    assert not any(e.kind == "claim" for e in parse_message("Seer, please claim now.", 2))


def test_nobody_accuses_themselves_and_a_plain_wolf_word_is_an_accusation_not_a_result():
    assert parse_message("Player 2 is a wolf", 2) == []
    ev = parse_message("Player 3 is a wolf", 1)
    assert ("accuse", 3, None) in kinds(ev) and not any(e.kind == "result" for e in ev)


def test_partner_plans_are_read_in_private():
    ev = parse_message("Kill 3 tonight; vote 5 today. I will claim seer.", 1, private=True)
    assert ("plan_kill", 3, None) in kinds(ev) and ("plan_vote", 5, None) in kinds(ev)
    assert any(e.kind == "plan_counterclaim" for e in ev)


def test_trim_keeps_messages_under_the_limit():
    out = trim("word " * 200)
    assert len(out.split()) <= 44 and len(out) <= 400


# -- the state reader --------------------------------------------------------------------------------------

def test_snapshot_reads_role_partner_deaths_and_votes():
    state = gs(me=4, role="wolf", obs_extra="Your fellow wolves: Player1.", sub="night_wolf", day=2, alive=[0, 1, 2, 4, 5, 6],
               dead=[dead(3, "villager", "vote", 1)], votes=[vote_day(1, {0: 3, 1: 3, 2: 3, 3: 0, 4: 3, 5: 6, 6: 5}, 3)],
               legal=[0, 2, 5, 6])
    snap = read_snapshot(state, ctx(4))
    assert (snap.me, snap.role, snap.partner, snap.day, snap.sub_phase) == (4, "wolf", 1, 2, "night_wolf")
    assert snap.alive == {0, 1, 2, 4, 5, 6} and snap.dead[3].role == "villager" and snap.dead[3].cause == "vote"
    assert (1, 1, 3) in snap.votes and snap.lynched[1] == 3
    assert snap.legal == [0, 2, 5, 6]


def test_snapshot_accepts_other_shapes_of_votes_and_deaths():
    raw = raw_state(me=0, role="villager", alive=[0, 1, 2, 3, 4, 5])
    raw["game_state"]["dead"] = [{"day": 1, "name": "Player6", "role": "Werewolf", "cause": "vote"}]
    raw["game_state"]["vote_history"] = {"1": {"votes": [{"voter": "Player0", "target": "Player6"}, ["Player1", "abstain"],
                                               {"player": "Player2", "vote": 7}], "eliminated": "Player6"}}
    snap = read_snapshot(GameState.from_mcp_state(raw), ctx(0))
    assert snap.dead[6].role == "wolf"
    assert sorted(snap.votes) == [(1, 0, 6), (1, 1, None), (1, 2, None)]
    assert snap.lynched == {1: 6}


def test_snapshot_survives_garbage():
    raw = raw_state()
    raw["game_state"] = {"dead": "???", "vote_history": 7, "alive": None, "day": "x"}
    raw["observation"] = None
    snap = read_snapshot(GameState.from_mcp_state(raw), ctx())
    assert snap.role is None and snap.day == 1


# -- the belief ---------------------------------------------------------------------------------------------

def test_revealed_roles_are_hard_facts():
    snap = read_snapshot(gs(me=2, alive=[0, 1, 2, 3, 5, 6], dead=[dead(4, "wolf", "vote", 1)]), ctx())
    bel = Belief(snap, Memory())
    assert bel.p_wolf(4) == pytest.approx(1.0)
    assert sum(bel.p_wolf(s) for s in range(7)) == pytest.approx(2.0)
    assert bel.p_wolf(2) == 0.0  # we are a villager


def test_a_seer_claim_with_a_wolf_result_makes_the_target_the_favourite():
    snap = read_snapshot(gs(me=2), ctx())
    mem = Memory()
    mem.add_events(parse_message("I am the seer. I checked Player 5: wolf.", 0), 1)
    bel = Belief(snap, mem)
    assert bel.p_seer(0) > 0.35 and bel.p_seer(0) > max(bel.p_seer(s) for s in range(1, 7))
    assert max(range(7), key=bel.p_wolf) == 5 and bel.p_wolf(5) > bel.p_wolf(1) * 1.5


def test_a_dead_villager_the_claimed_seer_called_a_wolf_makes_the_claim_a_lie():
    mem = Memory()
    mem.add_events(parse_message("I am the seer. I checked Player 5: wolf.", 0), 1)
    snap = read_snapshot(gs(me=2, alive=[0, 1, 2, 3, 4, 6], dead=[dead(5, "villager", "vote", 1)]), ctx())
    bel = Belief(snap, mem)
    assert bel.p_seer(0) < 0.01 and bel.p_wolf(0) > 0.6


def test_two_seer_claims_cannot_both_be_true():
    mem = Memory()
    mem.add_events(parse_message("I am the seer. Player 3 is a wolf.", 0), 1)
    mem.add_events(parse_message("I'm the seer! I checked Player 0: wolf.", 4), 1)
    bel = Belief(read_snapshot(gs(me=2), ctx()), mem)
    assert bel.p_seer(0) + bel.p_seer(4) > 0.8
    assert bel.p_wolf(0) + bel.p_wolf(4) > 0.9  # at least one is a wolf (a villager rarely claims)


def test_a_wolf_knows_its_partner_and_the_seer_its_own_results():
    snap = read_snapshot(gs(me=4, role="wolf", obs_extra="Your fellow wolves: Player1."), ctx(4))
    bel = Belief(snap, Memory())
    assert bel.p_wolf(1) == pytest.approx(1.0) and bel.p_wolf(4) == pytest.approx(1.0)
    mem = Memory()
    mem.my_checks = {3: WOLF, 5: VILLAGER}
    snap = read_snapshot(gs(me=2, role="seer"), ctx())
    bel = Belief(snap, mem)
    assert bel.p_wolf(3) == pytest.approx(1.0) and bel.p_wolf(5) == 0.0 and bel.p_seer(2) == pytest.approx(1.0)


def test_votes_for_a_revealed_wolf_clear_the_voter_a_little():
    votes = [vote_day(1, {0: 6, 1: 6, 2: 6, 3: 0, 4: 6, 5: 1, 6: 5}, 6)]
    snap = read_snapshot(gs(me=2, alive=[0, 1, 2, 3, 4, 5], dead=[dead(6, "wolf", "vote", 1)], votes=votes), ctx())
    bel = Belief(snap, Memory())
    assert bel.p_wolf(3) > bel.p_wolf(4)  # 3 voted for someone else, 4 joined the lynch
    assert bel.p_wolf(5) > bel.p_wolf(4)  # 5 did not join the lynch of the wolf either


def test_belief_is_fast():
    import time

    mem = Memory()
    mem.add_events(parse_message("I am the seer. I checked Player 5: wolf.", 0), 1)
    votes = [vote_day(1, {i: (i + 1) % 7 for i in range(7)}), vote_day(2, {i: (i + 2) % 7 for i in range(6)})]
    snap = read_snapshot(gs(me=2, day=3, alive=range(6), dead=[dead(6, "villager", "night_kill", 2)], votes=votes), ctx())
    t = time.perf_counter()
    for _ in range(10):
        Belief(snap, mem)
    assert (time.perf_counter() - t) / 10 < 0.05


# -- the policies, through the agent -----------------------------------------------------------------------

def agent(**settings) -> WerewolfAgent:
    return create_agent(settings=settings or None, log=lambda line: None)


def test_villager_votes_the_wolf_a_credible_seer_named_and_never_abstains():
    a = agent()
    state = gs(me=2, messages=[(1, 0, "I am the seer. I investigated Player 5: wolf. Vote Player 5.")])
    assert chosen(a.choose_action(state, ctx())) == 5
    # nothing to go on: still a vote on a seat, not the abstention
    blank = agent().choose_action(gs(me=2), ctx())
    assert chosen(blank) != 7


def test_villager_does_not_vote_itself_and_follows_only_legal_actions():
    for seed in range(10):
        a = agent()
        state = gs(me=3, legal=[0, 1, 2, 4, 5, 7])
        move = a.choose_action(state, ctx(3, f"s{seed}"))
        assert move.action_id in {"0", "1", "2", "4", "5", "7"}


def test_seer_investigates_an_unchecked_suspect_and_votes_the_wolf_it_found():
    a = agent()
    night = gs(me=2, role="seer", sub="night_seer", legal=[0, 1, 3, 4, 5, 6])
    first = chosen(a.choose_action(night, ctx()))
    assert first in (0, 1, 3, 4, 5, 6) and a.mem.pending_check == first
    # the result lands in the next observation
    verdict = f"Your investigation results: Player{first} is a wolf."
    day = gs(me=2, role="seer", obs_extra=verdict, day=1)
    assert chosen(a.choose_action(day, ctx())) == first
    assert a.mem.my_checks == {first: WOLF}
    # next night: a different seat
    night2 = gs(me=2, role="seer", obs_extra=verdict, sub="night_seer", day=2, legal=[0, 1, 3, 4, 5, 6])
    assert chosen(a.choose_action(night2, ctx())) != first


def test_wolf_never_kills_or_votes_its_partner_and_kills_the_claimed_seer():
    a = agent()
    msgs = [(1, 0, "I'm the seer. I checked Player 3: villager.")]
    obs = "Your fellow wolves: Player1."
    day = gs(me=4, role="wolf", obs_extra=obs, day=2, alive=[0, 1, 2, 3, 4, 5, 6], messages=msgs)
    move = a.choose_action(day, ctx(4))
    assert chosen(move) not in (1, 4)
    night = gs(me=4, role="wolf", obs_extra=obs, sub="night_wolf", day=3, alive=[0, 1, 2, 3, 4, 5, 6], legal=[0, 2, 3, 5, 6])
    assert chosen(a.choose_action(night, ctx(4))) == 0  # the claimed seer


def test_the_lower_wolf_decides_the_kill():
    a = agent()
    obs = "Your fellow wolves: Player1."
    a._read(gs(me=4, role="wolf", obs_extra=obs, day=2, messages=[(1, 1, "Kill 6 tonight; vote 2 today.", [4])]), ctx(4))
    assert a.mem.partner_kill and a.mem.partner_vote
    night = gs(me=4, role="wolf", obs_extra=obs, sub="night_wolf", day=3, legal=[0, 2, 3, 5, 6])
    assert chosen(a.choose_action(night, ctx(4))) == 6


def test_a_villager_ignores_private_messages_it_should_not_get():
    a = agent()
    a._read(gs(me=2, messages=[(1, 1, "Kill 6 tonight", [2])]), ctx())
    assert not a.mem.partner_kill


# -- talk ----------------------------------------------------------------------------------------------------

def say_all(a: WerewolfAgent, make_state, context, limit=8):
    out = []
    for _ in range(limit):
        m = a.choose_message(make_state(), context)
        out.append(m)
        if m is TERMINATE_MESSAGING:
            break
    return out


def test_a_discussion_window_ends_with_terminate_within_the_chat_budget():
    a = agent()
    out = say_all(a, lambda: gs(me=2, phase="messaging", sub="day_vote", legal=[]), ctx())
    assert out[-1] is TERMINATE_MESSAGING and len(out) - 1 <= 4
    assert all(isinstance(m, SendMessage) for m in out[:-1])
    # once terminated, always terminated that day
    assert a.choose_message(gs(me=2, phase="messaging", legal=[]), ctx()) is TERMINATE_MESSAGING
    # a new day, a new window
    assert a.choose_message(gs(me=2, phase="messaging", day=2, legal=[]), ctx()) is not TERMINATE_MESSAGING


def test_every_message_is_short_and_has_valid_recipients():
    for role, obs in (("villager", ""), ("seer", ""), ("wolf", "Your fellow wolves: Player1.")):
        a = agent()
        a.mem.my_checks = {5: WOLF} if role == "seer" else {}
        for day in (1, 2, 3):
            for _ in range(6):
                m = a.choose_message(gs(me=4, role=role, obs_extra=obs, phase="messaging", day=day, legal=[],
                                       messages=[(10 * day, 0, "I am the seer. I checked Player 3: wolf.")]), ctx(4))
                if m is TERMINATE_MESSAGING:
                    break
                snap = read_snapshot(gs(me=4, role=role, obs_extra=obs, phase="messaging", day=day, legal=[]), ctx(4))
                check_message(m, snap)
                assert len(m.content.split()) <= 50 and m.recipients in ([], [1])


def test_the_seer_claims_at_once_when_it_found_a_wolf_and_not_before():
    a = agent()
    a.mem.my_checks = {5: VILLAGER}
    a.mem.check_days = {5: 1}
    quiet = a.choose_message(gs(me=2, role="seer", phase="messaging", day=1, legal=[]), ctx())
    assert isinstance(quiet, SendMessage) and "I am the seer" not in quiet.content  # clean result, day 1: no claim
    b = agent()
    b.mem.my_checks = {5: WOLF}
    b.mem.check_days = {5: 1}
    loud = b.choose_message(gs(me=2, role="seer", phase="messaging", day=1, legal=[]), ctx())
    assert "I am the seer" in loud.content and "Player5" in loud.content and b.mem.claim_day == 1
    c = agent()
    c.mem.my_checks = {5: VILLAGER}
    c.mem.check_days = {5: 1}
    late = c.choose_message(gs(me=2, role="seer", phase="messaging", day=2, legal=[]), ctx())
    assert "I am the seer" in late.content  # by day 2 anyway


def test_the_wolves_talk_privately_to_the_partner_only():
    a = agent()
    obs = "Your fellow wolves: Player1."
    m = a.choose_message(gs(me=4, role="wolf", obs_extra=obs, phase="messaging", legal=[]), ctx(4))
    assert isinstance(m, SendMessage) and m.recipients == [1] and "kill" in m.content.lower()
    # the partner is dead: nobody to whisper to
    b = agent()
    m = b.choose_message(gs(me=4, role="wolf", obs_extra=obs, phase="messaging", legal=[], alive=[0, 2, 3, 4, 5, 6],
                           dead=[dead(1, "wolf", "vote", 1)]), ctx(4))
    assert m is TERMINATE_MESSAGING or m.recipients == []


def test_a_wolf_counterclaims_when_the_real_seer_names_it_and_stays_consistent():
    obs = "Your fellow wolves: Player1."
    seer_says = [(1, 0, "I am the seer. I checked Player 1: wolf. Vote Player 1.")]
    a = agent()  # seat 4: the partner (1) was named, so seat 4 is the designated claimant
    out = say_all(a, lambda: gs(me=4, role="wolf", obs_extra=obs, phase="messaging", day=1, legal=[], messages=seer_says), ctx(4))
    texts = [m.content for m in out if isinstance(m, SendMessage) and not m.recipients]
    assert any("real seer" in t and "Player0" in t for t in texts)
    assert a.mem.fake_claim_day == 1 and a.mem.fake_results[0].target == 0
    # next day it does not claim again
    again = say_all(a, lambda: gs(me=4, role="wolf", obs_extra=obs, phase="messaging", day=2, legal=[], messages=[]), ctx(4))
    assert sum("real seer" in m.content for m in again if isinstance(m, SendMessage)) == 0


def test_settings_can_silence_the_bot():
    a = agent(talk=False)
    assert a.choose_message(gs(me=2, phase="messaging", legal=[]), ctx()) is TERMINATE_MESSAGING


def test_a_talker_can_be_swapped_and_cannot_break_the_bot():
    class Loud:
        def next_message(self, c):
            raise RuntimeError("model down")

    a = create_agent(talker=Loud(), log=lambda line: None)
    assert a.choose_message(gs(me=2, phase="messaging", legal=[]), ctx()) is TERMINATE_MESSAGING
    assert a.errors and "model down" in a.errors[-1]

    class TooLong:
        def next_message(self, c):
            from bots.werewolf.talk import Utterance

            return Utterance("word " * 120, act="x")

    b = create_agent(talker=TooLong(), log=lambda line: None)
    m = b.choose_message(gs(me=2, phase="messaging", legal=[]), ctx())
    assert isinstance(m, SendMessage) and len(m.content.split()) <= 50  # trimmed, not refused

    class BadRecipient:
        def next_message(self, c):
            from bots.werewolf.talk import Utterance

            return Utterance("hello", (2,), act="x")  # ourselves

    c = create_agent(talker=BadRecipient(), log=lambda line: None)
    assert c.choose_message(gs(me=2, phase="messaging", legal=[]), ctx()) is TERMINATE_MESSAGING


# -- the safety layer ----------------------------------------------------------------------------------------

def test_check_action_and_message():
    state = gs(me=2, legal=[0, 1, 3, 7])
    check_action(state.legal_actions[0], state)
    with pytest.raises(Unacceptable):
        check_action(LegalAction("5", None, {}), state)
    with pytest.raises(Unacceptable):
        check_action(3, state)
    snap = read_snapshot(state, ctx())
    check_message(SendMessage("hi", []), snap)
    for bad in (SendMessage("", []), SendMessage("a b " * 40, []), SendMessage("x", [2]), SendMessage("x", [0, 1]),
                SendMessage("x", [9]), "hello"):
        with pytest.raises(Unacceptable):
            check_message(bad, snap)


def test_a_crash_in_the_belief_falls_back_to_a_legal_most_suspicious_seat(monkeypatch):
    a = agent()
    a.mem.my_checks = {}
    state = gs(me=2, messages=[(1, 0, "I am the seer. I checked Player 5: wolf.")])
    calls = {"n": 0}
    real = ww_agent.Belief

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kw)

    monkeypatch.setattr(ww_agent, "Belief", flaky)
    move = a.choose_action(state, ctx())
    assert move.action_id == "5"  # the fallback still names the most suspicious seat
    assert a.errors and "boom" in a.errors[0]


def test_when_everything_crashes_the_first_non_abstain_legal_action_is_played(monkeypatch):
    a = agent()

    def boom(*args, **kw):
        raise RuntimeError("no belief at all")

    monkeypatch.setattr(ww_agent, "Belief", boom)
    state = gs(me=2, legal=[7, 0, 1])
    assert a.choose_action(state, ctx()).action_id == "0"
    assert len(a.errors) >= 2


def test_garbage_state_never_raises():
    a = agent()
    raw = raw_state(me=2)
    raw["game_state"] = None
    raw["observation"] = 12
    state = GameState.from_mcp_state(raw)
    assert a.choose_action(state, ctx()).action_id in {s.action_id for s in state.legal_actions}
    assert a.choose_message(state, ctx()) is not None


def test_decisions_are_deterministic_for_a_session():
    def play_once():
        a = agent()
        return [chosen(a.choose_action(gs(me=2, sub=sub, role=role, legal=legal, day=1), ctx(2, "fixed")))
                for sub, role, legal in (("night_seer", "seer", [0, 1, 3, 4, 5, 6]), ("day_vote", "seer", None))]

    assert play_once() == play_once()


# -- routing from agent/agent.py -------------------------------------------------------------------------------

def test_friendly_fighter_routes_werewolf_to_the_bot_and_records_the_discussion(tmp_path):
    ff = FriendlyFighter(record=True, controls=False)
    ff._recorder = Recorder(root=tmp_path, enabled=True, bot="test")
    context = ctx(2, "route")
    move = ff.choose_action(gs(me=2, legal=[0, 1, 3, 7]), context)
    assert isinstance(ff._fallback, WerewolfAgent) and move.action_id in {"0", "1", "3"}
    message = ff.choose_message(gs(me=2, phase="messaging", legal=[]), context)
    assert isinstance(message, SendMessage)
    ff._recorder.close()
    kinds_seen = [json.loads(line)["kind"] for line in gzip.open(tmp_path / "route" / "seat2.jsonl.gz", "rt")]
    assert kinds_seen == ["state", "action", "message_state", "message"]


def test_the_bot_speaks_before_it_has_moved():
    ff = FriendlyFighter(record=False, controls=False)
    assert ff.choose_message(gs(me=2, phase="messaging", legal=[]), ctx(2, "first")) is not None
    assert isinstance(ff._fallback, WerewolfAgent)


def test_other_games_do_not_go_to_the_werewolf_bot():
    ff = FriendlyFighter(record=False, controls=False)
    assert ff.choose_message(gs(me=2, phase="messaging", legal=[]), DecisionContext("x", None, "pokemon_vgc_doubles_draft", "a")) is TERMINATE_MESSAGING
    assert ff._fallback is None


# -- the simulator follows the rules --------------------------------------------------------------------------

class Script:
    """A player that votes/kills by a fixed table of seat -> {sub_phase: choice}."""

    def __init__(self, seat, plan):
        self.seat, self.plan = seat, plan

    def choose_action(self, state, context):
        sub = state.raw["game_state"]["phase"]
        want = self.plan.get(sub, state.legal_actions[0].action_id)
        want = want(state) if callable(want) else want
        return next(a for a in state.legal_actions if a.action_id == str(want))

    def choose_message(self, state, context):
        return TERMINATE_MESSAGING


def engine(plans, roles, rules=None):
    return Engine(1, lambda seat, role, rng: Script(seat, plans.get(seat, {})), roles=roles, rules=rules)


ROLES = [WOLF, WOLF, SEER, VILLAGER, VILLAGER, VILLAGER, VILLAGER]


def test_engine_night_one_is_peaceful_and_a_tie_lynches_nobody():
    plans = {i: {"day_vote": (i + 1) % 7 if (i + 1) % 7 != i else 0} for i in range(7)}  # everyone votes someone else
    e = engine(plans, ROLES)
    e._night()
    assert not e.dead and e.checks[2]
    e._window()
    e._vote()
    assert not e.dead and e.stall == 1 and e.winner is None


def test_engine_plurality_lynch_reveals_the_role():
    plans = {i: {"day_vote": 0} for i in range(1, 7)}
    plans[0] = {"day_vote": 1}
    e = engine(plans, ROLES)
    e._night(), e._window(), e._vote()
    assert e.dead == [{"day": 1, "player": "Player0", "role": WOLF, "cause": "vote"}]
    assert e.stall == 0


def test_engine_three_days_without_a_vote_out_hand_the_wolves_the_win():
    # a real wolf that never kills anyone is not possible, so test the counter directly: three tied days in a row
    e = engine({i: {"day_vote": ABSTAIN} for i in range(7)}, ROLES)
    for expected in (1, 2):
        e._window(), e._vote()
        assert e.stall == expected and e.winner is None
        e.day += 1
    e._window(), e._vote()
    assert (e.winner, e.reason) == (WOLF, "stalling")


def test_a_night_kill_resets_the_stall_counter_only_when_the_rule_says_so():
    for resets, expected in ((False, 1), (True, 0)):
        e = engine({0: {"night_wolf": 6}, 1: {"night_wolf": 6}}, ROLES, Rules(night_kill_resets_stall=resets))
        e.stall, e.day = 1, 2
        e._night()
        assert e.stall == expected


def test_engine_wolves_disagreeing_kill_the_lower_seat():
    plans = {0: {"night_wolf": 6}, 1: {"night_wolf": 4}}
    e = engine(plans, ROLES)
    e._night()  # night 1: peaceful
    e.day = 2
    e._night()
    assert [d["player"] for d in e.dead] == ["Player4"] and e.dead[0]["cause"] == "night_kill"


def test_engine_villagers_win_when_both_wolves_are_dead():
    plans = {i: {"day_vote": 0} for i in range(7)}
    plans[0] = {"day_vote": 1}
    e = engine(plans, ROLES)
    e._night(), e._window(), e._vote()  # wolf 0 out
    e.day = 2
    e.alive.discard(6)  # pretend the night took someone
    e._window()
    plans2 = {i: {"day_vote": 1} for i in range(2, 7)}
    for i in range(2, 7):
        e.players[i] = Script(i, {"day_vote": 1})
    e._vote()
    assert e.winner == VILLAGER and e.reason == "both wolves dead"


def test_engine_wolves_win_at_parity_and_not_before():
    e = engine({}, ROLES)
    for s in (3, 4):
        e.alive.discard(s)
    assert not e._check_win() and e.winner is None  # 2 wolves vs 3 others
    e.alive.discard(5)
    assert e._check_win() and (e.winner, e.reason) == (WOLF, "parity")


def test_engine_chat_limits_and_private_messages():
    sent = []

    class Talker:
        def __init__(self, seat):
            self.seat, self.n = seat, 0

        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            self.n += 1
            if self.seat == 0:
                return SendMessage("hello", [1] if self.n == 1 else [2, 3] if self.n == 2 else [])
            return TERMINATE_MESSAGING

    e = Engine(2, lambda s, r, g: Talker(s), roles=ROLES, trace=sent)
    e._window()
    by_sender = [(m.sender, m.recipients) for m in e.messages]
    assert (0, [1]) in by_sender
    assert not any(len(r) > 1 for _, r in by_sender)  # 2+ recipients refused
    assert len([m for m in e.messages if m.sender == 0]) <= 5


def test_private_messages_reach_only_their_recipient():
    e = Engine(3, lambda s, r, g: FirstLegal(), roles=ROLES)
    from bots.werewolf.harness.engine import Msg

    e.messages.append(Msg(1, 0, [1], "psst", 1))
    assert [m.content for m in e._visible(1)] == ["psst"]
    assert e._visible(2) == [] and [m.content for m in e._visible(0)] == ["psst"]


@pytest.mark.parametrize("kinds_", [["first"] * 7, ["random"] * 7, ["rule"] * 7])
def test_baselines_play_whole_games(kinds_):
    from bots.werewolf.harness.players import make_player

    for seed in range(5):
        e = Engine(seed, lambda s, r, g: make_player(kinds_[s], s, g))
        out = e.play()
        assert out.winner in (WOLF, VILLAGER) and out.days >= 1


def test_our_bot_plays_whole_games_without_errors_in_every_seat_mix():
    for n in (1, 2, 3, 7):
        for seed in range(6):
            ours, kinds = layout(seed, n, ["first", "random", "rule"])
            eng, out, trace, errors = play(seed, ours, kinds, "ours", {}, Rules())
            assert errors == 0 and not trace, (n, seed, trace)
            assert out.winner in (WOLF, VILLAGER)


def test_simulated_games_are_reproducible():
    results = []
    for _ in range(2):
        ours, kinds = layout(77, 2, ["rule"])
        eng, out, _, _ = play(77, ours, kinds, "ours", {}, Rules())
        results.append((out.winner, out.days, [(m.sender, m.content) for m in eng.messages]))
    assert results[0] == results[1]


# -- the replay tool -------------------------------------------------------------------------------------------

def test_replay_of_a_recorded_simulated_game(tmp_path):
    ours, kinds = layout(5, 1, ["rule"])
    seat = ours[0]

    def factory(s, role, rng):
        if s != seat:
            from bots.werewolf.harness.players import make_player

            return make_player(kinds[s], s, rng)
        ff = FriendlyFighter(record=True, controls=False)
        ff._recorder = Recorder(root=tmp_path, enabled=True, bot="test")
        return ff

    eng = Engine(5, factory)
    eng.play()
    eng.players[seat]._recorder.close()
    path = tmp_path / eng.session_id / f"seat{seat}.jsonl.gz"
    lines = []
    result = replay_tool.replay(path, show_audit=True, out=lines.append)
    assert result["errors"] == 0 and result["actions"] > 0 and result["messages"] > 0
    assert result["flags"] == 0, [l for l in lines if "UNKNOWN" in l or "NOT READ" in l]
    assert result["same"] == result["actions"]  # same bot, same views: same moves
