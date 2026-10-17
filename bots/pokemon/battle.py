"""Doubles turns: read the board, score every move x target and switch per slot, then pick the best
pair of slot choices together (focus fire, Helping Hand, a spread move while the partner protects).

All values are in "HP fractions": damage dealt to a foe (weighted by how dangerous it is), damage
our side avoids, plus a bonus for each likely KO. Legality comes only from the template's own
options, so whatever this module picks is something the server offered.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .damage import NO_HIT, Battler, Field, Hit, Side, damage, effective_speed, grounded, is_immune, move_type
from .dex import Mon, guess_mon, move as move_data, species as species_data, to_id
from .team import FAKE_OUT, PROTECT, REDIRECT, SLEEP, stab_placeholders

SETUP = {"swordsdance", "nastyplot", "calmmind", "dragondance", "quiverdance", "bulkup", "shellsmash", "bellydrum",
         "irondefense", "coil", "workup", "shiftgear", "victorydance", "tidyup", "agility", "growth", "howl",
         "tailglow", "geomancy", "filletaway", "clangoroussoul", "noretreat", "honeclaws", "curse", "cosmicpower",
         "amnesia", "acidarmor", "rockpolish", "autotomize", "takeheart", "trailblaze"}
RECOVER = {"recover", "roost", "slackoff", "moonlight", "synthesis", "morningsun", "softboiled", "milkdrink",
           "shoreup", "strengthsap", "healorder", "lunarblessing", "junglehealing", "lifedew", "wish", "rest"}
PIVOT = {"uturn", "voltswitch", "flipturn", "partingshot", "teleport", "chillyreception", "shedtail"}
SCREENS = {"reflect": "reflect", "lightscreen": "light_screen", "auroraveil": "aurora_veil"}
WEATHER_MOVES = {"sunnyday": "sunnyday", "raindance": "raindance", "sandstorm": "sandstorm", "snowscape": "snow",
                 "chillyreception": "snow"}
TERRAIN_MOVES = {"electricterrain", "grassyterrain", "psychicterrain", "mistyterrain"}
NO_FLINCH_ABILITIES = {"innerfocus", "shielddust"}


@dataclass
class Board:
    mine: list[Battler | None]  # our slot 0, slot 1
    foes: list[Battler | None]  # opponent position A (target 1), B (target 2)
    my_side: Side = field(default_factory=Side)
    foe_side: Side = field(default_factory=Side)
    fld: Field = field(default_factory=Field)
    bench: dict[str, Battler] = field(default_factory=dict)  # our switch-ins by species id
    first_turn: list[bool] = field(default_factory=lambda: [False, False])  # ours: Fake Out works
    foe_first_turn: list[bool] = field(default_factory=lambda: [False, False])
    last_move: list[str] = field(default_factory=lambda: ["", ""])  # our last move id per slot


@dataclass
class Option:
    slot: int
    send: dict
    kind: str  # move / switch / pass
    move_id: str = ""
    mv: dict | None = None
    hits: dict[int, Hit] = field(default_factory=dict)  # foe index -> hit
    ally_hit: Hit | None = None
    value: float = 0.0  # everything that isn't damage to the foes
    p_act: float = 1.0
    protect: bool = False
    helping: bool = False
    flinch: int | None = None
    flinch_value: float = 0.0
    switch_to: str | None = None
    note: str = ""


# -- threats ---------------------------------------------------------------------------------------

def foe_moves(b: Battler) -> list[dict]:
    """The moves we think this foe has: its card's four, else a STAB placeholder per type."""
    out = [mv for mv in (move_data(m) for m in b.mon.moves) if mv]
    if not any(mv["cat"] != "Status" for mv in out):
        out += stab_placeholders(b.mon)
    return out


@dataclass
class Threat:
    hit: Hit  # the foe's most damaging move on us
    priority: int
    move_id: str
    moves: list[tuple[Hit, int]] = field(default_factory=list)  # every damaging move: (hit, priority)


def threat(foe: Battler, me: Battler, board: Board) -> Threat:
    best, best_mv, moves = NO_HIT, None, []
    for mv in foe_moves(foe):
        if mv["cat"] == "Status":
            continue
        h = damage(foe, me, mv, board.fld, board.my_side)
        pri = int(mv["pri"]) + (1 if to_id(mv["name"]) == "grassyglide" and board.fld.terrain_id == "grassyterrain" else 0)
        moves.append((h, pri))
        if h.mean > best.mean:
            best, best_mv = h, mv
    return Threat(best, int(best_mv["pri"]) if best_mv else 0, to_id(best_mv["name"]) if best_mv else "", moves)


def ko_chance(hit: Hit, hp: float) -> float:
    if hit.high <= 0 or hp <= 0:
        return 0.0 if hp > 0 else 1.0
    if hit.low >= hp:
        p = 1.0
    elif hit.high < hp:
        p = 0.0
    else:
        p = (hit.high - hp) / max(1e-9, hit.high - hit.low)
    return p * hit.accuracy


def moves_first(me: Battler, my_pri: int, my_side: Side, foe: Battler, foe_pri: int, foe_side: Side, fld: Field) -> float:
    """Chance ``me`` acts before ``foe`` (1, 0, or 0.5 on a speed tie)."""
    if my_pri != foe_pri:
        return 1.0 if my_pri > foe_pri else 0.0
    a, b = effective_speed(me, my_side), effective_speed(foe, foe_side)
    if fld.trick_room:
        a, b = -a, -b
    return 1.0 if a > b else 0.0 if a < b else 0.5


# -- the decision ------------------------------------------------------------------------------------

class TurnPlanner:
    def __init__(self, board: Board, slots: list[dict]):
        self.board = board
        self.slots = slots  # the template's slots, by slot number
        b = board
        self.threats: dict[tuple[int, int], Threat] = {}
        for j, foe in enumerate(b.foes):
            for i, me in enumerate(b.mine):
                if foe is not None and me is not None and foe.hp > 0 and me.hp > 0:
                    self.threats[j, i] = threat(foe, me, b)
        self.foe_weight = [self._foe_weight(j) for j in range(2)]
        self.foe_protect = [
            0.2 if f is not None and any(m in PROTECT for m in f.mon.moves) else 0.0 for f in b.foes
        ]

    # How dangerous foe j is: 0.7 for a harmless one, up to 1.3 for one that KOs.
    def _foe_weight(self, j: int) -> float:
        worst = 0.0
        for i, me in enumerate(self.board.mine):
            t = self.threats.get((j, i))
            if t and me:
                worst = max(worst, min(1.0, t.hit.mean / max(me.hp, 0.05)))
        foe = self.board.foes[j]
        extra = 0.0
        if foe is not None and any(m in ("trickroom", "tailwind", "spore", "followme", "ragepowder") for m in foe.mon.moves):
            extra = 0.1
        return 0.7 + 0.6 * worst + extra

    def incoming(self, i: int, me: Battler | None = None) -> tuple[float, float]:
        """Expected damage slot i takes this turn (each foe aims at it half the time), and the KO chance."""
        b = self.board
        me = me or b.mine[i]
        if me is None:
            return 0.0, 0.0
        dmg, survive = 0.0, 1.0
        for j, foe in enumerate(b.foes):
            if foe is None or foe.hp <= 0:
                continue
            t = self.threats.get((j, i)) if me is b.mine[i] else threat(foe, me, b)
            if t is None:
                continue
            dmg += 0.5 * min(t.hit.mean, me.hp)
            survive *= 1 - 0.5 * ko_chance(t.hit, me.hp)
        return min(dmg, me.hp), 1 - survive

    def future_value(self, i: int) -> float:
        """What keeping slot i's Pokémon alive one more turn is worth: what it can still hit for."""
        b = self.board
        me = b.mine[i]
        if me is None:
            return 0.0
        best = 0.0
        for f in b.foes:
            if f is not None and f.hp > 0:
                for mv in _attacks(me):
                    best = max(best, min(f.hp, damage(me, f, mv, b.fld, b.foe_side).mean))
        return 0.3 + 0.7 * best

    def p_act(self, i: int, priority: int) -> float:
        """Chance slot i gets to act: not KO'd first by a faster foe, not Faked Out."""
        b = self.board
        me = b.mine[i]
        p = 1.0
        for j, foe in enumerate(b.foes):
            if foe is None or foe.hp <= 0:
                continue
            t = self.threats.get((j, i))
            if t:
                # The foe's best way to KO us before we move: a faster hit or a priority move.
                worst = max((ko_chance(h, me.hp) * (1 - moves_first(me, priority, b.my_side, foe, pri, b.foe_side, b.fld))
                             for h, pri in t.moves), default=0.0)
                p *= 1 - 0.5 * worst
            if b.foe_first_turn[j] and "fakeout" in foe.mon.moves and priority < 3 and fake_out_works(foe, me, b):
                p *= 0.6
        return p

    # -- options -----------------------------------------------------------------------------------

    def options(self, i: int) -> list[Option]:
        slot = self.slots[i]
        out: list[Option] = []
        me = self.board.mine[i]
        for raw in slot.get("options") or []:
            kind = raw.get("type")
            if kind == "pass":
                out.append(Option(i, {"type": "pass"}, "pass", value=-0.5))
            elif kind == "switch":
                sp = raw.get("species")
                if isinstance(sp, str) and sp:
                    out.append(self._switch(i, sp, forced=bool(slot.get("force_switch")) or me is None))
            elif kind == "move":
                out.extend(self._moves(i, raw))
        return out

    def _switch(self, i: int, sp: str, forced: bool) -> Option:
        b = self.board
        opt = Option(i, {"type": "switch", "species": sp}, "switch", switch_to=to_id(sp))
        new = b.bench.get(to_id(sp))
        if new is None:
            opt.value = -0.3 if not forced else 0.0
            return opt
        foes = [f for f in b.foes if f is not None and f.hp > 0]
        off_new = max((min(f.hp, damage(new, f, mv, b.fld, b.foe_side).mean) for f in foes for mv in _attacks(new)),
                      default=0.0)
        inc_new, ko_new = self.incoming(i, new)
        bonus = 0.08 if new.mon.ability_id == "intimidate" and foes else 0.0
        if forced:
            opt.value = 1.0 * off_new - 0.7 * inc_new - 0.4 * ko_new + 0.25 * new.hp + bonus
            return opt
        me = b.mine[i]
        off_me = max((min(f.hp, damage(me, f, mv, b.fld, b.foe_side).mean) for f in foes for mv in _attacks(me)),
                     default=0.0)
        inc_me, ko_me = self.incoming(i)
        opt.value = 0.8 * (inc_me - inc_new) + 0.5 * (ko_me - ko_new) * me.hp + 0.4 * (off_new - off_me) - 0.2 + bonus
        opt.note = f"switch: off {off_me:.2f}->{off_new:.2f}, inc {inc_me:.2f}->{inc_new:.2f}"
        return opt

    def _moves(self, i: int, raw: dict) -> list[Option]:
        b = self.board
        me = b.mine[i]
        mid = to_id(raw.get("move_id"))
        mv = move_data(mid)
        targets = [t for t in raw.get("targets") or [] if isinstance(t, int) and not isinstance(t, bool)]
        names = {t.get("target"): t for t in raw.get("target_options") or [] if isinstance(t, dict)}
        if me is None or mv is None:
            # We can't judge it; offer it plainly, valued low.
            send = {"type": "move", "move_id": raw.get("move_id")}
            if targets:
                send["target"] = next((t for t in targets if t > 0), targets[0])
            return [Option(i, send, "move", move_id=mid, value=0.05)]
        pri = int(mv["pri"])
        if mv["cat"] == "Status" and me.mon.ability_id == "prankster":
            pri += 1
        if mid == "grassyglide" and b.fld.terrain_id == "grassyterrain":
            pri += 1
        p_act = self.p_act(i, pri)
        foe_targets = [t for t in targets if t > 0]
        own_targets = [t for t in targets if t < 0]
        opts: list[Option] = []
        if mv["cat"] != "Status":
            if mv["target"] in ("allAdjacentFoes", "allAdjacent", "all") or not targets or targets == [0]:
                opts.append(self._attack(i, raw, mv, None, p_act))
            else:
                live = [t for t in foe_targets if self._foe_at(t) is not None]
                for t in live or foe_targets[:1]:
                    opts.append(self._attack(i, raw, mv, t, p_act))
                if not foe_targets and own_targets:
                    # Only our own side to hit: legal, almost never right.
                    o = Option(i, {"type": "move", "move_id": raw.get("move_id"), "target": own_targets[0]}, "move",
                               move_id=mid, mv=mv, value=-1.0)
                    opts.append(o)
        else:
            opts.extend(self._status(i, raw, mv, targets, p_act))
        return opts

    def _foe_at(self, target: int) -> Battler | None:
        j = target - 1
        if 0 <= j < 2:
            f = self.board.foes[j]
            return f if f is not None and f.hp > 0 else None
        return None

    def _attack(self, i: int, raw: dict, mv: dict, target: int | None, p_act: float) -> Option:
        b = self.board
        me = b.mine[i]
        mid = to_id(mv["name"])
        send = {"type": "move", "move_id": raw.get("move_id")}
        targets = raw.get("targets") or []
        if target is not None:
            send["target"] = target
        elif targets:
            send["target"] = 0 if 0 in targets else targets[0]
        opt = Option(i, send, "move", move_id=mid, mv=mv, p_act=p_act)
        spread_kind = mv["target"] in ("allAdjacentFoes", "allAdjacent", "all")
        live_foes = [j for j, f in enumerate(b.foes) if f is not None and f.hp > 0]
        spread = spread_kind and (len(live_foes) + (1 if mv["target"] == "allAdjacent" and b.mine[1 - i] else 0)) > 1
        if spread_kind:
            for j in live_foes:
                opt.hits[j] = damage(me, b.foes[j], mv, b.fld, b.foe_side, spread=spread)
            ally = b.mine[1 - i]
            if mv["target"] == "allAdjacent" and ally is not None and ally.hp > 0:
                opt.ally_hit = damage(me, ally, mv, b.fld, b.my_side, spread=spread)
        elif target is not None and target > 0:
            j = target - 1
            if self._foe_at(target) is not None:
                opt.hits[j] = damage(me, b.foes[j], mv, b.fld, b.foe_side, spread=False)
        else:
            # A single-target move with no listed target (random foe): spread its value over both.
            for j in live_foes:
                h = damage(me, b.foes[j], mv, b.fld, b.foe_side, spread=False)
                opt.hits[j] = Hit(h.expected / len(live_foes), h.low / len(live_foes), h.high / len(live_foes),
                                  h.accuracy, h.effectiveness)
        # Side effects of attacking moves.
        v = 0.0
        if mv.get("recharge"):
            v -= 0.25
        if mv.get("charge") and not (mid == "solarbeam" and b.fld.weather_id == "sunnyday") and mid != "electroshot":
            v -= 0.35
        if mid == "electroshot" and b.fld.weather_id != "raindance" and me.item != "powerherb":
            v -= 0.2
        if mv.get("selfdestruct"):
            v -= 0.5 + me.hp
        if mv.get("recoil"):
            v -= 0.4 * mv["recoil"] * sum(h.mean for h in opt.hits.values())
        if mv.get("drain"):
            v += 0.5 * min(1 - me.hp, mv["drain"] * sum(h.mean for h in opt.hits.values()) * 0.5)
        sb = mv.get("selfBoosts") or {}
        drops = -sum(x for x in sb.values() if x < 0)
        if drops:
            v -= 0.04 * drops
        sec = mv.get("sec")
        if sec:
            chance = sec["chance"] / 100
            for j in opt.hits:
                foe = b.foes[j]
                if sec.get("boosts"):
                    v += chance * 0.06 * -sum(x for x in sec["boosts"].values() if x < 0)
                    if "spe" in sec["boosts"] and not b.fld.trick_room and foe is not None:
                        if any(m and effective_speed(m, b.my_side) < effective_speed(foe, b.foe_side) for m in b.mine):
                            v += chance * 0.1
                if sec.get("status") and foe is not None and not foe.status:
                    v += chance * 0.15
                if sec.get("volatile") == "flinch" and foe is not None:
                    if moves_first(me, int(mv["pri"]), b.my_side, foe, 0, b.foe_side, b.fld) > 0.5:
                        v += chance * 0.15
        if mid in PIVOT and b.bench:
            v += 0.05
        if mid == "knockoff":
            v += 0.05
        if mid in FAKE_OUT:
            if not b.first_turn[i]:
                return Option(i, send, "move", move_id=mid, mv=mv, value=-0.6, note="fake out fails after turn 1")
            if target is not None and target > 0:
                j = target - 1
                foe = b.foes[j]
                if foe is not None and opt.hits.get(j, NO_HIT).effectiveness > 0 and fake_out_works(foe, me, b, flinch=True):
                    opt.flinch = j
                    opt.flinch_value = (1 - self.foe_protect[j]) * self._action_value(j)
        opt.value = v * p_act
        return opt

    def _action_value(self, j: int) -> float:
        """What foe j's action this turn is worth to it (what a flinch takes away)."""
        b = self.board
        foe = b.foes[j]
        if foe is None:
            return 0.0
        v = 0.15
        for i, me in enumerate(b.mine):
            t = self.threats.get((j, i))
            if t and me:
                v += 0.5 * (min(t.hit.mean, me.hp) + 0.5 * ko_chance(t.hit, me.hp))
        if "trickroom" in foe.mon.moves and not b.fld.trick_room:
            v += 0.3
        if "tailwind" in foe.mon.moves and not b.foe_side.tailwind:
            v += 0.2
        if any(m in SETUP for m in foe.mon.moves):
            v += 0.1
        return v

    def _status(self, i: int, raw: dict, mv: dict, targets: list[int], p_act: float) -> list[Option]:
        b = self.board
        me = b.mine[i]
        mid = to_id(mv["name"])
        ally = b.mine[1 - i]
        mk = lambda target, value, **kw: Option(  # noqa: E731
            i, _send(raw, target), "move", move_id=mid, mv=mv, value=value, p_act=p_act, **kw)
        inc, ko = self.incoming(i)
        foes = [(j, f) for j, f in enumerate(b.foes) if f is not None and f.hp > 0]

        if mid in PROTECT or mid in ("wideguard", "quickguard", "maxguard"):
            if mid in PROTECT:
                chained = b.last_move[i] in PROTECT
                ko_first = 1 - self.p_act(i, 0)
                v = 0.25 * inc + 0.5 * ko_first * self.future_value(i)
                if any(b.foe_first_turn[j] and "fakeout" in f.mon.moves for j, f in foes):
                    v += 0.1
                v = v * (0.33 if chained else 1) - 0.05
                return [mk(0, v, protect=True, note=f"protect: inc {inc:.2f} ko-first {ko_first:.2f}")]
            if mid == "wideguard":
                spread = sum(0.3 for _, f in foes for m in f.mon.moves
                             if (move_data(m) or {}).get("target") in ("allAdjacentFoes", "allAdjacent")
                             and (move_data(m) or {}).get("cat") != "Status")
                return [mk(0, min(0.6, spread) - 0.05)]
            return [mk(0, 0.0)]
        if mid in REDIRECT:
            if ally is None or ally.hp <= 0:
                return [mk(0, -0.3)]
            inc_ally, ko_ally = self.incoming(1 - i)
            v = 0.6 * inc_ally + 0.4 * ko_ally * ally.hp - 0.5 * inc - 0.1
            if mid == "ragepowder":
                v *= 0.7  # Grass types and Overcoat ignore it
            return [mk(0, v)]
        if mid == "helpinghand":
            target = next((t for t in targets if t < 0), 0) if targets else None
            return [mk(target, -0.02, helping=True)]
        if mid == "tailwind":
            if b.my_side.tailwind:
                return [mk(0, -0.5)]
            gain = 0.0
            for m in list(b.mine) + list(b.bench.values()):
                if m is None or m.hp <= 0:
                    continue
                s = effective_speed(m)
                for _, f in foes:
                    fs = effective_speed(f, b.foe_side)
                    if s < fs < 2 * s:
                        gain += 0.15
            return [mk(0, (min(0.75, gain) + 0.1) * (0.3 if b.fld.trick_room else 1))]
        if mid == "trickroom":
            slow = fast = 0.0
            for m in list(b.mine) + list(b.bench.values()):
                if m is None or m.hp <= 0:
                    continue
                for _, f in foes:
                    if effective_speed(m, b.my_side) < effective_speed(f, b.foe_side):
                        slow += 0.15
                    else:
                        fast += 0.15
            v = slow - fast
            return [mk(0, -v if b.fld.trick_room else v + 0.05)]
        if mid in SCREENS:
            if getattr(b.my_side, SCREENS[mid]) or (mid == "auroraveil" and b.fld.weather_id != "snow"):
                return [mk(0, -0.4)]
            return [mk(0, 0.3)]
        if mid in WEATHER_MOVES or mid in TERRAIN_MOVES:
            want = WEATHER_MOVES.get(mid, mid)
            have = b.fld.weather_id if mid in WEATHER_MOVES else b.fld.terrain_id
            return [mk(0, -0.3 if have == want else 0.12)]
        if mid in SETUP:
            boosted = sum(max(0, v) for v in me.boosts.values())
            safe = 1 - min(1.0, inc / max(me.hp, 0.05))
            v = (0.45 * safe - 0.1) if boosted < 2 else -0.2
            if mid == "bellydrum" and me.hp <= 0.5:
                v = -1
            if mid == "shellsmash":
                v += 0.1 * safe
            return [mk(0, v)]
        if mid in RECOVER:
            missing = 1 - me.hp
            v = 0.9 * min(0.5, missing) - 0.1 if me.hp < 0.6 else -0.2
            if mid in ("junglehealing", "lifedew", "lunarblessing") and ally is not None:
                v += 0.5 * min(0.25, 1 - ally.hp)
            return [mk(0, v)]
        if mid in ("haze", "clearsmog"):
            boosts = sum(max(0, x) for _, f in foes for x in f.boosts.values())
            return [mk(0, 0.12 * boosts - 0.1)]

        # Moves aimed at a foe.
        out = []
        foe_targets = [t for t in targets if t > 0] or ([1, 2] if not targets or targets == [0] else [])
        for t in foe_targets:
            j = t - 1
            foe = b.foes[j] if 0 <= j < 2 else None
            if foe is None or foe.hp <= 0:
                continue
            v = self._status_on_foe(i, mid, mv, foe, j)
            if v is None:
                continue
            send_t = t if any(x > 0 for x in targets) else (0 if 0 in targets else None)
            out.append(mk(send_t, v * accuracy_of(me, mv, b.fld)))
            if send_t is None or send_t == 0:
                break
        if out:
            return out
        # Anything else (ally support, unknown effects): a small positive value if it targets nothing harmful.
        own = [t for t in targets if t < 0]
        if mid in ("pollenpuff",):
            return []
        if targets and not any(t > 0 for t in targets) and own and 0 not in targets:
            return [mk(own[0] if mv["target"] != "adjacentAlly" else own[0], 0.02)]
        return [mk(_first_target(targets), 0.02)]

    def _status_on_foe(self, i: int, mid: str, mv: dict, foe: Battler, j: int) -> float | None:
        b = self.board
        me = b.mine[i]
        danger = self.foe_weight[j] - 0.7
        mtype = mv["type"]
        if mid in SLEEP:
            if foe.status or (("powder" in mid or mid == "spore") and ("Grass" in foe.mon.types or foe.mon.ability_id == "overcoat" or foe.item == "safetygoggles")):
                return -0.3
            if b.fld.terrain_id in ("electricterrain", "mistyterrain") and grounded(foe):
                return -0.3
            if foe.mon.ability_id in ("insomnia", "vitalspirit", "sweetveil", "purifyingsalt", "comatose"):
                return -0.3
            return (0.35 if mid == "yawn" else 0.7) * (0.5 + danger)
        if mid == "willowisp":
            if foe.status or "Fire" in foe.mon.types or foe.mon.ability_id in ("waterveil", "waterbubble", "flashfire", "wellbakedbody", "guts"):
                return -0.3
            physical = foe.mon.stats["atk"] > foe.mon.stats["spa"]
            return 0.5 * (0.4 + danger) if physical else 0.08
        if mid in ("thunderwave", "glare", "stunspore", "nuzzle"):
            if foe.status or (mid == "thunderwave" and ("Ground" in foe.mon.types or "Electric" in foe.mon.types)):
                return -0.3
            if mid == "stunspore" and "Grass" in foe.mon.types:
                return -0.3
            faster = any(m and effective_speed(m, b.my_side) < effective_speed(foe, b.foe_side) for m in b.mine)
            return 0.15 + (0.2 if faster and not b.fld.trick_room else 0)
        if mid in ("toxic", "poisonpowder"):
            if foe.status or "Steel" in foe.mon.types or "Poison" in foe.mon.types:
                return -0.3
            return 0.12
        if mid == "taunt":
            stat = [m for m in foe.mon.moves if (move_data(m) or {}).get("cat") == "Status" and m not in PROTECT]
            v = 0.08 * len(stat)
            if "trickroom" in foe.mon.moves and not b.fld.trick_room:
                v += 0.3
            if "tailwind" in foe.mon.moves and not b.foe_side.tailwind:
                v += 0.15
            return v - 0.05
        if mid == "encore":
            return 0.05
        if mid in ("faketears", "metalsound", "screech", "charm", "featherdance", "scaryface", "tickle",
                   "growl", "leer", "eerieimpulse", "captivate", "confide", "nobleroar", "partingshot",
                   "babydolleyes", "sweetscent", "stringshot", "cottonspore"):
            v = 0.12 + 0.1 * danger
            if mid == "partingshot":
                v += 0.15
            return v
        if mid in ("spore",):
            return None
        if mid in ("leechseed",):
            return -0.3 if "Grass" in foe.mon.types or foe.status == "seeded" else 0.12
        if mid in ("perishsong", "destinybond", "substitute", "transform", "skillswap", "entrainment",
                   "worryseed", "simplebeam", "trick", "switcheroo", "imprison", "disable", "torment"):
            return 0.03
        if mtype and is_immune(me, foe, {**mv, "cat": "Physical"}, move_type(me, mv, b.fld)) and mv["cat"] != "Status":
            return None
        return 0.03

    # -- choosing ------------------------------------------------------------------------------------

    def score_pair(self, a: Option, b_: Option) -> float:
        b = self.board
        if a.kind == "switch" and b_.kind == "switch" and a.switch_to == b_.switch_to:
            return float("-inf")
        total = a.value + b_.value
        opts = (a, b_)
        # Helping Hand boosts the partner's hits; it's wasted if the partner doesn't attack.
        boost = [1.0, 1.0]
        for k, o in enumerate(opts):
            if o.helping:
                partner = opts[1 - k]
                if partner.hits:
                    boost[1 - k] = 1.5
                else:
                    total -= 0.2
        for j, foe in enumerate(b.foes):
            if foe is None or foe.hp <= 0:
                continue
            parts = []
            for k, o in enumerate(opts):
                h = o.hits.get(j)
                if h is None or h.expected <= 0:
                    continue
                p = o.p_act * h.accuracy * (1 - self.foe_protect[j])
                parts.append((p, h.expected * boost[k], h.low * boost[k], h.high * boost[k]))
            if not parts:
                continue
            exp_dmg, exp_ko = 0.0, 0.0
            for mask in itertools.product((0, 1), repeat=len(parts)):
                prob, mean, low, high = 1.0, 0.0, 0.0, 0.0
                for on, (p, e, lo, hi) in zip(mask, parts):
                    prob *= p if on else 1 - p
                    if on:
                        mean, low, high = mean + e, low + lo, high + hi
                if prob <= 0 or mean <= 0:
                    continue
                exp_dmg += prob * min(mean, foe.hp)
                exp_ko += prob * ko_chance(Hit(mean, low, high, 1.0, 1.0), foe.hp)
            w = self.foe_weight[j]
            total += w * exp_dmg + exp_ko * (0.35 + 0.5 * w)
        # Spread moves that hit our own partner (unless it protects or switches).
        for k, o in enumerate(opts):
            partner = opts[1 - k]
            ally = b.mine[1 - k]
            if o.ally_hit is not None and ally is not None and not partner.protect and partner.kind != "switch":
                h = o.ally_hit
                p = o.p_act * h.accuracy
                total -= p * (1.1 * min(h.mean, ally.hp) + 0.8 * ko_chance(h, ally.hp))
        # Two Fake Outs on one foe: the second does nothing.
        flinches = [(o.flinch, o.flinch_value) for o in opts if o.flinch is not None]
        if len(flinches) == 2 and flinches[0][0] == flinches[1][0]:
            total += max(flinches[0][1], flinches[1][1])
        else:
            total += sum(v for _, v in flinches)
        return total

    def choose(self) -> tuple[dict, dict, float, list[str]]:
        per_slot = []
        for i in range(2):
            opts = self.options(i)
            if not opts:
                raise ValueError(f"slot {i}: no options")
            per_slot.append(opts)
        best, best_pair = float("-inf"), None
        both_pass_ok = all(all(o.kind == "pass" for o in opts) for opts in per_slot)
        for a in per_slot[0]:
            for c in per_slot[1]:
                if a.kind == "pass" and c.kind == "pass" and not both_pass_ok:
                    continue
                s = self.score_pair(a, c)
                if s > best:
                    best, best_pair = s, (a, c)
        if best_pair is None:
            raise ValueError("no legal pair of slot choices")
        a, c = best_pair
        return a.send, c.send, best, [a.note, c.note]


def _attacks(b: Battler) -> list[dict]:
    out = [mv for mv in (move_data(m) for m in b.mon.moves) if mv and mv["cat"] != "Status"]
    return out or foe_moves(b)


def accuracy_of(me: Battler, mv: dict, fld: Field) -> float:
    from .damage import accuracy

    return accuracy(me, mv, fld)


def fake_out_works(foe: Battler, me: Battler, board: Board, flinch: bool = False) -> bool:
    """Can ``me`` be flinched by Fake Out from ``foe`` (or, with ``flinch``, ``foe`` by ours)?"""
    target = foe if flinch else me
    if "Ghost" in target.mon.types:
        return False
    if target.mon.ability_id in NO_FLINCH_ABILITIES or target.item == "covertcloak":
        return False
    if target.mon.ability_id in ("armortail", "queenlymajesty", "dazzling"):
        return False
    if board.fld.terrain_id == "psychicterrain" and grounded(target):
        return False
    return True


def _send(raw: dict, target: int | None) -> dict:
    send = {"type": "move", "move_id": raw.get("move_id")}
    targets = raw.get("targets") or []
    if targets:
        if target is None or target not in targets:
            target = _first_target(targets)
        send["target"] = target
    return send


def _first_target(targets: list[int]) -> int | None:
    if not targets:
        return None
    return next((t for t in targets if t > 0), 0 if 0 in targets else targets[0])


# -- reading the board ---------------------------------------------------------------------------

def _hp(summary: dict | None) -> float:
    if not isinstance(summary, dict):
        return 1.0
    if summary.get("fainted"):
        return 0.0
    frac = summary.get("current_hp_fraction")
    if isinstance(frac, (int, float)):
        return max(0.0, min(1.0, float(frac if frac <= 1.0 else frac / 100)))
    cur, mx = summary.get("current_hp"), summary.get("max_hp")
    if isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
        return max(0.0, min(1.0, cur / mx))
    cond = summary.get("condition")
    if isinstance(cond, str):
        if "fnt" in cond:
            return 0.0
        try:
            cur_s, mx_s = cond.split()[0].split("/")
            return float(cur_s) / float(mx_s)
        except (ValueError, IndexError):
            pass
    return 1.0


def _status(summary: dict | None) -> str:
    if not isinstance(summary, dict):
        return ""
    st = to_id(summary.get("status") or "")
    return {"burn": "brn", "burned": "brn", "paralysis": "par", "paralyzed": "par", "sleep": "slp", "asleep": "slp",
            "poison": "psn", "poisoned": "psn", "toxic": "tox", "badlypoisoned": "tox", "freeze": "frz",
            "frozen": "frz", "fnt": "", "none": ""}.get(st, st)


def _boosts(summary: dict | None) -> dict[str, int]:
    if not isinstance(summary, dict) or not isinstance(summary.get("boosts"), dict):
        return {}
    out = {}
    for k, v in summary["boosts"].items():
        try:
            out[to_id(k)] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def summary_species(summary: object, key: object = None) -> str:
    if isinstance(summary, dict):
        for k in ("species", "name", "base_species"):
            if summary.get(k):
                sp = species_data(summary[k])
                if sp:
                    return to_id(sp["name"])
        if summary.get("ident"):
            sp = species_data(summary["ident"])
            if sp:
                return to_id(sp["name"])
    if key is not None:
        sp = species_data(key)
        if sp:
            return to_id(sp["name"])
    if isinstance(summary, str):
        sp = species_data(summary)
        if sp:
            return to_id(sp["name"])
    return ""


def _summaries(container: object) -> dict[str, dict]:
    """``team``/``opponent_team`` as {species id: summary}, whether it's a dict or a list."""
    out: dict[str, dict] = {}
    items = container.items() if isinstance(container, dict) else enumerate(container or [])
    for key, summary in items:
        if isinstance(summary, dict):
            sid = summary_species(summary, key if isinstance(key, str) else None)
            if sid:
                out[sid] = summary
    return out


def _conditions(value: object) -> set[str]:
    # The platform sends poke-env's maps, {"GRASSY_TERRAIN": 0}: a field or side condition is up
    # while its key is there, and the value is the turn it started (or its layers), so 0 counts.
    if isinstance(value, dict):
        return {to_id(k) for k, v in value.items() if v is not False and v is not None}
    if isinstance(value, (list, tuple, set)):
        return {to_id(x.get("name") if isinstance(x, dict) else x) for x in value}
    if isinstance(value, str) and value:
        return {to_id(value)}
    return set()


def make_battler(sid: str, summary: dict | None, cards: dict[str, Mon]) -> Battler | None:
    mon = cards.get(sid)
    if mon is None:
        s = summary if isinstance(summary, dict) else {}
        mon = guess_mon(sid, moves=[m if isinstance(m, str) else (m or {}).get("id", "") for m in s.get("moves") or []],
                        ability=str(s.get("ability") or ""), item=str(s.get("item") or ""))
        if mon is None:
            return None
    b = Battler(mon, hp=_hp(summary), status=_status(summary), boosts=_boosts(summary))
    if isinstance(summary, dict) and "item" in summary and summary.get("revealed", True):
        item = summary.get("item")
        if item in ("", None) and summary.get("item_known", False):
            b.item_known_gone = True
    return b


def read_board(obs: dict, slots: list[dict], my_cards: dict[str, Mon], foe_cards: dict[str, Mon]) -> Board:
    obs = obs if isinstance(obs, dict) else {}
    my_sum = _summaries(obs.get("team"))
    foe_sum = _summaries(obs.get("opponent_team"))
    actives = obs.get("active_pokemon")
    actives = actives if isinstance(actives, list) else [actives] if isinstance(actives, dict) else []
    foe_actives = obs.get("opponent_active_pokemon")
    foe_actives = foe_actives if isinstance(foe_actives, list) else [foe_actives] if isinstance(foe_actives, dict) else []
    for s in actives:
        sid = summary_species(s)
        if sid and isinstance(s, dict):
            my_sum.setdefault(sid, s)
            my_sum[sid] = {**my_sum[sid], **s}
    for s in foe_actives:
        sid = summary_species(s)
        if sid and isinstance(s, dict):
            foe_sum[sid] = {**foe_sum.get(sid, {}), **s}

    mine: list[Battler | None] = [None, None]
    for i, slot in enumerate(slots):
        sid = summary_species(slot.get("active"))
        if not sid and i < len(actives):
            sid = summary_species(actives[i])
        if sid and not slot.get("force_switch"):
            summary = my_sum.get(sid)
            if isinstance(slot.get("active"), dict):
                summary = {**(summary or {}), **{k: v for k, v in slot["active"].items() if v is not None}}
            b = make_battler(sid, summary, my_cards)
            if b is not None and b.hp > 0:
                mine[i] = b

    # Foe positions: the targets' own names when the template gives them.
    foe_ids: list[str] = ["", ""]
    for slot in slots:
        for o in slot.get("options") or []:
            for t in o.get("target_options") or []:
                if isinstance(t, dict) and t.get("side") == "opponent" and t.get("target") in (1, 2) and t.get("species"):
                    foe_ids[t["target"] - 1] = summary_species(t.get("species")) or foe_ids[t["target"] - 1]
    for j in range(2):
        if not foe_ids[j] and j < len(foe_actives):
            foe_ids[j] = summary_species(foe_actives[j])
    foes: list[Battler | None] = [None, None]
    for j, sid in enumerate(foe_ids):
        if sid:
            b = make_battler(sid, foe_sum.get(sid), foe_cards)
            if b is not None and b.hp > 0:
                foes[j] = b

    bench: dict[str, Battler] = {}
    for slot in slots:
        for o in slot.get("options") or []:
            if o.get("type") == "switch" and o.get("species"):
                sid = summary_species(o["species"]) or to_id(o["species"])
                if sid not in bench:
                    b = make_battler(sid, my_sum.get(sid) or (o if isinstance(o, dict) else None), my_cards)
                    if b is not None:
                        bench[sid] = b

    fld = Field()
    weather = obs.get("weather")
    if isinstance(weather, dict):
        weather = weather.get("name") or next(iter(weather), "")
    fld.weather = str(weather or "")
    conds = _conditions(obs.get("fields")) | _conditions(obs.get("field"))
    for c in conds:
        if "terrain" in c:
            fld.terrain = c
        if c == "trickroom":
            fld.trick_room = True
    if isinstance(obs.get("terrain"), str):
        fld.terrain = obs["terrain"]
    if obs.get("trick_room"):
        fld.trick_room = True
    return Board(mine=mine, foes=foes, my_side=_side(obs.get("side_conditions")),
                 foe_side=_side(obs.get("opponent_side_conditions")), fld=fld, bench=bench)


def _side(value: object) -> Side:
    c = _conditions(value)
    return Side(reflect="reflect" in c, light_screen="lightscreen" in c, aurora_veil="auroraveil" in c,
                tailwind="tailwind" in c)


def known_species(name: object) -> bool:
    return species_data(name) is not None


__all__ = ["Board", "TurnPlanner", "read_board", "summary_species", "Mon"]
