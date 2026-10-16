"""bots/jev/v10: the family's opt-in staging settings leave v10 alone by default."""

from __future__ import annotations

from bots.jev.v10.redalert_bot import Knobs, RedAlertBot


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
