"""bots/jev/laststand.py: the hiding places, the overrun test and the sale, on small synthetic games."""

from __future__ import annotations

from bots.jev.laststand import LastStandBot, Terrain


def _terrain(width=40, height=12, wall_x=None):
    rows = []
    for y in range(height):
        row = ["."] * width
        if wall_x is not None and y not in (0, 1):
            row[wall_x] = "^"
        rows.append("".join(row))
    return Terrain({"origin": [0, 0], "rows": rows})


def test_distances_go_round_a_wall():
    t = _terrain(wall_x=20)
    d = t.distances((2, 6))
    assert d[(10, 6)] == 8
    assert d[(30, 6)] >= 28  # (through the gap at the top)
    assert (20, 6) not in d


def test_hiding_places_are_far_from_both_bases_and_apart():
    t = _terrain(width=80, height=30)
    places = t.hiding_places((10, 15), (70, 15), count=3)
    assert places, "there are roomy cells far from both"
    for x, y in places:
        assert min(abs(x - 10), abs(x - 70)) >= 20 or abs(y - 15) >= 12
    assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) >= 25 for i, a in enumerate(places) for b in places[i + 1:])


def test_the_settings_are_off_by_default():
    bot = LastStandBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1)
    assert bot.insure_from is None and bot.sell_mode == "off" and bot.sell_dying == 0


def _bot(**kw):
    return LastStandBot(oracle="rules", plan="boom", keep_first=240, early_refineries=1, **kw)


def _building(i, kind="powr", x=10, y=10, hp=100):
    return {"id": i, "type": kind, "x": x, "y": y, "hp": hp}


def test_overrun_needs_enemy_value_and_falling_buildings():
    bot = _bot(sell_mode="overrun")
    home = (10, 10)
    tanks = [{"id": 100 + i, "type": "3tnk", "x": 12, "y": 12} for i in range(4)]  # 4 x 1150
    ours = [{"id": 1, "type": "e1", "x": 10, "y": 10}]
    buildings = [_building(1), _building(2, hp=30)]
    assert bot._overrun(ours, tanks, buildings, home, 900.0)  # a building under fire at 30%
    healthy = [_building(1), _building(2)]
    assert not bot._overrun(ours, tanks, healthy, home, 900.0)  # nothing falling yet
    bot.destroyed_at = [895.0, 899.0]
    assert bot._overrun(ours, tanks, healthy, home, 900.0)  # two buildings gone in the last 25 s
    strong = ours + [{"id": 200 + i, "type": "3tnk", "x": 9, "y": 9} for i in range(4)]
    assert not bot._overrun(strong, tanks, buildings, home, 900.0)  # we are as strong as they are


def test_the_stand_sells_everything_once_a_spare_mcv_lives():
    bot = _bot(sell_mode="overrun", overrun_seconds=0.0)
    bot.stand = True
    obs = {"units": [{"id": 9, "type": "mcv", "x": 50, "y": 50, "idle": True}],
           "buildings": [_building(1), _building(2, "fact"), _building(3, "weap")],
           "military": {"kills_cost": 5000, "deaths_cost": 1000}, "economy": {"cash": 0, "ore": 0},
           "base_center": [10, 10], "visible_enemies": []}
    raw = {"observation": obs, "time": {"game_seconds": 1000.0}, "legal_actions": {"enemy_base": [60, 60]}}
    mine, inner, tags = bot._last_stand(raw, obs, 1000.0, None, [{"cmd": "deploy", "units": [9]}, {"cmd": "train", "item": "e1"}])
    sold = {o["building"] for o in mine if o["cmd"] == "sell"}
    assert sold == {1, 2, 3}
    assert inner == []  # (no deploying the spare MCV, no new production while the base is sold)


def test_without_a_spare_mcv_nothing_is_sold():
    bot = _bot(sell_mode="overrun", overrun_seconds=0.0)
    obs = {"units": [], "buildings": [_building(1), _building(2, hp=20)],
           "military": {"kills_cost": 5000, "deaths_cost": 1000}, "economy": {"cash": 0, "ore": 0},
           "base_center": [10, 10],
           "visible_enemies": [{"id": 100 + i, "type": "3tnk", "x": 12, "y": 12} for i in range(4)]}
    raw = {"observation": obs, "time": {"game_seconds": 1000.0}, "legal_actions": {"enemy_base": [60, 60]}}
    mine, _, _ = bot._last_stand(raw, obs, 1000.0, None, [])
    assert not any(o["cmd"] == "sell" for o in mine)
