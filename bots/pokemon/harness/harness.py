"""Local harness: whole ``pokemon_vgc_doubles_draft`` games (draft -> Team Preview -> doubles battle)
between two agents, with Showdown's own battle engine (sim_server.js) as the referee.

The platform's adapter isn't public, so this module builds its observations and action templates
the way the platform guide (``<control_plane>/skill/pokemon``) describes them, and turns the
agents' answers back into Showdown choices. Agents see only ``GameState`` objects, exactly like
on the platform.

    .venv/bin/python -m bots.pokemon.harness.harness --games 100 --opponent maxdamage
    .venv/bin/python -m bots.pokemon.harness.harness --games 100 --opponent random --workers 2

Showdown comes from ``npm install pokemon-showdown@0.11.11`` in ``SHOWDOWN_DIR`` (default
/home/dev/showdown, the dev container's home).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable

from altruagent import DecisionContext, GameState, LegalAction

from ..dex import move as move_data, species as species_data, to_id
from .baselines import make_baseline

HERE = Path(__file__).resolve().parent
SHOWDOWN_DIR = Path(os.environ.get("SHOWDOWN_DIR", "/home/dev/showdown"))
FORMAT = "gen9vgc2025regi"
CARDS = HERE.parent / "data" / "sim_cards.json"
# Every card seen in live test matches so far (2026-10-16: 30 of them). A live pool is 18 of the
# platform's list, with items shared; a species' card is the same set in every pool.
LIVE_CARDS = HERE.parent / "data" / "live_cards.json"
GAME = "pokemon_vgc_doubles_draft"
SNAKE = [0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0]
STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")

NO_TARGET = {"self", "allAdjacentFoes", "allAdjacent", "all", "allySide", "foeSide", "allies", "randomNormal",
             "scripted", "foeSideOrAlly"}


def load_cards(pool: str = "sim") -> list[dict]:
    with (LIVE_CARDS if pool == "live" else CARDS).open() as f:
        return json.load(f)


# -- the Showdown process ---------------------------------------------------------------------------

class Showdown:
    def __init__(self) -> None:
        env = {**os.environ, "NODE_PATH": str(SHOWDOWN_DIR / "node_modules")}
        self.proc = subprocess.Popen(["node", str(HERE / "sim_server.js")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=env, bufsize=1)

    def send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("showdown process ended")
        return json.loads(line)

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


# -- states shaped like the platform's -------------------------------------------------------------

def make_state(observation: dict, actions: list[dict], phase: str, version: int) -> GameState:
    return GameState.from_mcp_state({
        "session_id": "local", "game_type": GAME, "runtime_adapter": "pokemon", "status": "in_progress",
        "state_version": version, "phase": phase, "observation": observation, "is_terminal": False,
        "is_current_actor": True,
        "legal_actions": {"session_id": "local", "state_version": version, "actions": actions},
    })


def card_summary(card: dict, *, own: bool = True) -> dict:
    """A roster entry at Team Preview, as the platform sends it: our own with its set, the
    opponent's with no HP, item, ability or moves."""
    sp = species_data(card["species"]) or {}
    s = {"species": to_id(card["species"]), "name": card["species"], "current_hp": 0, "max_hp": 0,
         "current_hp_fraction": 0, "status": None, "ability": None, "item": "unknown_item",
         "types": [t.upper() for t in sp.get("types", [])], "base_stats": sp.get("bs", {}),
         "boosts": {k: 0 for k in BOOST_KEYS}, "moves": [], "fainted": False, "active": False, "revealed": False,
         "is_terastallized": False}
    if own:
        s.update(current_hp_fraction=1.0, ability=to_id(card.get("ability")), item=to_id(card.get("item")),
                 moves=[to_id(m) for m in card.get("moves", [])])
    return s


def card_set(card: dict) -> dict:
    return {"name": card["species"], "species": card["species"], "item": card.get("item", ""),
            "ability": card.get("ability", ""), "nature": card.get("nature", ""), "level": card.get("level", 50),
            "evs": {k: int((card.get("evs") or {}).get(k, 0)) for k in STAT_KEYS},
            "ivs": {k: int((card.get("ivs") or {}).get(k, 31)) for k in STAT_KEYS},
            "moves": list(card.get("moves", []))}


@dataclass
class Seat:
    agent: Any
    agent_id: str
    side: str  # p1 / p2
    roster: list[dict] = field(default_factory=list)  # drafted cards
    version: int = 0
    decisions: int = 0
    seconds: float = 0.0
    max_seconds: float = 0.0
    errors: int = 0

    def ask(self, state: GameState) -> Any:
        ctx = DecisionContext(session_id="local", tournament_id=None, game_type=GAME, agent_id=self.agent_id,
                              seat_position=0 if self.side == "p1" else 1)
        start = time.perf_counter()
        fn = self.agent.choose_action if hasattr(self.agent, "choose_action") else self.agent
        answer = fn(state, ctx)
        took = time.perf_counter() - start
        self.decisions += 1
        self.seconds += took
        self.max_seconds = max(self.max_seconds, took)
        return answer


# -- draft ----------------------------------------------------------------------------------------

def run_draft(seats: list[Seat], pool: list[dict], first: int, rng: random.Random) -> list[dict]:
    available = list(pool)
    picks: list[dict] = []
    for n, k in enumerate(SNAKE):
        seat = seats[k if first == 0 else 1 - k]
        items = {c.get("item") for c in seat.roster}
        offered = [c for c in available if c.get("item") not in items]
        if not offered:
            raise RuntimeError("no legal card (item clause)")
        obs = {
            "phase": "draft", "draft_complete": False, "battle_format": FORMAT, "current_seat": seat.agent_id,
            "pick_number": n + 1, "picks_remaining": len(SNAKE) - n, "first_drafter": seats[first].agent_id,
            "available_card_ids": [c["card_id"] for c in available], "available_cards": [dict(c) for c in available],
            "rosters": {s.agent_id: [{"card_id": c["card_id"], "species": c["species"]} for c in s.roster] for s in seats},
            "picks": picks, "decision_timeout_seconds": 15,
        }
        actions = [{"action_id": f"draft_pick:{c['card_id']}", "label": f"Draft {c['species']}",
                    "input": {"action_id": f"draft_pick:{c['card_id']}", "action": {"type": "draft_pick", "card_id": c["card_id"]}}}
                   for c in offered]
        seat.version += 1
        answer = seat.ask(make_state(obs, actions, "draft", seat.version))
        cid = None
        if isinstance(answer, LegalAction):
            cid = answer.action_id.split(":", 1)[1]
        elif isinstance(answer, str):
            cid = answer.split(":", 1)[1] if ":" in answer else answer
        elif isinstance(answer, dict):
            cid = answer.get("card_id")
        card = next((c for c in offered if c["card_id"] == cid), None)
        if card is None:
            seat.errors += 1
            card = rng.choice(offered)
        seat.roster.append(card)
        available.remove(card)
        picks.append({"pick_number": n + 1, "seat_id": seat.agent_id, "card_id": card["card_id"],
                      "species": card["species"], "auto": False})
    return picks


def run_preview(seat: Seat, foe: Seat, rng: random.Random) -> list[int]:
    """Returns indices into seat.roster: the 4 brought, leads first."""
    roster = [to_id(c["species"]) for c in seat.roster]
    obs = {"phase": "team_preview", "battle_format": FORMAT, "waiting_for_action": True,
           "your_roster": [card_summary(c) for c in seat.roster],
           "opponent_roster": [card_summary(c, own=False) for c in foe.roster],
           "clock": {"decision_seconds_left": 90, "bank_seconds_left": 420}}
    template = {"type": "select_lineup", "roster": roster, "bring_count": 4, "lead_count": 2,
                "instructions": "Choose exactly 4 species from roster for bring, and 2 of them for leads."}
    actions = [{"action_id": "select_lineup", "label": "Select lineup",
                "input": {"action_id": "select_lineup", "action": template}}]
    seat.version += 1
    answer = seat.ask(make_state(obs, actions, "team_preview", seat.version))
    try:
        bring = [roster.index(to_id(s)) for s in answer["bring"]]
        leads = [roster.index(to_id(s)) for s in answer["leads"]]
        assert len(set(bring)) == 4 and len(set(leads)) == 2 and set(leads) <= set(bring)
    except Exception:  # noqa: BLE001
        seat.errors += 1
        bring = list(range(4))
        leads = [0, 1]
    return leads + [i for i in bring if i not in leads]


# -- battle -------------------------------------------------------------------------------------------

@dataclass
class MonState:
    species: str
    hp: float = 100.0
    max_hp: float = 100.0
    status: str = ""
    boosts: dict[str, int] = field(default_factory=dict)
    moves: list[str] = field(default_factory=list)
    fainted: bool = False
    item: str | None = None
    ability: str | None = None


class BattleView:
    """One side's view of the battle from the public log (the opponent's side in percent)."""

    def __init__(self) -> None:
        self.mons: dict[str, dict[str, MonState]] = {"p1": {}, "p2": {}}
        self.active: dict[str, list[str | None]] = {"p1": [None, None], "p2": [None, None]}
        # As the platform sends them (poke-env's maps): {"GRASSY_TERRAIN": the turn it started}.
        self.weather: dict[str, int] = {}
        self.fields: dict[str, int] = {}
        self.sides: dict[str, dict[str, int]] = {"p1": {}, "p2": {}}
        self.turn = 0
        self.winner: str | None = None
        self.ended = False

    def _mon(self, ident: str) -> tuple[str, int, MonState]:
        side_pos, name = ident.split(": ", 1)
        side = side_pos[:2]
        pos = "ab".index(side_pos[2]) if len(side_pos) > 2 else -1
        sid = to_id(name)
        mon = self.mons[side].setdefault(sid, MonState(species=sid))
        return side, pos, mon

    @staticmethod
    def _hp(mon: MonState, cond: str) -> None:
        cond = cond.strip()
        if cond.endswith("fnt") or cond.startswith("0 "):
            mon.hp, mon.fainted = 0.0, True
            return
        parts = cond.split()
        try:
            cur, mx = parts[0].split("/")
            mon.hp, mon.max_hp = float(cur), float(mx)
        except (ValueError, IndexError):
            return
        mon.status = parts[1] if len(parts) > 1 else ""

    def feed(self, lines: list[str]) -> None:
        skip = False
        for line in lines:
            if skip:
                skip = False
                continue
            if not line.startswith("|"):
                continue
            parts = line.split("|")
            tag = parts[1] if len(parts) > 1 else ""
            try:
                if tag == "split":
                    skip = True  # the next line is the private one; keep the public one after it
                elif tag in ("switch", "drag", "replace"):
                    # Sets are named after their species, so the ident's name is the species id.
                    side, pos, mon = self._mon(parts[2])
                    self.active[side][pos] = mon.species
                    mon.boosts = {}
                    self._hp(mon, parts[4])
                elif tag in ("-damage", "-heal", "-sethp"):
                    _, _, mon = self._mon(parts[2])
                    self._hp(mon, parts[3])
                elif tag == "faint":
                    side, pos, mon = self._mon(parts[2])
                    mon.hp, mon.fainted = 0.0, True
                elif tag == "-status":
                    _, _, mon = self._mon(parts[2])
                    mon.status = parts[3]
                elif tag == "-curestatus":
                    _, _, mon = self._mon(parts[2])
                    mon.status = ""
                elif tag in ("-boost", "-unboost"):
                    _, _, mon = self._mon(parts[2])
                    n = int(parts[4]) * (1 if tag == "-boost" else -1)
                    mon.boosts[parts[3]] = max(-6, min(6, mon.boosts.get(parts[3], 0) + n))
                elif tag == "-setboost":
                    _, _, mon = self._mon(parts[2])
                    mon.boosts[parts[3]] = int(parts[4])
                elif tag in ("-clearboost", "-clearnegativeboost"):
                    _, _, mon = self._mon(parts[2])
                    mon.boosts = {} if tag == "-clearboost" else {k: v for k, v in mon.boosts.items() if v > 0}
                elif tag == "-clearallboost":
                    for side in self.mons.values():
                        for mon in side.values():
                            mon.boosts = {}
                elif tag == "move":
                    _, _, mon = self._mon(parts[2])
                    mid = to_id(parts[3])
                    if mid not in mon.moves:
                        mon.moves.append(mid)
                elif tag == "-weather":
                    name = enum_name(parts[2])
                    if parts[2] == "none":
                        self.weather = {}
                    elif name not in self.weather:
                        self.weather = {name: self.turn}
                elif tag == "-fieldstart":
                    self.fields[enum_name(parts[2])] = self.turn
                elif tag == "-fieldend":
                    self.fields.pop(enum_name(parts[2]), None)
                elif tag == "-sidestart":
                    self.sides[parts[2][:2]].setdefault(enum_name(parts[3]), self.turn)
                elif tag == "-sideend":
                    self.sides[parts[2][:2]].pop(enum_name(parts[3]), None)
                elif tag == "-item":
                    _, _, mon = self._mon(parts[2])
                    mon.item = parts[3]
                elif tag == "-enditem":
                    _, _, mon = self._mon(parts[2])
                    mon.item = ""
                elif tag == "-ability":
                    _, _, mon = self._mon(parts[2])
                    mon.ability = parts[3]
                elif tag == "turn":
                    self.turn = int(parts[2])
                elif tag == "win":
                    self.winner = parts[2]
                    self.ended = True
                elif tag == "tie":
                    self.ended = True
            except (IndexError, ValueError, KeyError):
                continue


def enum_name(name: str) -> str:
    """A Showdown condition as poke-env's enum spells it: "move: Light Screen" -> "LIGHT_SCREEN"."""
    return name.replace("move: ", "").strip().upper().replace(" ", "_").replace("-", "_")


def status_name(status: str | None, fainted: bool = False) -> str | None:
    """poke-env's spelling: "BRN", "PAR"..., "FNT" once fainted."""
    if fainted:
        return "FNT"
    return status.upper() if status else None


BOOST_KEYS = ("accuracy", "atk", "def", "evasion", "spa", "spd", "spe")


def summary(mon: MonState, *, own: dict | None = None, active: bool = False) -> dict:
    """A Pokémon as the platform's observation shows it: the opponent's in percent, with an
    unknown item and ability until they're revealed."""
    sp = species_data(mon.species) or {}
    s = {"species": mon.species, "name": sp.get("name", mon.species), "current_hp": round(mon.hp),
         "max_hp": round(mon.max_hp), "current_hp_fraction": round(mon.hp / mon.max_hp, 4) if mon.max_hp else 0.0,
         "status": status_name(mon.status, mon.fainted), "ability": to_id(mon.ability) if mon.ability else None,
         "item": "unknown_item" if mon.item is None else to_id(mon.item),
         "types": [t.upper() for t in sp.get("types", [])], "base_stats": sp.get("bs", {}),
         "boosts": {k: mon.boosts.get(k, 0) for k in BOOST_KEYS}, "moves": list(mon.moves), "fainted": mon.fainted,
         "active": active, "revealed": True, "is_terastallized": False}
    if own is not None:
        cur, mx, st = parse_condition(own["condition"])
        fainted = own["condition"].endswith("fnt")
        s.update(current_hp=round(cur), max_hp=round(mx), current_hp_fraction=round(cur / mx, 4) if mx else 0.0,
                 status=status_name(st, fainted), fainted=fainted, moves=list(own.get("moves", [])),
                 ability=own.get("baseAbility"), item=own.get("item"))
    return s


def parse_condition(cond: str) -> tuple[float, float, str]:
    if cond.endswith("fnt"):
        return 0.0, 1.0, ""
    parts = cond.split()
    cur, mx = parts[0].split("/")
    return float(cur), float(mx), parts[1] if len(parts) > 1 else ""


def move_targets(target: str, slot: int) -> list[int]:
    me, ally = -(slot + 1), -(2 - slot)
    if target in ("normal", "any"):
        return [ally, 1, 2]
    if target == "adjacentFoe":
        return [1, 2]
    if target == "adjacentAlly":
        return [ally]
    if target == "adjacentAllyOrSelf":
        return sorted([me, ally])
    return [0]


def build_turn(request: dict, view: BattleView, side: str) -> tuple[dict, dict]:
    """The observation and the doubles_turn template for this request, from ``side``'s view."""
    foe_side = "p2" if side == "p1" else "p1"
    pokemon = request["side"]["pokemon"]
    by_species = {}
    for p in pokemon:
        sp = species_data(p["details"].split(",")[0])
        by_species[to_id(sp["name"]) if sp else to_id(p["ident"].split(": ", 1)[1])] = p
    own_ids = list(by_species)
    force = request.get("forceSwitch")
    actives = request.get("active") or []
    bench = [sid for i, sid in enumerate(own_ids) if i >= 2 and not pokemon[i]["condition"].endswith("fnt")]
    foe_names = view.active[foe_side]

    def summary_of(sid: str, active: bool) -> dict:
        mon = view.mons[side].get(sid) or MonState(species=sid)
        return summary(mon, own=by_species[sid], active=active)

    slots = []
    for i in range(2):
        sid = own_ids[i] if i < len(own_ids) else None
        alive = sid is not None and not pokemon[i]["condition"].endswith("fnt")
        options: list[dict] = []
        forced_here = bool(force and force[i])
        if force:
            if forced_here:
                options = [{"type": "switch", "species": s} for s in bench]
                n_forced = sum(1 for f in force if f)
                if len(bench) < n_forced:
                    options.append({"type": "pass"})
                if not options:
                    options = [{"type": "pass"}]
            else:
                options = [{"type": "pass"}]
        elif i < len(actives) and alive and not pokemon[i].get("commanding"):
            act = actives[i]
            for m in act.get("moves", []):
                if m.get("disabled"):
                    continue
                # A locked move (the second turn of Shadow Force, Outrage...) comes without a target type
                # and takes no target.
                targets = move_targets(m["target"], i) if m.get("target") else [0]
                opts = []
                for t in targets:
                    if t > 0:
                        name = foe_names[t - 1]
                        opts.append({"target": t, "side": "opponent", "species": name})
                    elif t < 0:
                        idx = -t - 1
                        opts.append({"target": t, "side": "ally",
                                     "species": own_ids[idx] if idx < len(own_ids) else None})
                    else:
                        opts.append({"target": 0, "side": "none", "species": None})
                mv = move_data(m["id"]) or {}
                options.append({"type": "move", "move_id": m["id"], "base_power": mv.get("bp", 0),
                                "category": str(mv.get("cat", "")).upper(), "move_type": str(mv.get("type", "")).upper(),
                                "current_pp": m.get("pp"), "accuracy": (mv.get("acc") or 100) / 100,
                                "targets": targets, "target_options": opts})
            if not act.get("trapped") and not act.get("maybeTrapped"):
                options.extend({"type": "switch", "species": s} for s in bench)
            if not options:
                options = [{"type": "pass"}]
        else:
            options = [{"type": "pass"}]
        slots.append({"slot": i, "board_position": -(i + 1), "active": summary_of(sid, True) if sid and alive else None,
                      "force_switch": forced_here, "options": options})
    template = {"type": "doubles_turn", "slots": slots,
                "target_legend": {"1": "opponent position A", "2": "opponent position B", "-1": "your slot 0",
                                  "-2": "your slot 1", "0": "no target"},
                "instructions": "Submit one action per slot."}
    obs = {
        "phase": "moving", "is_doubles": True, "turn": view.turn, "format": FORMAT,
        "weather": dict(view.weather), "fields": dict(view.fields),
        "active_pokemon": [summary_of(own_ids[i], True) if i < len(own_ids) else None for i in range(2)],
        "opponent_active_pokemon": [summary(view.mons[foe_side][n], active=True) if n and n in view.mons[foe_side] else None
                                    for n in foe_names],
        "team": {f"{side}: {sid}": summary_of(sid, i < 2) for i, sid in enumerate(own_ids)},
        "opponent_team": {f"{foe_side}: {sid}": summary(m, active=sid in foe_names) for sid, m in view.mons[foe_side].items()},
        "side_conditions": dict(view.sides[side]), "opponent_side_conditions": dict(view.sides[foe_side]),
        "force_switch": [bool(force and force[i]) for i in range(2)], "can_tera": [False, False],
        "clock": {"decision_seconds_left": 55, "bank_seconds_left": 420},
    }
    return obs, template


def to_choice(answer: Any, template: dict, request: dict) -> str | None:
    """A doubles_turn answer as a Showdown choice string, or None if it isn't legal."""
    if not isinstance(answer, dict) or answer.get("type") != "doubles_turn":
        return None
    own_ids = []
    for p in request["side"]["pokemon"]:
        sp = species_data(p["details"].split(",")[0])
        own_ids.append(to_id(sp["name"]) if sp else to_id(p["ident"].split(": ", 1)[1]))
    parts, switched = [], set()
    force = request.get("forceSwitch")
    for i in range(2):
        a = answer.get(f"slot_{i}")
        slot = template["slots"][i]
        if not isinstance(a, dict):
            return None
        kind = a.get("type")
        if kind == "pass":
            if not any(o["type"] == "pass" for o in slot["options"]):
                return None
            parts.append("pass")
        elif kind == "switch":
            sid = to_id(a.get("species"))
            if not any(o["type"] == "switch" and to_id(o["species"]) == sid for o in slot["options"]) or sid in switched:
                return None
            switched.add(sid)
            parts.append(f"switch {own_ids.index(sid) + 1}")
        elif kind == "move":
            opt = next((o for o in slot["options"] if o["type"] == "move" and o["move_id"] == a.get("move_id")), None)
            if opt is None:
                return None
            idx = [m["id"] for m in request["active"][i]["moves"]].index(opt["move_id"]) + 1
            t = a.get("target")
            if opt["targets"] != [0]:
                if t not in opt["targets"]:
                    return None
                parts.append(f"move {idx} {t}")
            else:
                parts.append(f"move {idx}")
        else:
            return None
    if force and not parts:
        return None
    return ", ".join(parts)


def random_choice(template: dict, request: dict, rng: random.Random) -> str:
    from .baselines import random_turn

    return to_choice(random_turn(template, rng), template, request) or "default"


def draw_pool(cards: list[dict], rng: random.Random, size: int = 18) -> list[dict]:
    """A random pool: different species and different items, so the Item Clause never leaves a
    player without a legal card (the platform's own pools hide the clash the same way)."""
    pool, items = [], set()
    for card in rng.sample(cards, len(cards)):
        if card.get("item") in items:
            continue
        pool.append(card)
        items.add(card.get("item"))
        if len(pool) == size:
            break
    return pool


@dataclass
class Result:
    winner: int | None  # seat index 0/1, None for a tie
    turns: int
    errors: tuple[int, int]
    decisions: tuple[int, int]
    seconds: tuple[float, float]
    max_seconds: tuple[float, float]
    rosters: tuple[list[str], list[str]]
    first: int


def play_game(agent_factories: list[Callable[[], Any]], seed: int, showdown: Showdown, cards: list[dict],
              live_pool: bool = False) -> Result:
    rng = random.Random(seed)
    seats = [Seat(agent_factories[0](), "agent-a", "p1"), Seat(agent_factories[1](), "agent-b", "p2")]
    # A live pool shares items, as the platform's do; the Item Clause is the draft's to enforce.
    pool = rng.sample(cards, min(18, len(cards))) if live_pool else draw_pool(cards, rng)
    first = seed % 2
    run_draft(seats, pool, first, rng)
    orders = [run_preview(seats[0], seats[1], rng), run_preview(seats[1], seats[0], rng)]
    teams = []
    for seat, order in zip(seats, orders):
        teams.append([card_set(seat.roster[i]) for i in order] + [card_set(c) for k, c in enumerate(seat.roster) if k not in order])
    bid = f"b{seed}"
    showdown.send({"id": bid, "cmd": "start", "format": FORMAT,
                   "seed": [seed & 0xFFFF, (seed >> 16) & 0xFFFF or 1, 7, 13],
                   "p1": {"name": "agent-a", "team": teams[0]}, "p2": {"name": "agent-b", "team": teams[1]}})
    view = BattleView()
    requests: dict[str, dict] = {}
    preview_sent: set[str] = set()
    guard = 0
    while not view.ended:
        guard += 1
        if guard > 5000:
            break
        msg = showdown.read()
        if msg.get("id") != bid:
            continue
        if msg.get("error"):
            raise RuntimeError(msg["error"])
        if msg.get("closed"):
            break
        chunk = msg["chunk"]
        kind, _, body = chunk.partition("\n")
        if kind == "update":
            view.feed(body.split("\n"))
        elif kind == "sideupdate":
            side, _, rest = body.partition("\n")
            if rest.startswith("|request|"):
                req = json.loads(rest[len("|request|"):])
                if req.get("wait"):
                    continue
                k = 0 if side == "p1" else 1
                if req.get("teamPreview"):
                    if side not in preview_sent:
                        preview_sent.add(side)
                        showdown.send({"id": bid, "cmd": "write", "data": f">{side} team 1234"})
                    continue
                requests[side] = req
                obs, template = build_turn(req, view, side)
                seat = seats[k]
                seat.version += 1
                actions = [{"action_id": "doubles_turn", "label": "Doubles turn",
                            "input": {"action_id": "doubles_turn", "action": template}}]
                try:
                    answer = seat.ask(make_state(obs, actions, "moving", seat.version))
                    choice = to_choice(answer, template, req)
                except Exception:  # noqa: BLE001
                    choice = None
                if choice is None:
                    seat.errors += 1
                    choice = random_choice(template, req, rng)
                showdown.send({"id": bid, "cmd": "write", "data": f">{side} {choice}"})
            elif rest.startswith("|error|"):
                # A choice Showdown refused: count it and play a random legal one.
                k = 0 if side == "p1" else 1
                seats[k].errors += 1
                req = requests.get(side)
                if req is not None and "[Invalid choice]" in rest:
                    _, template = build_turn(req, view, side)
                    choice = random_choice(template, req, rng) if seats[k].errors % 5 else "default"
                    showdown.send({"id": bid, "cmd": "write", "data": f">{side} {choice}"})
        elif kind == "end":
            view.ended = True
    showdown.send({"id": bid, "cmd": "close"})
    winner = {"agent-a": 0, "agent-b": 1}.get(view.winner or "")
    return Result(winner, view.turn, (seats[0].errors, seats[1].errors), (seats[0].decisions, seats[1].decisions),
                  (seats[0].seconds, seats[1].seconds), (seats[0].max_seconds, seats[1].max_seconds),
                  ([c["species"] for c in seats[0].roster], [c["species"] for c in seats[1].roster]), first)


# -- running many -------------------------------------------------------------------------------------

def factory(name: str) -> Callable[[], Any]:
    """``bot``; a baseline (``random``, ``maxdamage``, ``smoke``); or ``bot+bstdraft``: our battle play
    with max-damage's draft and lineup (highest base-stat totals), to measure the battle part alone."""
    if name in ("bot", "ours"):
        from bots.pokemon import create_agent

        return lambda: create_agent(log=False)
    if name == "bot+bstdraft":
        return _bst_drafting_bot
    return lambda: make_baseline(name)


def _bst_drafting_bot() -> Any:
    from bots.pokemon import PokemonAgent

    bst = make_baseline("maxdamage")

    class BstDraft(PokemonAgent):
        def _draft(self, state, obs, context):  # noqa: ANN001
            self._remember(obs.get("available_cards"))
            return bst.choose_action(state, context)

        def _lineup(self, template, obs):  # noqa: ANN001
            super()._lineup(template, obs)  # remembers both rosters
            roster = sorted(template["roster"], key=lambda s: -sum((species_data(s) or {"bs": {}})["bs"].values()))
            return {"type": "select_lineup", "bring": roster[:4], "leads": roster[:2]}

    return BstDraft(log=False)


def _worker(args: tuple[str, str, list[int], str]) -> list[tuple[int, Result | str]]:
    a, b, seeds, card_pool = args
    cards = load_cards(card_pool)
    sd = Showdown()
    out = []
    try:
        for seed in seeds:
            # Alternate seats so neither side always sits in p1.
            names = [a, b] if seed % 4 < 2 else [b, a]
            try:
                r = play_game([factory(names[0]), factory(names[1])], seed, sd, cards, card_pool == "live")
            except Exception as exc:  # noqa: BLE001
                out.append((seed, f"error: {exc!r}"))
                sd.close()
                sd = Showdown()
                continue
            if names[0] != a:  # report from a's point of view
                r = Result(None if r.winner is None else 1 - r.winner, r.turns, r.errors[::-1], r.decisions[::-1],
                           r.seconds[::-1], r.max_seconds[::-1], r.rosters[::-1], 1 - r.first)
            out.append((seed, r))
    finally:
        sd.close()
    return out


def run(a: str, b: str, games: int, seed: int, workers: int, card_pool: str = "sim") -> dict:
    seeds = [seed + i for i in range(games)]
    chunks = [seeds[k::workers] for k in range(workers)]
    if workers > 1:
        with Pool(workers) as pool:
            parts = pool.map(_worker, [(a, b, c, card_pool) for c in chunks])
    else:
        parts = [_worker((a, b, seeds, card_pool))]
    results = [r for part in parts for r in part]
    played = [(s, r) for s, r in results if isinstance(r, Result)]
    failed = [(s, r) for s, r in results if not isinstance(r, Result)]
    wins = sum(1 for _, r in played if r.winner == 0)
    losses = sum(1 for _, r in played if r.winner == 1)
    ties = len(played) - wins - losses
    dec = sum(r.decisions[0] for _, r in played) or 1
    summary_ = {
        "a": a, "b": b, "pool": card_pool, "games": len(played), "failed": len(failed), "wins": wins, "losses": losses, "ties": ties,
        "win_rate": round(wins / max(1, len(played)), 3),
        "win_rate_first_drafter": _rate([r for _, r in played if r.first == 0]),
        "win_rate_second_drafter": _rate([r for _, r in played if r.first == 1]),
        "avg_turns": round(sum(r.turns for _, r in played) / max(1, len(played)), 1),
        "a_errors": sum(r.errors[0] for _, r in played), "b_errors": sum(r.errors[1] for _, r in played),
        "a_decisions": dec, "a_mean_ms": round(1000 * sum(r.seconds[0] for _, r in played) / dec, 2),
        "a_max_ms": round(1000 * max((r.max_seconds[0] for _, r in played), default=0), 1),
        "a_top_picks": ", ".join(f"{sp} {n}" for sp, n in Counter(sp for _, r in played for sp in r.rosters[0]).most_common(10)),
        "failures": [f"{s}: {r}" for s, r in failed[:5]],
    }
    return summary_


def _rate(rs: list[Result]) -> str:
    if not rs:
        return "-"
    return f"{sum(1 for r in rs if r.winner == 0)}/{len(rs)}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--a", default="bot", help="bot | random | maxdamage | smoke")
    p.add_argument("--opponent", "--b", dest="b", default="maxdamage")
    p.add_argument("--games", type=int, default=20)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--pool", choices=("sim", "live"), default="sim",
                   help="sim: 18 of data/sim_cards.json's 447; live: 18 of the cards seen live (data/live_cards.json)")
    args = p.parse_args(argv)
    start = time.time()
    out = run(args.a, args.b, args.games, args.seed, args.workers, args.pool)
    out["wall_seconds"] = round(time.time() - start, 1)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
