"""bots.pokemon: the scripted pokemon_vgc_doubles_draft agent, on payloads shaped like the platform
guide's (<control_plane>/skill/pokemon) and the existing Pokémon fixtures."""

from __future__ import annotations

import time

import pytest

from altruagent import DecisionContext, GameState, LegalAction
from bots.pokemon import PokemonAgent, create_agent
from bots.pokemon import agent as pokemon_agent
from bots.pokemon.battle import TurnPlanner
from bots.pokemon.damage import Battler, damage
from bots.pokemon.dex import calc_stat, effectiveness, mon_from_card

ME, FOE = "agent-me", "agent-foe"
CTX = DecisionContext(session_id="s", tournament_id=None, game_type="pokemon_vgc_doubles_draft", agent_id=ME,
                      seat_position=0)


def card(cid, species, item, ability, nature, evs, moves):
    return {"card_id": cid, "species": species, "item": item, "ability": ability, "nature": nature, "level": 50,
            "evs": evs, "ivs": {}, "moves": moves}


PHYS = {"hp": 4, "atk": 252, "spe": 252}
SPEC = {"hp": 4, "spa": 252, "spe": 252}
BULK = {"hp": 252, "def": 128, "spd": 128}
CARDS = [
    card("vgc-incineroar", "Incineroar", "Sitrus Berry", "Intimidate", "Careful", BULK,
         ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]),
    card("vgc-rillaboom", "Rillaboom", "Miracle Seed", "Grassy Surge", "Adamant", PHYS,
         ["Fake Out", "Grassy Glide", "Wood Hammer", "U-turn"]),
    card("vgc-urshifu-rapid-strike", "Urshifu-Rapid-Strike", "Mystic Water", "Unseen Fist", "Adamant", PHYS,
         ["Surging Strikes", "Close Combat", "Aqua Jet", "Protect"]),
    card("vgc-amoonguss", "Amoonguss", "Rocky Helmet", "Regenerator", "Calm", BULK,
         ["Spore", "Rage Powder", "Pollen Puff", "Protect"]),
    card("vgc-garchomp", "Garchomp", "Life Orb", "Rough Skin", "Jolly", PHYS,
         ["Earthquake", "Dragon Claw", "Rock Slide", "Protect"]),
    card("vgc-tornadus", "Tornadus", "Covert Cloak", "Prankster", "Timid", SPEC,
         ["Tailwind", "Bleakwind Storm", "Taunt", "Protect"]),
    card("vgc-gholdengo", "Gholdengo", "Choice Specs", "Good as Gold", "Modest", SPEC,
         ["Make It Rain", "Shadow Ball", "Nasty Plot", "Protect"]),
    card("vgc-dragonite", "Dragonite", "Choice Band", "Multiscale", "Adamant", PHYS,
         ["Extreme Speed", "Dragon Claw", "Fire Punch", "Protect"]),
    card("vgc-magikarp", "Magikarp", "Light Ball", "Swift Swim", "Hardy", {}, ["Splash", "Tackle", "Flail", "Bounce"]),
    card("vgc-chienpao", "Chien-Pao", "Focus Sash", "Sword of Ruin", "Jolly", PHYS,
         ["Icicle Crash", "Sucker Punch", "Sacred Sword", "Protect"]),
    card("vgc-flutter-mane", "Flutter Mane", "Booster Energy", "Protosynthesis", "Timid", SPEC,
         ["Moonblast", "Shadow Ball", "Dazzling Gleam", "Protect"]),
    card("vgc-pelipper", "Pelipper", "Damp Rock", "Drizzle", "Modest", SPEC,
         ["Hurricane", "Weather Ball", "Tailwind", "Protect"]),
    card("vgc-kingambit", "Kingambit", "Black Glasses", "Defiant", "Adamant", PHYS,
         ["Kowtow Cleave", "Sucker Punch", "Iron Head", "Protect"]),
    card("vgc-farigiraf", "Farigiraf", "Electric Seed", "Armor Tail", "Quiet", BULK,
         ["Trick Room", "Psychic", "Hyper Voice", "Protect"]),
]
BY_ID = {c["card_id"]: c for c in CARDS}
BY_SPECIES = {mon_from_card(c).species_id: c for c in CARDS}


def raw_state(observation, actions, phase, version=3):
    return GameState.from_mcp_state({
        "session_id": "s", "game_type": "pokemon_vgc_doubles_draft", "status": "in_progress",
        "state_version": version, "phase": phase, "observation": observation, "is_terminal": False,
        "is_current_actor": True, "legal_actions": {"session_id": "s", "state_version": version, "actions": actions},
    })


def templated(action_id, template):
    return {"action_id": action_id, "label": action_id,
            "input": {"session_id": "s", "action_id": action_id, "state_version": 3, "action": template}}


def draft_state(available, legal, mine=(), theirs=(), pick=1):
    obs = {
        "phase": "draft", "draft_complete": False, "battle_format": "gen9vgc2025regi", "current_seat": ME,
        "pick_number": pick, "picks_remaining": 13 - pick, "first_drafter": ME,
        "available_card_ids": [c["card_id"] for c in available], "available_cards": available,
        "rosters": {ME: [{"card_id": c["card_id"], "species": c["species"]} for c in mine],
                    FOE: [{"card_id": c["card_id"], "species": c["species"]} for c in theirs]},
        "picks": [], "decision_timeout_seconds": 15, "decision_deadline_at": "2026-10-16T17:00:15+00:00",
    }
    actions = [{"action_id": f"draft_pick:{c['card_id']}", "label": f"Draft {c['species']}",
                "input": {"session_id": "s", "action_id": f"draft_pick:{c['card_id']}", "state_version": 3,
                          "action": {"type": "draft_pick", "card_id": c["card_id"]}}} for c in legal]
    return raw_state(obs, actions, "draft")


def summary(c, hp=1.0):
    mon = mon_from_card(c)
    return {"species": mon.species_id, "name": mon.name, "types": [t.upper() for t in mon.types],
            "base_stats": mon.base, "current_hp": round(mon.stats["hp"] * hp), "max_hp": mon.stats["hp"],
            "current_hp_fraction": hp, "status": None, "boosts": {}, "moves": [], "fainted": hp <= 0,
            "active": True, "revealed": True}


# -- the model ----------------------------------------------------------------------------------------

def test_stats_match_showdown_at_level_50():
    # Incineroar 95/115/90/80/90/60, Careful 252 HP / 128 Def / 128 SpD: Showdown's numbers.
    inc = mon_from_card(BY_ID["vgc-incineroar"])
    assert inc.stats["hp"] == 202
    assert inc.stats["def"] == calc_stat("def", 90, 128) == 126
    assert inc.stats["spa"] == 90  # (2*80+31)*50/100 + 5, minus 10% (Careful)


def test_damage_respects_types_and_immunities():
    chomp, gholdengo = Battler(mon_from_card(BY_ID["vgc-garchomp"])), Battler(mon_from_card(BY_ID["vgc-gholdengo"]))
    tornadus = Battler(mon_from_card(BY_ID["vgc-tornadus"]))
    assert damage(chomp, tornadus, "earthquake").expected == 0  # Flying
    eq = damage(chomp, gholdengo, "earthquake", spread=False)
    assert eq.effectiveness == 2 and eq.low < eq.expected < eq.high
    assert damage(chomp, gholdengo, "earthquake", spread=True).expected < eq.expected
    assert effectiveness("Ghost", ["Normal"]) == 0


# -- draft ---------------------------------------------------------------------------------------------

def test_draft_returns_one_of_the_offered_cards_fast():
    pool = CARDS
    legal = [c for c in pool if c["card_id"] != "vgc-pelipper"]
    state = draft_state(pool, legal)
    start = time.perf_counter()
    decision = PokemonAgent(log=False).choose_action(state, CTX)
    assert time.perf_counter() - start < 2.0
    assert isinstance(decision, LegalAction)
    assert decision.action_id in {a.action_id for a in state.legal_actions}
    assert decision.action_id != "draft_pick:vgc-magikarp"


def test_draft_never_takes_magikarp_over_a_real_card():
    pool = [BY_ID["vgc-magikarp"], BY_ID["vgc-incineroar"]]
    decision = PokemonAgent(log=False).choose_action(draft_state(pool, pool), CTX)
    assert decision.action_id == "draft_pick:vgc-incineroar"


def test_draft_without_card_details_still_picks_a_legal_card():
    legal = CARDS[:3]
    state = draft_state([], legal)
    decision = PokemonAgent(log=False).choose_action(state, CTX)
    assert decision.action_id in {a.action_id for a in state.legal_actions}


# -- team preview --------------------------------------------------------------------------------------

def preview_state(mine, theirs, roster_spelling=lambda c: mon_from_card(c).species_id):
    obs = {"phase": "team_preview", "battle_format": "gen9vgc2025regi", "waiting_for_action": True,
           "your_roster": [summary(c) for c in mine], "opponent_roster": [summary(c) for c in theirs]}
    template = {"type": "select_lineup", "roster": [roster_spelling(c) for c in mine], "bring_count": 4,
                "lead_count": 2, "instructions": "Choose exactly 4 ..."}
    return raw_state(obs, [templated("select_lineup", template)], "team_preview")


def test_lineup_brings_four_and_leads_two_of_them():
    mine = [BY_ID[c] for c in ("vgc-incineroar", "vgc-rillaboom", "vgc-magikarp", "vgc-garchomp", "vgc-tornadus",
                               "vgc-gholdengo")]
    theirs = [BY_ID[c] for c in ("vgc-urshifu-rapid-strike", "vgc-amoonguss", "vgc-dragonite", "vgc-chienpao",
                                 "vgc-flutter-mane", "vgc-kingambit")]
    state = preview_state(mine, theirs)
    decision = PokemonAgent(log=False).choose_action(state, CTX)
    roster = state.legal_actions[0].input["action"]["roster"]
    assert decision["type"] == "select_lineup"
    assert len(set(decision["bring"])) == 4 and set(decision["bring"]) <= set(roster)
    assert len(set(decision["leads"])) == 2 and set(decision["leads"]) <= set(decision["bring"])
    assert "magikarp" not in decision["bring"]


def test_lineup_answers_in_the_roster_s_own_spelling():
    mine = CARDS[:6]
    state = preview_state(mine, CARDS[6:12], roster_spelling=lambda c: c["species"])
    decision = PokemonAgent(log=False).choose_action(state, CTX)
    assert set(decision["bring"]) <= {c["species"] for c in mine}


def test_lineup_uses_draft_memory_when_the_preview_has_only_names():
    agent = PokemonAgent(log=False)
    agent.choose_action(draft_state(CARDS, CARDS), CTX)  # sees every card
    state = preview_state(CARDS[:6], CARDS[6:12])
    state.raw["observation"]["your_roster"] = [{"species": mon_from_card(c).species_id} for c in CARDS[:6]]
    decision = agent.choose_action(state, CTX)
    assert len(decision["bring"]) == 4 and agent.fallbacks == 0


# -- doubles turns --------------------------------------------------------------------------------------

def move_option(move_id, targets, slot, foes, mine):
    opts = []
    for t in targets:
        if t > 0:
            opts.append({"target": t, "side": "opponent", "species": foes[t - 1]})
        elif t < 0:
            opts.append({"target": t, "side": "self" if -t - 1 == slot else "ally", "species": mine[-t - 1]})
        else:
            opts.append({"target": 0, "side": "none", "species": None})
    return {"type": "move", "move_id": move_id, "base_power": 80, "category": "physical", "move_type": "normal",
            "current_pp": 10, "accuracy": 100, "targets": targets, "target_options": opts}


SINGLE = lambda slot: [-(2 - slot), 1, 2]  # noqa: E731 "normal" target: ally or either foe
TARGETS = {
    "fakeout": "adj", "flareblitz": "normal", "knockoff": "normal", "partingshot": "normal",
    "grassyglide": "normal", "woodhammer": "normal", "uturn": "normal", "surgingstrikes": "normal",
    "closecombat": "normal", "aquajet": "normal", "protect": "none", "earthquake": "none", "dragonclaw": "normal",
    "rockslide": "none", "tailwind": "none", "bleakwindstorm": "none", "taunt": "normal", "makeitrain": "none",
    "shadowball": "normal", "nastyplot": "none", "spore": "normal", "ragepowder": "none", "pollenpuff": "normal",
    "icicle crash": "normal", "sacredsword": "normal", "suckerpunch": "normal", "iciclecrash": "normal",
    "moonblast": "normal", "dazzlinggleam": "none", "trickroom": "none", "psychic": "normal", "hypervoice": "none",
}


def turn_state(mine, foes, *, hp=None, foe_hp=None, bench=(), forced=(False, False), turn=1, only=None):
    hp = hp or [1.0, 1.0]
    foe_hp = foe_hp or [1.0, 1.0]
    my_ids = [mon_from_card(c).species_id if c else None for c in mine]
    foe_ids = [mon_from_card(c).species_id if c else None for c in foes]
    slots = []
    for i, c in enumerate(mine):
        options = []
        if forced[i]:
            options = [{"type": "switch", "species": mon_from_card(b).species_id} for b in bench] or [{"type": "pass"}]
        elif c is None or any(forced):
            options = [{"type": "pass"}]
        else:
            for m in (only or {}).get(i, c["moves"]):
                mid = m.lower().replace(" ", "").replace("-", "")
                kind = TARGETS.get(mid, "normal")
                targets = {"normal": SINGLE(i), "adj": [1, 2], "none": [0]}[kind]
                options.append(move_option(mid, targets, i, foe_ids, my_ids))
            options += [{"type": "switch", "species": mon_from_card(b).species_id} for b in bench]
        slots.append({"slot": i, "board_position": -(i + 1),
                      "active": summary(c, hp[i]) if c and not forced[i] else None,
                      "force_switch": forced[i], "options": options})
    obs = {
        "phase": "moving", "is_doubles": True, "turn": turn, "weather": None, "fields": [],
        "active_pokemon": [summary(c, h) if c else None for c, h in zip(mine, hp)],
        "opponent_active_pokemon": [summary(c, h) if c else None for c, h in zip(foes, foe_hp)],
        "team": {f"p1: {c['species']}": summary(c, h) for c, h in zip(mine, hp) if c}
        | {f"p1: {b['species']}": summary(b) for b in bench},
        "opponent_team": {f"p2: {c['species']}": summary(c, h) for c, h in zip(foes, foe_hp) if c},
        "side_conditions": [], "opponent_side_conditions": [], "force_switch": list(forced),
    }
    template = {"type": "doubles_turn", "slots": slots, "target_legend": {"1": "opponent A"},
                "instructions": "Submit one action per slot."}
    return raw_state(obs, [templated("doubles_turn", template)], "moving")


def seen_draft(mine, foes):
    """An agent that went through the draft and Team Preview: it knows every set on the field."""
    agent = PokemonAgent(log=False)
    agent.choose_action(draft_state(CARDS, CARDS), CTX)
    mine = [c for c in mine if c] + [c for c in CARDS if c not in mine and c not in foes]
    agent.choose_action(preview_state(mine[:6], [c for c in foes if c] + [c for c in CARDS if c not in mine[:6]][:4]), CTX)
    return agent


def decide(state, agent=None):
    if agent is None:
        tmpl = state.legal_actions[0].input["action"]
        obs = state.raw["observation"]
        mine = [BY_SPECIES.get((s.get("active") or {}).get("species", "")) for s in tmpl["slots"]]
        foes = [BY_SPECIES.get((f or {}).get("species", "")) for f in obs.get("opponent_active_pokemon") or []]
        agent = seen_draft([m for m in mine if m], [f for f in foes if f])
    decision = agent.choose_action(state, CTX)
    assert agent.fallbacks == 0, "the planner failed and the fallback played"
    pokemon_agent.check(decision, state)
    return decision


def test_turn_one_fake_out_and_no_hit_on_the_partner():
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]],
                       [BY_ID["vgc-urshifu-rapid-strike"], BY_ID["vgc-chienpao"]])
    d = decide(state)
    for k in ("slot_0", "slot_1"):
        assert d[k]["type"] == "move"
        assert d[k].get("target", 0) >= 0, "never aim at our own side"
    assert d["slot_0"]["move_id"] == "fakeout"


def test_fake_out_is_not_repeated_after_the_first_turn():
    agent = PokemonAgent(log=False)
    mine = [BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]]
    foes = [BY_ID["vgc-urshifu-rapid-strike"], BY_ID["vgc-chienpao"]]
    assert decide(turn_state(mine, foes, turn=1), agent)["slot_0"]["move_id"] == "fakeout"
    d = decide(turn_state(mine, foes, turn=2), agent)
    assert d["slot_0"]["move_id"] != "fakeout"


def test_focus_fire_finishes_a_weakened_foe():
    # Kingambit at 15% is a free KO for Urshifu's Close Combat.
    state = turn_state([BY_ID["vgc-urshifu-rapid-strike"], BY_ID["vgc-amoonguss"]],
                       [BY_ID["vgc-pelipper"], BY_ID["vgc-kingambit"]], foe_hp=[1.0, 0.15], turn=3,
                       only={0: ["Close Combat", "Protect"]})
    d = decide(state)
    assert d["slot_0"] == {"type": "move", "move_id": "closecombat", "target": 2}


def test_spread_move_that_hits_the_partner_is_avoided_unless_it_is_immune():
    # Garchomp's Earthquake would hit Incineroar; with Tornadus (Flying) as partner it's free.
    foes = [BY_ID["vgc-amoonguss"], BY_ID["vgc-farigiraf"]]  # both take Earthquake neutrally
    d = decide(turn_state([BY_ID["vgc-garchomp"], BY_ID["vgc-tornadus"]], foes, turn=3,
                          only={0: ["Earthquake", "Dragon Claw"], 1: ["Bleakwind Storm"]}))
    assert d["slot_0"]["move_id"] == "earthquake"
    pokemon_hp = [1.0, 0.3]
    d = decide(turn_state([BY_ID["vgc-garchomp"], BY_ID["vgc-urshifu-rapid-strike"]], foes, hp=pokemon_hp, turn=3,
                          only={0: ["Earthquake", "Dragon Claw"], 1: ["Close Combat"]}))
    assert d["slot_0"]["move_id"] == "dragonclaw"


def test_protect_when_about_to_be_knocked_out():
    # Chien-Pao at 10% HP against two fast attackers that outspeed and KO it.
    state = turn_state([BY_ID["vgc-chienpao"], BY_ID["vgc-incineroar"]],
                       [BY_ID["vgc-flutter-mane"], BY_ID["vgc-urshifu-rapid-strike"]], hp=[0.1, 1.0], turn=3,
                       only={0: ["Sacred Sword", "Protect"], 1: ["Knock Off", "Flare Blitz"]})
    d = decide(state)
    assert d["slot_0"]["move_id"] == "protect"


def test_forced_switch_with_two_fainted_and_one_reserve():
    reserve = BY_ID["vgc-tornadus"]
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]], [BY_ID["vgc-gholdengo"], None],
                       bench=[reserve], forced=(True, True))
    for slot in state.legal_actions[0].input["action"]["slots"]:
        slot["options"].append({"type": "pass"})
    d = decide(state)
    kinds = sorted(d[k]["type"] for k in ("slot_0", "slot_1"))
    assert kinds == ["pass", "switch"]


def test_forced_switch_picks_the_better_matchup():
    # Gholdengo and Kingambit on the field: Garchomp (Ground) beats both; Amoonguss doesn't.
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-tornadus"]],
                       [BY_ID["vgc-gholdengo"], BY_ID["vgc-kingambit"]],
                       bench=[BY_ID["vgc-amoonguss"], BY_ID["vgc-garchomp"]], forced=(True, False))
    d = decide(state)
    assert d["slot_0"] == {"type": "switch", "species": "garchomp"}
    assert d["slot_1"] == {"type": "pass"}


def test_unknown_species_and_moves_still_get_a_legal_answer():
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]],
                       [BY_ID["vgc-gholdengo"], BY_ID["vgc-flutter-mane"]])
    tmpl = state.legal_actions[0].input["action"]
    tmpl["slots"][0]["active"] = {"species": "missingno"}
    tmpl["slots"][1]["options"].insert(0, {"type": "move", "move_id": "notarealmove", "targets": [1, 2]})
    state.raw["observation"] = {}
    agent = PokemonAgent(log=False)
    d = agent.choose_action(state, CTX)
    pokemon_agent.check(d, state)


# -- the safety layer -----------------------------------------------------------------------------------

def test_a_crash_in_the_planner_falls_back_to_a_legal_move(monkeypatch):
    def boom(self):
        raise RuntimeError("bug")

    monkeypatch.setattr(TurnPlanner, "choose", boom)
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]],
                       [BY_ID["vgc-gholdengo"], BY_ID["vgc-flutter-mane"]])
    agent = PokemonAgent(log=False)
    d = agent.choose_action(state, CTX)
    assert agent.fallbacks == 1
    pokemon_agent.check(d, state)
    assert d["type"] == "doubles_turn"


def test_an_answer_the_template_does_not_offer_is_replaced(monkeypatch):
    monkeypatch.setattr(PokemonAgent, "_turn", lambda self, t, o: {
        "type": "doubles_turn", "slot_0": {"type": "move", "move_id": "hyperbeam", "target": 1},
        "slot_1": {"type": "pass"}})
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]],
                       [BY_ID["vgc-gholdengo"], BY_ID["vgc-flutter-mane"]])
    agent = PokemonAgent(log=False)
    d = agent.choose_action(state, CTX)
    assert agent.fallbacks == 1
    pokemon_agent.check(d, state)


@pytest.mark.parametrize("bad", [
    {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "fakeout", "target": -2}, "slot_1": {"type": "pass"}},
    {"type": "doubles_turn", "slot_0": {"type": "pass"}, "slot_1": {"type": "pass"}},
    {"type": "doubles_turn", "slot_0": {"type": "switch", "species": "tornadus"},
     "slot_1": {"type": "switch", "species": "tornadus"}},
    {"type": "select_lineup", "bring": ["a"], "leads": ["a"]},
    "doubles_turn",
])
def test_check_refuses_what_the_server_would_refuse(bad):
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]],
                       [BY_ID["vgc-gholdengo"], BY_ID["vgc-flutter-mane"]], bench=[BY_ID["vgc-tornadus"]])
    with pytest.raises(pokemon_agent.Unacceptable):
        pokemon_agent.check(bad, state)


def test_fallback_never_returns_the_bare_template():
    template = {"type": "doubles_turn", "slots": [
        {"slot": 0, "options": [{"type": "move", "move_id": "tackle", "targets": [1, 2]}]},
        {"slot": 1, "options": [{"type": "pass"}]}]}
    state = raw_state({}, [templated("doubles_turn", template)], "moving")
    d = pokemon_agent.fallback(state)
    assert d == {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "tackle", "target": 1},
                 "slot_1": {"type": "pass"}}


def test_a_whole_turn_answers_well_under_two_seconds():
    state = turn_state([BY_ID["vgc-incineroar"], BY_ID["vgc-garchomp"]],
                       [BY_ID["vgc-gholdengo"], BY_ID["vgc-flutter-mane"]],
                       bench=[BY_ID["vgc-tornadus"], BY_ID["vgc-amoonguss"]])
    agent = PokemonAgent(log=False)
    start = time.perf_counter()
    for _ in range(5):
        decide(state, agent)
    assert (time.perf_counter() - start) / 5 < 0.5


# -- routing --------------------------------------------------------------------------------------------

def test_friendly_fighter_routes_pokemon_to_the_bot(monkeypatch):
    from agent.agent import FriendlyFighter

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    ff = FriendlyFighter(record=False, controls=False)
    state = preview_state(CARDS[:6], CARDS[6:12])
    decision = ff.choose_action(state, CTX)
    assert isinstance(ff._pokemon_bot, PokemonAgent)
    assert ff._fallback is None  # the LLM example was never built
    assert decision["type"] == "select_lineup"


def test_create_agent():
    assert isinstance(create_agent(), PokemonAgent)
