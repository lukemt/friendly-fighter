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
