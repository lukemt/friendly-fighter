"""bots/jev_bot.py without the network: a fake Jev answers, the rails and the bookkeeping are checked."""

from __future__ import annotations

from altruagent import WithReasoning
from bots.jev_bot import JevBot
from tests.test_friendly_fighter import CTX, cmds, state


class FakeJev:
    def __init__(self, posture="defend", opponent="unknown", production="mixed", p=0.9):
        self.posture, self.opponent, self.production, self.p = posture, opponent, production, p
        self.asked: list[tuple[dict, dict]] = []
        self.last_tokens = 0

    def ask(self, state, questions):
        self.asked.append((state, questions))
        pick = lambda q, v: {"type": "choice", "choice": v, "probabilities": {v: self.p}}  # noqa: E731
        return {"opponent": pick("opponent", self.opponent), "posture": pick("posture", self.posture),
                "production": pick("production", self.production)}


def bot_with(tmp_path, **answers) -> JevBot:
    bot = JevBot(log_dir=str(tmp_path))
    bot.jev = FakeJev(**answers)
    return bot


def army(n, x=14, y=17, first=200):
    return [{"id": first + i, "type": "e1", "x": x, "y": y, "hp": 100, "idle": True} for i in range(n)]


def at(seconds, left=None):
    return {"game_seconds": seconds, "remaining_seconds": (600 - seconds) if left is None else left}


def test_no_key_plays_on_as_the_scripted_bot(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    bot = JevBot(log_dir=str(tmp_path))
    bot.jev.key = None
    answer = bot.choose_action(state(time=at(30)), CTX)
    assert isinstance(answer, WithReasoning) and answer.action["orders"]
    assert bot.bot.knobs.posture == "defend" and bot.bot.knobs.opening == "guard"


def test_the_state_is_words_and_the_call_is_logged(tmp_path):
    bot = bot_with(tmp_path)
    bot.choose_action(state(time=at(200), units=army(12),
                            visible_enemies=[{"id": 900, "type": "3tnk", "x": 18, "y": 18, "hp": 100}]), CTX)
    sent, questions = bot.jev.asked[0]
    assert "opening" in questions  # (asked only in the first 4 minutes)
    assert sent["now_at_our_base"] == "1 enemy units are attacking our base"
    assert "12 combat units (medium)" in sent["our_army"]
    assert list(tmp_path.glob("ra-*.jsonl"))


def test_no_attack_when_not_behind(tmp_path):
    bot = bot_with(tmp_path, posture="commit")
    bot.choose_action(state(time=at(450), units=army(30)), CTX)
    assert bot.posture == "defend" and "not behind enough" in bot.note


def test_attack_when_behind_late_with_a_big_army(tmp_path):
    bot = bot_with(tmp_path, posture="commit")
    s = state(time=at(450), units=army(30), military={"kills_cost": 0, "deaths_cost": 900})
    bot.choose_action(s, CTX)
    assert bot.posture == "commit" and bot.bot.knobs.posture == "attack"
    assert bot.bot.knobs.attack_threshold <= 30


def test_no_attack_with_a_small_army(tmp_path):
    bot = bot_with(tmp_path, posture="commit")
    bot.choose_action(state(time=at(450), units=army(6), military={"kills_cost": 0, "deaths_cost": 900}), CTX)
    assert bot.posture == "gather"


def test_retreat_brings_the_army_home(tmp_path):
    bot = bot_with(tmp_path, posture="retreat")
    bot.posture = "commit"
    bot.posture_since = 0
    s = state(time=at(450), units=army(20, x=80, y=12), military={"kills_cost": 0, "deaths_cost": 900})
    moves = cmds(bot.choose_action(s, CTX).action["orders"], "move")
    assert any(o["to"] == [12, 16] and len(o["units"]) == 20 for o in moves)


def test_a_winning_fight_is_not_abandoned(tmp_path):
    bot = bot_with(tmp_path, posture="defend")
    bot.posture, bot.posture_since = "commit", 0
    enemies = [{"id": 900 + i, "type": "e1", "x": 82, "y": 12, "hp": 100} for i in range(3)]
    bot.choose_action(state(time=at(450), units=army(20, x=80, y=12), visible_enemies=enemies,
                            military={"kills_cost": 500, "deaths_cost": 0}), CTX)
    assert bot.posture == "commit" and "finish the fight" in bot.note


def test_push_holds_a_retreat_while_the_odds_are_good(tmp_path):
    bot = JevBot(log_dir=str(tmp_path), push=True)
    bot.jev = FakeJev(posture="retreat")
    bot.posture, bot.posture_since = "commit", 0
    enemies = [{"id": 900, "type": "e1", "x": 82, "y": 12, "hp": 100}]
    bot.choose_action(state(time=at(450), units=army(20, x=80, y=12), visible_enemies=enemies,
                            military={"kills_cost": 0, "deaths_cost": 900}), CTX)
    assert bot.posture == "commit" and "push on" in bot.note


def test_hold_the_lead_in_the_last_minute(tmp_path):
    bot = bot_with(tmp_path, posture="gather")
    bot.choose_action(state(time=at(560), units=army(30), military={"kills_cost": 900, "deaths_cost": 0}), CTX)
    assert bot.posture == "defend"


def test_a_passive_read_needs_the_facts(tmp_path):
    bot = bot_with(tmp_path, posture="commit", opponent="passive")
    bot.enemy_max_army = 12
    bot.choose_action(state(time=at(450), units=army(20)), CTX)
    assert bot.posture == "defend"


def test_the_scout_goes_and_comes_back(tmp_path):
    bot = bot_with(tmp_path)
    orders = bot.choose_action(state(time=at(110), units=army(10)), CTX).action["orders"]
    scout = [o for o in cmds(orders, "move") if o["to"] == [95, 11]]
    assert scout and bot.scout_id == scout[0]["units"][0]
    units = army(10)
    units[0].update(x=93, y=12)
    s = state(time=at(140), units=units,
              visible_enemy_buildings=[{"id": 700, "type": "tsla", "x": 96, "y": 12, "hp": 100}])
    orders = bot.choose_action(s, CTX).action["orders"]
    assert any(o["units"] == [bot.scout_id] and o["to"] == [12, 16] for o in cmds(orders, "move"))
    assert "tsla" in bot.enemy_defenses


def test_a_forced_posture_holds_without_calls(tmp_path):
    bot = JevBot(log_dir=str(tmp_path), force="commit")
    bot.jev = FakeJev(posture="defend")
    bot.choose_action(state(time=at(200), units=army(5)), CTX)  # (small, early, even: every rail says no)
    assert bot.posture == "commit" and bot.bot.knobs.posture == "attack" and not bot.jev.asked
    retreat = JevBot(log_dir=str(tmp_path), force="retreat")
    retreat.jev = FakeJev()
    retreat.choose_action(state(time=at(200), units=army(5)), CTX)
    assert retreat.posture == "defend"  # (nobody out to pull back)
