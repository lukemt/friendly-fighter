"""The rush-wave family's copy of v10 (bots/rush_wave/v10): its settings are off by default."""

from __future__ import annotations

from bots.rush_wave.v10.bot import CouncilBot
from bots.rush_wave.v10.redalert_bot import Knobs, RedAlertBot
from tests.test_friendly_fighter import BASE, CTX, Clock, cmds, orders_of, state

RICH = {"cash": 4000, "power_provided": 400, "power_drained": 100}
AVAILABLE = ["powr", "weap", "proc", "e1", "e2", "e3", "e4", "3tnk", "arty", "v2rl"]


def trained(focus: str, n_vehicles: int = 0) -> set[str]:
    bot = RedAlertBot(log=lambda _: None, clock=Clock(), knobs=Knobs(unit_focus=focus))
    bot._vehicles_ordered = n_vehicles
    s = state(economy=RICH, buildings=BASE, available_production=AVAILABLE, units=[])
    return {o["item"] for o in cmds(orders_of(bot.choose_action(s, CTX)), "train")}


def test_antiinf_trains_flamethrowers_and_leaves_the_other_focuses_alone():
    assert "e4" in trained("antiinf") and "e3" not in trained("antiinf")
    assert "e3" in trained("rockets") and "e4" not in trained("rockets")
    assert "e4" not in trained("mixed")


def test_antiinf_takes_artillery_but_not_v2s_as_support():
    assert trained("antiinf", n_vehicles=3) >= {"arty"} and "v2rl" not in trained("antiinf", n_vehicles=3)
    assert "arty" in trained("mixed", n_vehicles=3) or "v2rl" in trained("mixed", n_vehicles=3)


def test_antiinf_without_its_units_falls_back_to_the_default_mix():
    bot = RedAlertBot(log=lambda _: None, clock=Clock(), knobs=Knobs(unit_focus="antiinf"))
    s = state(economy=RICH, buildings=BASE, available_production=["powr", "weap", "proc", "e1", "e3"], units=[])
    assert {o["item"] for o in cmds(orders_of(bot.choose_action(s, CTX)), "train")} & {"e1"}


def council(tmp_path, **kwargs) -> CouncilBot:
    return CouncilBot(log_dir=str(tmp_path), oracle="rules", **kwargs)


def see(bot: CouncilBot, kinds: list[str], at: float = 700.0) -> None:
    bot.enemy_seen = {i: (t, at) for i, t in enumerate(kinds)}


def test_off_by_default_whatever_the_enemy_is(tmp_path):
    bot = council(tmp_path)
    see(bot, ["e1"] * 30)
    assert not bot._anti_infantry_now(720.0)


def test_switches_on_an_infantry_heavy_enemy_only(tmp_path):
    bot = council(tmp_path, anti_infantry=0.6)
    see(bot, ["e1"] * 12 + ["e3"] * 8 + ["2tnk"] * 4)       # 20 of 24 infantry
    assert bot._anti_infantry_now(720.0)
    see(bot, ["e1"] * 6 + ["2tnk"] * 10)                     # tanks mostly
    assert not bot._anti_infantry_now(720.0)
    see(bot, ["e1"] * 8)                                     # too few seen to tell
    assert not bot._anti_infantry_now(720.0)
    see(bot, ["e1"] * 20, at=100.0)                          # seen long ago
    assert not bot._anti_infantry_now(720.0)
    see(bot, ["e1"] * 20)
    assert not bot._anti_infantry_now(200.0)                 # before anti_infantry_from


def test_guard_settings_default_to_v10s(tmp_path):
    bot = council(tmp_path)
    assert bot.guard_settings == {"guard_defenses": 1, "guard_infantry": 10, "late_buildings_from": 0.0, "guard_mix": False,
                                    "defend_radius": 16}
    k = Knobs()
    assert (k.guard_defenses, k.guard_infantry, k.late_buildings_from, k.guard_mix, k.defend_radius) == (1, 10, 0.0, False, 16)


def build_order(**knobs) -> list[str]:
    bot = RedAlertBot(log=lambda _: None, clock=Clock(), knobs=Knobs(opening="guard", **knobs))
    types = ["fact", "powr", "proc", "tent", "weap", "proc"]
    avail = ["powr", "proc", "tent", "weap", "fix", "dome", "ftur", "pbox"]
    economy = {"power_provided": 400, "power_drained": 100}
    return [bot._next_building(types, avail, economy, "", 3000, bot.knobs, now) for now in (60.0, 400.0)]


def test_service_depot_waits_when_asked_and_not_otherwise():
    assert build_order() == ["fix", "fix"]
    # before 5:00 the money goes elsewhere (here: a second war factory); at 6:40 the depot comes
    assert build_order(late_buildings_from=300.0) == ["weap", "fix"]


def defenses_wanted(types, **knobs):
    bot = RedAlertBot(log=lambda _: None, clock=Clock(), knobs=Knobs(opening="guard", **knobs))
    return bot._next_defense(types, ["ftur", "pbox"], 2000, bot.knobs)


def test_the_guard_builds_as_many_defenses_as_asked():
    one, two = ["fact", "tent", "ftur"], ["fact", "tent", "ftur", "ftur"]
    assert defenses_wanted(["fact", "tent"]) == "ftur"
    assert defenses_wanted(one) is None                      # v10: one, then the economy's turn
    assert defenses_wanted(one, guard_defenses=3) == "ftur"
    assert defenses_wanted(two, guard_defenses=3) == "ftur"
    assert defenses_wanted(two + ["ftur"], guard_defenses=3) is None


def guard_orders(cap: int, ordered: int) -> list[tuple[str, int]]:
    bot = RedAlertBot(log=lambda _: None, clock=Clock(), knobs=Knobs(opening="guard", guard_infantry=cap))
    bot._infantry_ordered = ordered
    got: list[dict] = []
    bot._train_army(lambda o, *_: got.append(o) or True, ["e1", "e3"], {"Infantry": 0, "Vehicle": 0}, 3000,
                    ["fact", "tent", "proc"], bot.knobs, 100.0)
    return [(o["item"], o["count"]) for o in got]


def test_guard_infantry_cap_is_a_setting():
    assert guard_orders(10, 9) == [("e3", 2)] and guard_orders(10, 10) == []     # v10: ten, then stop
    assert guard_orders(16, 10) == [("e1", 2)] and guard_orders(16, 15) == [("e3", 2)]
    assert guard_orders(16, 16) == []


def test_v10s_guard_trains_only_rifles_and_guard_mix_alternates():
    def run(mix: bool) -> list[str]:
        bot = RedAlertBot(log=lambda _: None, clock=Clock(), knobs=Knobs(opening="guard", guard_mix=mix, guard_infantry=16))
        items = []
        for ordered in range(0, 16, 2):
            bot._infantry_ordered = ordered
            bot._train_army(lambda o, *_: items.append(o["item"]) or True, ["e1", "e3"], {"Infantry": 0, "Vehicle": 0},
                            3000, ["fact", "tent", "proc"], bot.knobs, 100.0)
        return items
    assert run(False) == ["e1"] * 8
    assert run(True) == ["e1", "e3"] * 4


def test_defend_radius_decides_which_enemies_pull_the_army_out():
    bot = RedAlertBot(log=lambda _: None, clock=Clock())
    obs = {"visible_enemies": [{"id": 1, "type": "e1", "x": 50, "y": 50}, {"id": 2, "type": "e1", "x": 50, "y": 62}]}
    assert len(bot._threats(obs, (50, 50))) == 2                 # v10: 16 cells
    assert bot._threats(obs, (50, 50), 8) == [(50, 50)]
    assert bot._threats(obs, (50, 50), 11) == [(50, 50)]
    assert len(bot._threats(obs, (50, 50), 12)) == 2             # (inclusive: the second one is 12 away)
