"""bots/jev_council.py without the network: a fake Jev answers each loop's questions."""

from __future__ import annotations

import json

from bots.jev_council import CouncilBot, loop_request
from tests.test_friendly_fighter import CTX, state
from tests.test_jev_bot import army, at


class FakeJev:
    """Answers any question: ``picks`` maps question id -> choice (or a noul probability)."""

    def __init__(self, p=0.9, **picks):
        self.picks, self.p = picks, p
        self.asked: list[tuple[dict, dict]] = []

    def ask_counted(self, state, questions):
        self.asked.append((state, questions))
        answers = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": float(self.picks.get(qid, 0.1))}
                continue
            options = list(q["criteria"])
            pick = self.picks.get(qid, options[0])
            rest = (1 - self.p) / max(1, len(options) - 1)
            answers[qid] = {"type": "choice", "choice": pick, "probabilities": {o: self.p if o == pick else rest for o in options}}
        return answers, 100

    def loops(self):
        return [("strategy" if "plan" in q else "supply" if "production" in q else "field") for _, q in self.asked]


def council(tmp_path, **picks) -> CouncilBot:
    bot = CouncilBot(log_dir=str(tmp_path))
    bot.jev = FakeJev(**picks)
    return bot


def test_each_loop_has_its_own_briefing_sent_once(tmp_path):
    words = {"clock": "6:00 played", "score": "even", "our_army": "12 combat units", "economy": "4 harvesters"}
    for loop in ("strategy", "supply", "field"):
        st, qs = loop_request(loop, words, seconds=360)
        assert isinstance(st["briefing"], list) and st["briefing"]
        text = json.dumps(qs)
        assert "`briefing`" in text and not any(rule in text for rule in st["briefing"])
    _, early = loop_request("supply", words, seconds=100)
    _, late = loop_request("supply", words, seconds=300)
    assert "infantry" in early["production"]["criteria"] and "infantry" not in late["production"]["criteria"]


def test_a_quiet_base_asks_no_field_question(tmp_path):
    bot = council(tmp_path)
    bot.choose_action(state(time=at(200), units=army(10)), CTX)
    assert sorted(bot.jev.loops()) == ["strategy", "supply"]
    assert bot.posture == "defend"


def test_the_plan_becomes_knobs_and_an_attack_forecast_vetoes_greed(tmp_path):
    bot = council(tmp_path, plan="boom", attack_soon=0.1)
    bot.choose_action(state(time=at(200), units=army(10)), CTX)
    assert bot.plan == "boom" and bot.bot.knobs.greed == 2 and "plan=boom" in bot.note
    worried = council(tmp_path, plan="boom", attack_soon=0.8)
    worried.choose_action(state(time=at(200), units=army(10)), CTX)
    assert worried.plan == "hold" and worried.bot.knobs.greed == 0
    fort = council(tmp_path, plan="fortify")
    fort.choose_action(state(time=at(330), units=army(10)), CTX)
    assert fort.bot.knobs.defenses == 4 and fort.bot.knobs.greed == 0


def test_an_unsure_plan_is_not_taken(tmp_path):
    bot = CouncilBot(log_dir=str(tmp_path))
    bot.jev = FakeJev(p=0.4, plan="attack")
    bot.choose_action(state(time=at(450), units=army(30)), CTX)
    assert bot.plan == "hold"


def test_an_attack_plan_wakes_the_field_commander_and_the_rails_still_hold(tmp_path):
    bot = council(tmp_path, plan="attack", posture="commit")
    s = state(time=at(450), units=army(30), military={"kills_cost": 0, "deaths_cost": 900})
    bot.choose_action(s, CTX)
    assert "field" in bot.jev.loops() and bot.posture == "commit" and bot.bot.knobs.posture == "attack"
    even = council(tmp_path, plan="attack", posture="commit")
    even.choose_action(state(time=at(450), units=army(30)), CTX)
    assert even.posture == "defend"  # (not behind: the rail stands whatever the plan)
    plan_text = [st for st, q in even.jev.asked if "posture" in q][0]["current_plan"]
    assert plan_text.startswith("attack")


def test_raids_are_remembered_and_wake_the_strategist(tmp_path):
    bot = council(tmp_path)
    bot.choose_action(state(time=at(100), units=army(10)), CTX)
    n = len(bot.jev.asked)
    raid = state(time=at(104), units=army(10), visible_enemies=[{"id": 900, "type": "e1", "x": 18, "y": 18, "hp": 100}])
    bot.choose_action(raid, CTX)
    assert "strategy" in bot.jev.loops()[n:]
    assert any("came to our base" in e for e in bot.events)
    assert any("first saw enemy e1" in e for e in bot.events)
    bot.last_call = -1e9
    bot.choose_action(state(time=at(130), units=army(10)), CTX)
    assert any("the raid ended" in e for e in bot.events)
    strategy_states = [st for st, q in bot.jev.asked if "plan" in q]
    assert any("came to our base" in " ".join(st["what_happened"]) for st in strategy_states)


def test_the_rules_council_needs_no_jev(tmp_path):
    bot = CouncilBot(log_dir=str(tmp_path), oracle="rules", plan="boom", keep_first=240)
    assert bot.jev.key == "rules"
    bot.choose_action(state(time=at(100), units=army(10)), CTX)
    assert bot.plan == "boom" and bot.bot.knobs.greed == 2
    bot.last_call = -1e9
    bot.choose_action(state(time=at(330), units=army(14)), CTX)  # (5:30, no enemy army ever seen)
    assert bot.plan == "fortify" and bot.production == "rockets" and bot.bot.knobs.greed == 0
    bot.last_call = -1e9
    bot.choose_action(state(time=at(450), units=army(20)), CTX)  # (7:30, nobody came: a standoff)
    assert bot.plan == "boom"  # (the read turns over a few calls: a running belief)
