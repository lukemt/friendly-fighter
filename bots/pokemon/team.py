"""Team-level judgement: how one Pokémon fares against another, what a card is worth in a draft,
and which 4 to bring (and which 2 to lead) at Team Preview."""

from __future__ import annotations

import itertools
from functools import lru_cache

from .damage import Battler, Field, damage
from .dex import Mon, effectiveness, move as move_data

FAKE_OUT = {"fakeout"}
PROTECT = {"protect", "detect", "spikyshield", "kingsshield", "banefulbunker", "silktrap", "burningbulwark", "obstruct"}
REDIRECT = {"followme", "ragepowder", "spotlight"}
SPEED_CONTROL = {"tailwind", "icywind", "electroweb", "thunderwave", "glare", "bulldoze", "rocktomb", "scaryface"}
SLEEP = {"spore", "sleeppowder", "hypnosis", "lovelykiss", "darkvoid", "yawn", "sing", "grasswhistle"}
SUPPORT = {
    "helpinghand": 0.06, "wideguard": 0.10, "quickguard": 0.03, "willowisp": 0.08, "partingshot": 0.10,
    "snarl": 0.06, "encore": 0.06, "taunt": 0.05, "reflect": 0.07, "lightscreen": 0.07, "auroraveil": 0.12,
    "allyswitch": 0.03, "pollenpuff": 0.04, "lifedew": 0.03, "junglehealing": 0.04, "coaching": 0.03,
    "haze": 0.03, "clearsmog": 0.03, "lunarblessing": 0.03, "decorate": 0.04, "instruct": 0.03,
    "nuzzle": 0.04, "faketears": 0.04, "screech": 0.02, "imprison": 0.02, "sunnyday": 0.04, "raindance": 0.04,
}
SUPPORT_ABILITIES = {
    "intimidate": 0.22, "drought": 0.10, "drizzle": 0.10, "sandstream": 0.06, "snowwarning": 0.06,
    "electricsurge": 0.08, "psychicsurge": 0.10, "grassysurge": 0.08, "mistysurge": 0.06, "orichalcumpulse": 0.10,
    "hadronengine": 0.10, "friendguard": 0.05, "prankster": 0.08, "innerfocus": 0.04, "regenerator": 0.04,
    "armortail": 0.06, "queenlymajesty": 0.06, "dazzling": 0.06, "goodasgold": 0.06, "unaware": 0.03,
    "telepathy": 0.03, "levitate": 0.02, "multiscale": 0.04, "commander": 0.02,
}


def damaging_moves(mon: Mon) -> list[dict]:
    """Its damaging moves; for a Pokémon whose moves we don't know, an 85-power STAB move per type."""
    out = []
    for mid in mon.moves:
        mv = move_data(mid)
        if mv and mv["cat"] != "Status":
            out.append(mv)
    if not out and not mon.known_set:
        out = stab_placeholders(mon)
    return out


def stab_placeholders(mon: Mon) -> list[dict]:
    physical = mon.stats["atk"] >= mon.stats["spa"]
    return [{"name": f"STAB {t}", "type": t, "cat": "Physical" if physical else "Special", "bp": 85, "acc": 100,
             "pri": 0, "target": "normal"} for t in mon.types]


@lru_cache(maxsize=20000)
def _best_hit_cached(attacker: Mon, defender: Mon) -> tuple[float, str]:
    a, d = Battler(attacker), Battler(defender)
    best, best_id = 0.0, ""
    for mv in damaging_moves(attacker):
        h = damage(a, d, mv, Field(), spread=False)
        v = h.mean
        if mv.get("recharge") or (mv.get("charge") and mv["name"] not in ("Electro Shot",)):
            v *= 0.5
        if mv["target"] in ("allAdjacentFoes", "allAdjacent"):
            v *= 0.75 * 1.35  # weaker per target, but it hits both
        if v > best:
            best, best_id = v, mv["name"]
    return best, best_id


def best_hit(attacker: Mon, defender: Mon) -> float:
    """The most damage ``attacker`` does to ``defender`` in one move (fraction of max HP, expected)."""
    return _best_hit_cached(attacker, defender)[0]


def _speed_edge(a: Mon, d: Mon) -> float:
    sa, sd = a.stats["spe"], d.stats["spe"]
    if sa > sd:
        return 1.0
    if sa < sd:
        return -1.0
    return 0.0


def duel(a: Mon, d: Mon) -> float:
    """How well ``a`` does against ``d``, about -1..1: damage dealt vs taken, and who hits first."""
    off, taken = min(1.2, best_hit(a, d)), min(1.2, best_hit(d, a))
    score = off - taken
    edge = _speed_edge(a, d)
    if off >= 1.0 and (edge > 0 or taken < 1.0):
        score += 0.25  # one-shots it, and lives to do so
    if taken >= 1.0 and (edge < 0 or off < 1.0):
        score -= 0.25
    score += 0.08 * edge
    return score


def support_value(mon: Mon, team: list[Mon] = ()) -> float:
    """Doubles staples on this Pokémon, with less credit for what the team already has."""
    others = [m for m in team if m is not mon]

    def has(group: set[str], m: Mon) -> bool:
        return any(x in group for x in m.moves)

    v = 0.0
    if has(FAKE_OUT, mon):
        v += 0.22 * (0.4 if any(has(FAKE_OUT, m) for m in others) else 1)
    if has(PROTECT, mon):
        v += 0.05
    if has(REDIRECT, mon):
        v += 0.18 * (0.3 if any(has(REDIRECT, m) for m in others) else 1)
    if "tailwind" in mon.moves:
        v += 0.18 * (0.4 if any("tailwind" in m.moves for m in others) else 1)
    elif has(SPEED_CONTROL, mon):
        v += 0.07
    if "trickroom" in mon.moves:
        slow = sum(1 for m in others if m.base["spe"] <= 60)
        v += 0.06 + 0.06 * slow
    if has(SLEEP, mon):
        v += 0.18 if any(x in ("spore", "sleeppowder", "lovelykiss", "darkvoid") for x in mon.moves) else 0.06
    for mid in mon.moves:
        v += SUPPORT.get(mid, 0.0)
        mv = move_data(mid)
        if mv and mv["cat"] != "Status":
            if mv["target"] in ("allAdjacentFoes", "allAdjacent") and mv["bp"] >= 60 and mv["type"] in mon.types:
                v += 0.08
            if mv["pri"] > 0 and mv["bp"] >= 40 and mid not in FAKE_OUT:
                v += 0.03
    ab = SUPPORT_ABILITIES.get(mon.ability_id, 0.0)
    if mon.ability_id == "intimidate" and any(m.ability_id == "intimidate" for m in others):
        ab *= 0.5
    v += ab
    if mon.base["spe"] <= 60 and any("trickroom" in m.moves for m in others):
        v += 0.08
    return v


def shared_weakness_penalty(mon: Mon, team: list[Mon]) -> float:
    """Penalty for stacking weaknesses: types that would hit 3+ of the team super-effectively."""
    from .dex import dex

    pen = 0.0
    for t in dex()["types"]:
        if t == "Stellar":
            continue
        if effectiveness(t, mon.types) <= 1:
            continue
        weak = sum(1 for m in team if effectiveness(t, m.types) > 1)
        resist = sum(1 for m in team if effectiveness(t, m.types) < 1)
        if weak >= 2 and resist <= weak - 1:
            pen += 0.04 * (weak - 1)
    return pen


def card_value(mon: Mon, team: list[Mon], foes: list[Mon]) -> float:
    """What ``mon`` adds to ``team``, judged against the Pokémon it would face (``foes``)."""
    if foes:
        solo = sum(duel(mon, f) for f in foes) / len(foes)
        # Answers to the foes the team has no answer for yet count extra.
        holes = 0.0
        for f in foes:
            have = max((duel(m, f) for m in team), default=-1.0)
            holes += max(0.0, duel(mon, f) - max(have, 0.0))
        solo += 0.5 * holes / len(foes)
    else:
        solo = 0.0
    bst = sum(mon.base.values())
    return solo + support_value(mon, team + [mon]) - shared_weakness_penalty(mon, team) + 0.15 * (bst - 500) / 100


def choose_draft(options: dict[str, Mon], mine: list[Mon], theirs: list[Mon], pool: dict[str, Mon],
                 deny: float = 0.45) -> str:
    """The card id to pick: our gain plus part of the opponent's (denial)."""
    best_id, best = None, float("-inf")
    for cid, mon in options.items():
        rest = [m for k, m in pool.items() if k != cid]
        foes_now = theirs + rest
        mine_v = card_value(mon, mine, theirs + rest if not theirs else theirs * 2 + rest)
        their_v = card_value(mon, theirs, mine + [m for m in rest if m not in mine]) if foes_now else 0.0
        score = mine_v + deny * their_v
        if score > best:
            best_id, best = cid, score
    return best_id if best_id is not None else next(iter(options))


def choose_lineup(mine: list[Mon], theirs: list[Mon]) -> tuple[list[int], list[int]]:
    """Indices into ``mine``: the 4 to bring and the 2 of them to lead."""
    n = len(mine)
    if n <= 4:
        bring = list(range(n))
        return bring, bring[:2]
    m = [[duel(a, d) for d in theirs] for a in mine] if theirs else [[0.0] for _ in mine]
    # The opponent likely brings what does best against us.
    if theirs:
        their_score = [sum(duel(d, a) for a in mine) / n + support_value(d, theirs) for d in theirs]
        order = sorted(range(len(theirs)), key=lambda j: -their_score[j])
        weight = [0.0] * len(theirs)
        for rank, j in enumerate(order):
            weight[j] = 1.0 if rank < 4 else 0.45
    else:
        weight = [1.0]
    wsum = sum(weight) or 1.0

    def mean_vs(i: int) -> float:
        return sum(weight[j] * m[i][j] for j in range(len(weight))) / wsum

    best_bring, best_v = None, float("-inf")
    for combo in itertools.combinations(range(n), 4):
        team = [mine[i] for i in combo]
        v = sum(mean_vs(i) for i in combo) / 4
        v += 0.8 * sum(weight[j] * max(m[i][j] for i in combo) for j in range(len(weight))) / wsum
        v += 0.6 * sum(support_value(mine[i], team) for i in combo) / 4
        v -= sum(shared_weakness_penalty(mine[i], [x for x in team if x is not mine[i]]) for i in combo) / 4
        if v > best_v:
            best_bring, best_v = list(combo), v

    bring = best_bring or list(range(4))
    best_leads, best_l = None, float("-inf")
    for a, b in itertools.combinations(bring, 2):
        pair = [mine[a], mine[b]]
        v = mean_vs(a) + mean_vs(b)
        v += 0.25 * sum(weight[j] * max(m[a][j], m[b][j]) for j in range(len(weight))) / wsum
        v += lead_synergy(pair, [mine[i] for i in bring if i not in (a, b)])
        if v > best_l:
            best_leads, best_l = [a, b], v
    return bring, best_leads or bring[:2]


def lead_synergy(pair: list[Mon], back: list[Mon]) -> float:
    a, b = pair
    v = 0.0
    for x, y in ((a, b), (b, a)):
        if any(mv in FAKE_OUT for mv in x.moves):
            v += 0.12 + 0.05 * (best_offense(y) > 0)
        if any(mv in REDIRECT for mv in x.moves):
            v += 0.08
        if "tailwind" in x.moves:
            v += 0.10
        if "trickroom" in x.moves:
            v += 0.05 + 0.08 * (y.base["spe"] <= 60) + 0.04 * sum(1 for m in back if m.base["spe"] <= 60)
        if x.ability_id == "intimidate":
            v += 0.08
        if x.ability_id in ("drought", "drizzle", "sandstream", "snowwarning", "electricsurge", "psychicsurge",
                            "grassysurge", "mistysurge", "orichalcumpulse", "hadronengine"):
            v += 0.05
    # Two Pokémon that share a weakness lead badly into a spread move of that type.
    from .dex import dex

    for t in dex()["types"]:
        if effectiveness(t, a.types) > 1 and effectiveness(t, b.types) > 1:
            v -= 0.05
    return v


def best_offense(mon: Mon) -> int:
    return len(damaging_moves(mon))
