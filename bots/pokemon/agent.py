"""The Pokémon agent: one object per match, playing ``pokemon_vgc_doubles_draft`` from draft to the
last turn.

- **draft**: score the offered cards (team.choose_draft) and return that ``draft_pick`` action.
- **team_preview**: the 4 and the 2 leads with the best matchup (team.choose_lineup).
- **moving**: joint-score both slots (battle.TurnPlanner).

It remembers every draft card it sees (both rosters are public), so in battle it knows each
Pokémon's exact set. A process that starts mid-match has no such memory and guesses spreads.

**Safety layer.** Every answer is checked against the server's own template before it is sent;
an exception, a missing field or an answer that doesn't fit falls back to the smoke agent's legal
choice (examples/smoke_agent.py), and if even that can't read the template, to the simplest legal
answer this module can build. A bug costs one weak move, never an INVALID_ACTION (which would
end the match's worker).
"""

from __future__ import annotations

import os
import time
import traceback
from typing import Any

from altruagent import DecisionContext, GameState, LegalAction
from altruagent.console import print_line

from .battle import TurnPlanner, read_board, summary_species
from .dex import Mon, guess_mon, mon_from_card, to_id
from .team import choose_draft, choose_lineup

GAME_TYPE = "pokemon_vgc_doubles_draft"
VERBOSE = os.environ.get("FF_POKEMON_VERBOSE", "") not in ("", "0")


class PokemonAgent:
    def __init__(self, *, log: bool = True) -> None:
        self.log = log
        self.cards: dict[str, dict] = {}  # card_id -> card
        self.my_ids: list[str] = []  # our roster's species ids
        self.foe_ids: list[str] = []
        self._prev_active: set[str] = set()
        self._prev_foes: set[str] = set()
        self._last_move: dict[str, str] = {}  # our species id -> last move id sent
        self.decisions = 0
        self.fallbacks = 0
        self.last_seconds = 0.0

    # -- entry point -------------------------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext | None = None) -> LegalAction | dict:
        start = time.perf_counter()
        self.decisions += 1
        try:
            decision = self._decide(state, context)
            check(decision, state)
        except Exception as exc:  # noqa: BLE001 - any bug must still produce a legal move
            self.fallbacks += 1
            if self.log:
                print_line(f"[pokemon] decision failed ({exc!r}); playing the fallback")
                if VERBOSE:
                    print_line(traceback.format_exc())
            decision = fallback(state)
        self.last_seconds = time.perf_counter() - start
        return decision

    def on_action_result(self, result: dict, context: DecisionContext | None = None) -> None:
        if isinstance(result, dict) and result.get("error") and self.log:
            print_line(f"[pokemon] action refused: {result.get('error')} {str(result.get('detail'))[:200]}")

    # -- phases ------------------------------------------------------------------------------------

    def _decide(self, state: GameState, context: DecisionContext | None) -> LegalAction | dict:
        if not state.legal_actions:
            raise ValueError("no legal actions")
        obs = state.raw.get("observation") if isinstance(state.raw, dict) else None
        obs = obs if isinstance(obs, dict) else {}
        first = state.legal_actions[0]
        template = first.input.get("action") if isinstance(first.input, dict) else None
        kind = template.get("type") if isinstance(template, dict) else None
        if first.action_id == "select_lineup" or kind == "select_lineup":
            return self._lineup(template, obs)
        if first.action_id == "doubles_turn" or kind == "doubles_turn":
            return self._turn(template, obs)
        if all(a.action_id.startswith("draft_pick:") for a in state.legal_actions):
            return self._draft(state, obs, context)
        raise ValueError(f"unknown decision {first.action_id!r}")

    def _remember(self, cards: list) -> None:
        for card in cards or []:
            if isinstance(card, dict) and card.get("card_id"):
                self.cards.setdefault(str(card["card_id"]), card)

    def _card_mon(self, card_id: str, species: object = None) -> Mon | None:
        card = self.cards.get(card_id)
        mon = mon_from_card(card) if card else None
        return mon or guess_mon(species or card_id.removeprefix("vgc-"))

    def _draft(self, state: GameState, obs: dict, context: DecisionContext | None) -> LegalAction:
        self._remember(obs.get("available_cards"))
        me = str(context.agent_id) if context is not None else ""
        rosters = obs.get("rosters") if isinstance(obs.get("rosters"), dict) else {}
        mine_raw = rosters.get(me)
        if mine_raw is None:
            # Our roster isn't keyed by our id: the picks say whose cards are whose.
            mine_raw = [p for p in obs.get("picks") or [] if isinstance(p, dict) and str(p.get("seat_id")) == me]
        theirs_raw = [c for k, v in rosters.items() if k != me for c in (v or [])]
        mine = [m for m in (self._card_mon(str(c.get("card_id", "")), c.get("species")) for c in mine_raw or []) if m]
        theirs = [m for m in (self._card_mon(str(c.get("card_id", "")), c.get("species")) for c in theirs_raw) if m]
        pool = {}
        for card in obs.get("available_cards") or []:
            if isinstance(card, dict) and card.get("card_id"):
                mon = mon_from_card(card)
                if mon:
                    pool[str(card["card_id"])] = mon
        options: dict[str, Mon] = {}
        by_card: dict[str, LegalAction] = {}
        for action in state.legal_actions:
            cid = action.action_id.split(":", 1)[1]
            by_card[cid] = action
            mon = pool.get(cid) or self._card_mon(cid)
            if mon:
                options[cid] = mon
        if not options:
            raise ValueError("no draft option could be read")
        pick = choose_draft(options, mine, theirs, pool or options)
        if self.log:
            print_line(f"[pokemon] draft: {pick} (pick {obs.get('pick_number')}, roster {[m.name for m in mine]})")
        return by_card[pick]

    def _lineup(self, template: dict, obs: dict) -> dict:
        roster = list(dict.fromkeys(template["roster"]))
        your = {summary_species(s): s for s in obs.get("your_roster") or [] if isinstance(s, dict)}
        theirs_sum = [s for s in obs.get("opponent_roster") or [] if isinstance(s, dict)]
        by_species = {to_id(c.get("species")): c for c in self.cards.values()}
        mine: list[Mon] = []
        for name in roster:
            sid = summary_species(name) or to_id(name)
            mon = self._mon_for(sid, your.get(sid), by_species)
            if mon is None:
                raise ValueError(f"unknown species in roster: {name!r}")
            mine.append(mon)
        theirs = [m for m in (self._mon_for(summary_species(s), s, by_species) for s in theirs_sum) if m]
        self.my_ids = [m.species_id for m in mine]
        self.foe_ids = [m.species_id for m in theirs]
        bring, leads = choose_lineup(mine, theirs)
        answer = {"type": "select_lineup", "bring": [roster[i] for i in bring], "leads": [roster[i] for i in leads]}
        if self.log:
            print_line(f"[pokemon] lineup: bring {answer['bring']}, lead {answer['leads']} vs {[m.name for m in theirs]}")
        return answer

    def _mon_for(self, sid: str, summary: dict | None, by_species: dict[str, dict]) -> Mon | None:
        card = by_species.get(sid)
        if card:
            return mon_from_card(card)
        if isinstance(summary, dict) and summary.get("moves"):
            moves = [m if isinstance(m, str) else (m or {}).get("id", "") for m in summary.get("moves") or []]
            mon = mon_from_card({**summary, "species": sid, "moves": moves})
            if mon is not None:
                mon.known_set = False
                return mon
        return guess_mon(sid)

    def _species_cards(self) -> tuple[dict[str, Mon], dict[str, Mon]]:
        mine, theirs = {}, {}
        for card in self.cards.values():
            mon = mon_from_card(card)
            if mon is None:
                continue
            if mon.species_id in self.foe_ids:
                theirs[mon.species_id] = mon
            else:
                mine[mon.species_id] = mon
                theirs.setdefault(mon.species_id, mon)
        return mine, theirs

    def _turn(self, template: dict, obs: dict) -> dict:
        slots = sorted(template["slots"], key=lambda s: s.get("slot", 0))
        if len(slots) != 2:
            raise ValueError(f"expected 2 slots, got {len(slots)}")
        my_cards, foe_cards = self._species_cards()
        board = read_board(obs, slots, my_cards, foe_cards)
        forced = any(s.get("force_switch") for s in slots)
        active = [b.mon.species_id if b else "" for b in board.mine]
        foes = [b.mon.species_id if b else "" for b in board.foes]
        if not forced:
            board.first_turn = [bool(sid) and sid not in self._prev_active for sid in active]
            board.foe_first_turn = [bool(sid) and sid not in self._prev_foes for sid in foes]
        board.last_move = [self._last_move.get(sid, "") for sid in active]
        planner = TurnPlanner(board, slots)
        slot_0, slot_1, score, notes = planner.choose()
        if not forced:
            self._prev_active = {s for s in active if s}
            self._prev_foes = {s for s in foes if s}
        for sid, send in zip(active, (slot_0, slot_1)):
            if sid:
                self._last_move[sid] = to_id(send.get("move_id")) if send.get("type") == "move" else ""
        if self.log:
            print_line(f"[pokemon] turn {obs.get('turn')}: {fmt(slot_0)} | {fmt(slot_1)} "
                       f"({[b.mon.name if b else None for b in board.mine]} vs "
                       f"{[b.mon.name if b else None for b in board.foes]}, score {score:.2f})")
        return {"type": "doubles_turn", "slot_0": slot_0, "slot_1": slot_1}


def fmt(send: dict) -> str:
    if send.get("type") == "move":
        return f"{send.get('move_id')}->{send.get('target')}"
    if send.get("type") == "switch":
        return f"switch {send.get('species')}"
    return "pass"


# -- the safety layer ---------------------------------------------------------------------------------

class Unacceptable(ValueError):
    """An answer the server's template wouldn't accept."""


def check(decision: Any, state: GameState) -> None:
    """Raise ``Unacceptable`` unless ``decision`` fits what the server offered."""
    if isinstance(decision, LegalAction):
        if decision.action_id not in {a.action_id for a in state.legal_actions}:
            raise Unacceptable(f"{decision.action_id!r} is not a legal action")
        template = decision.input.get("action") if isinstance(decision.input, dict) else None
        if isinstance(template, dict) and template.get("type") in ("select_lineup", "doubles_turn"):
            raise Unacceptable("a template can't be sent as is")
        return
    if not isinstance(decision, dict):
        raise Unacceptable(f"unexpected answer {decision!r}")
    template = state.legal_actions[0].input.get("action")
    if not isinstance(template, dict) or decision.get("type") != template.get("type"):
        raise Unacceptable(f"a {decision.get('type')!r} answer to a {getattr(template, 'get', dict().get)('type')!r} decision")
    if decision.get("type") == "select_lineup":
        roster = {to_id(s) for s in template["roster"]}
        bring, leads = decision.get("bring"), decision.get("leads")
        if not isinstance(bring, list) or len(bring) != int(template.get("bring_count") or 4) \
                or len({to_id(s) for s in bring}) != len(bring) or not {to_id(s) for s in bring} <= roster:
            raise Unacceptable(f"bad bring {bring!r}")
        if not isinstance(leads, list) or len(leads) != int(template.get("lead_count") or 2) \
                or len({to_id(s) for s in leads}) != len(leads) or not {to_id(s) for s in leads} <= {to_id(s) for s in bring}:
            raise Unacceptable(f"bad leads {leads!r}")
        return
    if decision.get("type") == "doubles_turn":
        slots = {s.get("slot", i): s for i, s in enumerate(template["slots"])}
        chosen = []
        for n in (0, 1):
            send = decision.get(f"slot_{n}")
            options = slots[n].get("options") or []
            if not isinstance(send, dict) or not _offered(send, options):
                raise Unacceptable(f"slot_{n} {send!r} is not among its options")
            chosen.append(send)
        if chosen[0]["type"] == chosen[1]["type"] == "switch" and to_id(chosen[0]["species"]) == to_id(chosen[1]["species"]):
            raise Unacceptable("both slots switch to the same Pokémon")
        if chosen[0]["type"] == chosen[1]["type"] == "pass" and not all(
            all(o.get("type") == "pass" for o in s.get("options") or []) for s in slots.values()
        ):
            raise Unacceptable("both slots pass while something else is offered")
        return
    raise Unacceptable(f"unexpected answer type {decision.get('type')!r}")


def _offered(send: dict, options: list[dict]) -> bool:
    kind = send.get("type")
    for o in options:
        if o.get("type") != kind:
            continue
        if kind == "pass":
            return True
        if kind == "switch" and to_id(o.get("species")) == to_id(send.get("species")) and send.get("species"):
            return True
        if kind == "move" and o.get("move_id") == send.get("move_id"):
            targets = o.get("targets") or []
            if not targets:
                return "target" not in send or send.get("target") in (None, 0)
            return send.get("target") in targets and not isinstance(send.get("target"), bool)
    return False


def fallback(state: GameState) -> LegalAction | dict:
    """A legal answer without judgement: the smoke agent's, else the simplest one we can build."""
    from examples import smoke_agent

    try:
        decision = smoke_agent.choose_action(state, None)
        check(decision, state)
        return decision
    except Exception:  # noqa: BLE001
        pass
    first = state.legal_actions[0]
    template = first.input.get("action") if isinstance(first.input, dict) else None
    if isinstance(template, dict) and template.get("type") == "select_lineup":
        roster = list(dict.fromkeys(template.get("roster") or []))
        return {"type": "select_lineup", "bring": roster[:4], "leads": roster[:2]}
    if isinstance(template, dict) and template.get("type") == "doubles_turn":
        out, taken = {}, None
        for i, slot in enumerate(sorted(template.get("slots") or [], key=lambda s: s.get("slot", 0))):
            pick = {"type": "pass"}
            for o in slot.get("options") or []:
                if o.get("type") == "move" and o.get("move_id"):
                    pick = {"type": "move", "move_id": o["move_id"]}
                    if o.get("targets"):
                        pick["target"] = next((t for t in o["targets"] if isinstance(t, int) and t > 0), o["targets"][0])
                    break
                if o.get("type") == "switch" and o.get("species") and o["species"] != taken:
                    pick = {"type": "switch", "species": o["species"]}
                    taken = o["species"]
                    break
            out[f"slot_{slot.get('slot', i)}"] = pick
        return {"type": "doubles_turn", **out}
    return first


def create_agent(**kwargs: Any) -> PokemonAgent:
    return PokemonAgent(**kwargs)
