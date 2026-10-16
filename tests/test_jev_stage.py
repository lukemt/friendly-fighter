"""bots/jev/v10: the family's opt-in staging settings leave v10 alone by default."""

from __future__ import annotations

from bots.jev.v10.hands import Knobs, RedAlertBot


def _bot():
    return RedAlertBot(knobs=Knobs(posture="defend"), log=lambda line: None)


def test_defaults_keep_v10():
    k = Knobs()
    assert k.stage_cells == 0.0 and k.regroup is False
    bot = _bot()
    raw = {"map": {"bounds": [1, 1, 97, 97]}}
    assert bot._staging((26, 32), (66, 72), raw) == (34, 40)  # a fifth of the way, as before


def test_stage_cells_puts_the_point_that_far_from_home():
    bot = _bot()
    raw = {"map": {"bounds": [1, 1, 97, 97]}}
    x, y = bot._staging((26, 32), (66, 72), raw, 8.0)
    assert abs(((x - 26) ** 2 + (y - 32) ** 2) ** 0.5 - 8.0) < 1.0
    assert (x - 26) > 0 and (y - 32) > 0  # toward the enemy
    far = bot._staging((26, 32), (66, 72), raw, 500.0)
    assert far == (66, 72)  # never past the enemy base


def test_the_defend_posture_regroups_idle_units_only_when_asked():
    bot = _bot()
    sent = []

    def add(order, note=None):
        sent.append(order)
        return True

    combat = [{"id": 1, "type": "e1", "x": 60, "y": 60, "idle": True}, {"id": 2, "type": "e1", "x": 36, "y": 42, "idle": True},
              {"id": 3, "type": "e1", "x": 70, "y": 70, "idle": False}]
    raw = {"time": {"game_seconds": 800.0}, "observation": {"military": {}}}
    bot._attack(add, combat, raw, (26, 32), (66, 72), (32, 38), Knobs(posture="defend", opening="guard"), 800.0, [])
    assert sent == []
    bot._attack(add, combat, raw, (26, 32), (66, 72), (32, 38), Knobs(posture="defend", opening="guard", regroup=True), 800.0, [])
    assert [(o["cmd"], o["units"], o["to"]) for o in sent] == [("move", [1], [32, 38])]


def test_rifles_focus_trains_only_riflemen_and_more_support():
    bot = RedAlertBot(knobs=Knobs(posture="defend", unit_focus="rifles"), log=lambda line: None)
    sent = []

    def add(order, note=None):
        sent.append(order)
        return True

    available = {"e1", "e3", "3tnk", "v2rl", "tent", "weap"}
    queued = {"Building": 0, "Defense": 0, "Infantry": 0, "Vehicle": 0}
    types = ["fact", "proc", "proc", "weap", "tent", "powr"]
    bot._train_army(add, available, queued, 5000, types, Knobs(posture="defend", unit_focus="rifles"), 600.0)
    infantry = [o for o in sent if o["item"] in ("e1", "e3")]
    assert infantry and all(o["item"] == "e1" for o in infantry)
    # the second vehicle is the support one (every other, not every fourth)
    bot._vehicles_ordered = 1
    sent.clear()
    queued["Vehicle"] = 0
    bot._train_army(add, available, queued, 5000, types, Knobs(posture="defend", unit_focus="rifles"), 600.0)
    assert [o["item"] for o in sent if o["item"] in ("3tnk", "v2rl")] == ["v2rl"]


def test_the_rifle_rule_follows_the_enemy_mix_and_v10_is_the_default():
    from bots.jev.v10.bot import CouncilBot

    def production(bot, now):
        facts, raw = {"threat": False}, {"observation": {"economy": {"cash": 0}}}
        bot._now = (facts, now, raw)
        return bot.jev.production(facts, now, raw)

    v10 = CouncilBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1)
    v10.enemy_seen = {i: ("e1", 590.0) for i in range(10)}
    v10.enemy_seen[99] = ("jeep", 590.0)
    assert production(v10, 600.0) == "rockets"  # (v10: any vehicle seen, rockets from then on)
    blind = CouncilBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1, blind_production="rifles")
    assert production(blind, 400.0) == "rifles"  # (nothing seen by 5:00)
    rule = CouncilBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1, rifle_rule=True)
    rule.enemy_seen = dict(v10.enemy_seen)
    assert production(rule, 600.0) == "rifles"  # 10 of 11 seen units are infantry
    rule.enemy_seen = {i: ("3tnk", 590.0) for i in range(10)}
    assert production(rule, 600.0) == "rockets"
    rule.enemy_seen = {**{i: ("e1", 590.0) for i in range(5)}, **{10 + i: ("3tnk", 590.0) for i in range(5)}}
    assert production(rule, 600.0) == "mixed"
    rule.enemy_seen = {i: ("e1", 100.0) for i in range(10)}  # (not seen for a long time: no share)
    assert production(rule, 600.0) == "rockets"  # (the old rule: nothing seen lately, army never seen, so blind)


def test_focus_fire_gives_each_kind_of_unit_its_own_target():
    bot = RedAlertBot(knobs=Knobs(posture="defend", focus=True), log=lambda line: None)
    sent = []

    def add(order, note=None):
        sent.append(order)
        return True

    combat = [{"id": 1, "type": "e3", "x": 30, "y": 33, "idle": True}, {"id": 2, "type": "e1", "x": 30, "y": 33, "idle": True},
              {"id": 3, "type": "3tnk", "x": 30, "y": 33, "idle": True}, {"id": 4, "type": "v2rl", "x": 30, "y": 33, "idle": True}]
    obs = {"visible_enemies": [{"id": 10, "type": "e1", "x": 34, "y": 34}, {"id": 11, "type": "2tnk", "x": 35, "y": 36},
                               {"id": 12, "type": "heli", "x": 36, "y": 30}, {"id": 13, "type": "harv", "x": 33, "y": 33}]}
    ok = bot._focus_defend(add, combat, {1, 2, 3, 4}, obs, (26, 32), 600.0)
    assert ok
    target_of = {o["units"][0]: o["target"] for o in sent}
    assert target_of == {1: 12, 2: 10, 3: 11, 4: 10}  # rockets the helicopter, riflemen the rifleman, the tank the tank, the V2 the infantry
    assert 13 not in target_of.values()  # (a harvester is not worth a focus order)
    sent.clear()
    assert not bot._focus_defend(add, combat, {1, 2, 3, 4}, {"visible_enemies": []}, (26, 32), 600.0) and not sent


def test_split_jev_shadows_or_decides_the_production(monkeypatch):
    from bots.jev.splitjev import SplitJevBot

    def run(mode, jev_pick):
        bot = SplitJevBot(plan="boom", keep_first=240, early_refineries=1, supply_mode=mode)
        bot.enemy_seen = {i: ("e1", 590.0) for i in range(10)}  # the rules say: rifles
        facts, raw = {"threat": False}, {"observation": {"economy": {"cash": 0}}}
        bot._now = (facts, 600.0, raw)
        state, questions = __import__("bots.jev.v10.bot", fromlist=["loop_request"]).loop_request(
            "supply", {"clock": "10:00", "enemy_mix": "of the 10 enemy combat units seen, 10 are infantry"}, seconds=600.0,
            plan="hold", rifles=True, supply_brief=bot.supply_brief)
        assert "enemy_mix" in state and "rifles" in questions["production"]["criteria"]
        reply = {"production": {"type": "choice", "choice": jev_pick, "probabilities": {jev_pick: 0.9}}}
        monkeypatch.setattr(bot.supply_jev, "ask_counted", lambda s, q: (reply, 500))
        answers, tokens = bot._ask(state, questions)
        return bot, answers["production"]["choice"], tokens

    bot, pick, tokens = run("shadow", "rockets")
    assert pick == "rifles" and tokens == 500 and bot.agreement() == (1, 0)  # rules decide; Jev is only logged
    bot, pick, _ = run("jev", "rockets")
    assert pick == "rockets"  # Jev decides
    bot, pick, _ = run("jev", "rifles")
    assert pick == "rifles" and bot.agreement() == (1, 1)


def test_snipe_sends_everyone_at_tanya_and_the_tanya_rule_switches_to_tanks():
    from bots.jev.v10.bot import CouncilBot

    bot = RedAlertBot(knobs=Knobs(posture="defend", snipe=True), log=lambda line: None)
    sent = []

    def add(order, note=None):
        sent.append(order)
        return True

    combat = [{"id": i, "type": t, "x": 30, "y": 36, "idle": True} for i, t in enumerate(["e1", "e3", "3tnk", "v2rl"], start=1)]
    obs = {"visible_enemies": [{"id": 50, "type": "e1", "x": 34, "y": 38}, {"id": 51, "type": "e7", "x": 36, "y": 40}]}
    assert bot._snipe_defend(add, combat, {1, 2, 3, 4}, obs, (26, 32), 800.0)
    assert [(o["cmd"], sorted(o["units"]), o["target"]) for o in sent] == [("attack", [1, 2, 3, 4], 51)]
    sent.clear()
    assert not bot._snipe_defend(add, combat, {1, 2, 3, 4}, {"visible_enemies": [{"id": 50, "type": "e1", "x": 34, "y": 38}]}, (26, 32), 800.0)

    def production(rule):
        b = CouncilBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1, tanya_rule=rule)
        b.enemy_seen = {1: ("e7", 790.0), 2: ("e1", 790.0)}
        facts, raw = {"threat": False}, {"observation": {"economy": {"cash": 0}}}
        b._now = (facts, 800.0, raw)
        return b.jev.production(facts, 800.0, raw)

    assert production(True) == "vehicles" and production(False) == "rockets"  # (v10: blind-or-vehicles rule)


def test_greed_hold_keeps_the_economy_growing_outside_boom_and_fortify():
    from bots.jev.v10.bot import CouncilBot

    v10 = CouncilBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1)
    v10.plan = "hold"
    v10._knobs_for_plan()
    assert v10.greed == 0  # v10: a plan other than boom is no greed
    g2 = CouncilBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1, greed_hold=2)
    for plan, greed, defenses in (("boom", 2, 2), ("hold", 2, 2), ("fortify", 0, 4), ("attack", 2, 2)):
        g2.plan = plan
        g2._knobs_for_plan()
        assert (g2.greed, g2.defenses) == (greed, defenses), plan


def test_macro_wants_more_refineries_on_a_schedule_and_harvesters_follow():
    bot = RedAlertBot(knobs=Knobs(posture="defend", macro=6), log=lambda line: None)
    available = {"powr", "proc", "tent", "weap", "fix", "dome", "apwr", "pbox"}
    economy = {"power_provided": 500, "power_drained": 100}
    types = ["fact", "powr", "powr", "tent", "weap", "fix", "dome"] + ["proc"] * 3

    def pick(now, macro, n_proc=3):
        bot._game_seconds = now
        tt = ["fact", "powr", "powr", "tent", "weap", "fix", "dome"] + ["proc"] * n_proc
        return bot._next_building(tt, available, economy, [], 0, Knobs(posture="defend", macro=macro, early_refineries=1))

    assert pick(200.0, 6) != "proc"        # before 5:00 no macro refinery
    assert pick(310.0, 6, n_proc=3) == "proc"   # 4 by 5:00
    assert pick(310.0, 6, n_proc=4) != "proc"
    assert pick(500.0, 6, n_proc=5) == "proc"   # 6 by 8:00
    assert pick(500.0, 6, n_proc=6) != "proc"   # capped at macro=6
    assert pick(310.0, 0, n_proc=3) != "proc"   # v10: not asked


def test_defend_radius_widens_what_counts_as_a_threat():
    bot = RedAlertBot(knobs=Knobs(posture="defend"), log=lambda line: None)
    obs = {"visible_enemies": [{"id": 1, "type": "e1", "x": 26, "y": 52}, {"id": 2, "type": "e1", "x": 26, "y": 40}]}
    assert bot._threats(obs, (26, 32)) == [(26, 40)]  # 16: the one at 20 cells is no threat
    assert sorted(bot._threats(obs, (26, 32), 22)) == [(26, 40), (26, 52)]
    assert Knobs().defend_radius == 16
