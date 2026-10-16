"""A damage estimate: Showdown's gen 9 formula with the modifiers that matter most in VGC.

Not exact (no crit rolls, a few dozen abilities and items, no Tera): good enough to rank moves
and targets and to tell a likely KO from a chip. Damage is returned as a fraction of the
defender's max HP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dex import Mon, effectiveness, move as move_data, to_id, type_name

SPREAD_TARGETS = {"allAdjacentFoes", "allAdjacent"}
TYPE_ITEMS = {
    "charcoal": "Fire", "mysticwater": "Water", "miracleseed": "Grass", "magnet": "Electric",
    "nevermeltice": "Ice", "blackbelt": "Fighting", "poisonbarb": "Poison", "softsand": "Ground",
    "sharpbeak": "Flying", "twistedspoon": "Psychic", "silverpowder": "Bug", "hardstone": "Rock",
    "spelltag": "Ghost", "dragonfang": "Dragon", "blackglasses": "Dark", "metalcoat": "Steel",
    "silkscarf": "Normal", "fairyfeather": "Fairy",
}
RESIST_BERRIES = {
    "occaberry": "Fire", "passhoberry": "Water", "wacanberry": "Electric", "rindoberry": "Grass",
    "yacheberry": "Ice", "chopleberry": "Fighting", "kebiaberry": "Poison", "shucaberry": "Ground",
    "cobaberry": "Flying", "payapaberry": "Psychic", "tangaberry": "Bug", "chartiberry": "Rock",
    "kasibberry": "Ghost", "habanberry": "Dragon", "colburberry": "Dark", "babiriberry": "Steel",
    "roseliberry": "Fairy", "chilanberry": "Normal",
}
IMMUNE_ABILITIES = {
    "levitate": "Ground", "eartheater": "Ground", "flashfire": "Fire", "wellbakedbody": "Fire",
    "waterabsorb": "Water", "stormdrain": "Water", "dryskin": "Water", "voltabsorb": "Electric",
    "lightningrod": "Electric", "motordrive": "Electric", "sapsipper": "Grass",
}
SKIN_ABILITIES = {"aerilate": "Flying", "pixilate": "Fairy", "refrigerate": "Ice", "galvanize": "Electric"}
MASKS = {"wellspringmask", "hearthflamemask", "cornerstonemask"}


@dataclass
class Side:
    """What's true of one side of the field for the calc."""

    reflect: bool = False
    light_screen: bool = False
    aurora_veil: bool = False
    tailwind: bool = False


@dataclass
class Field:
    weather: str = ""  # sunnyday/raindance/sandstorm/snow (any spelling)
    terrain: str = ""  # electricterrain/grassyterrain/psychicterrain/mistyterrain
    trick_room: bool = False

    @property
    def weather_id(self) -> str:
        w = to_id(self.weather)
        return {"sun": "sunnyday", "desolateland": "sunnyday", "rain": "raindance", "primordialsea": "raindance",
                "sand": "sandstorm", "hail": "snow", "snowscape": "snow"}.get(w, w)

    @property
    def terrain_id(self) -> str:
        t = to_id(self.terrain)
        return t if t.endswith("terrain") or not t else t + "terrain"


@dataclass
class Battler:
    """A Mon on the field: its set plus what the battle did to it."""

    mon: Mon
    hp: float = 1.0  # fraction of max HP
    status: str = ""  # brn/par/slp/psn/tox/frz
    boosts: dict[str, int] = field(default_factory=dict)
    item_known_gone: bool = False

    @property
    def item(self) -> str:
        return "" if self.item_known_gone else self.mon.item_id


def boost_mult(stage: int) -> float:
    stage = max(-6, min(6, int(stage)))
    return (2 + stage) / 2 if stage >= 0 else 2 / (2 - stage)


def grounded(b: Battler) -> bool:
    return "Flying" not in b.mon.types and b.mon.ability_id != "levitate" and b.item != "airballoon"


def effective_speed(b: Battler, side: Side | None = None) -> float:
    spe = b.mon.stats["spe"] * boost_mult(b.boosts.get("spe", 0))
    if b.item == "choicescarf":
        spe *= 1.5
    if b.status == "par":
        spe *= 0.5
    if side is not None and side.tailwind:
        spe *= 2
    return spe


def move_type(attacker: Battler, mv: dict, fld: Field) -> str:
    mtype = mv["type"]
    mid = to_id(mv["name"])
    if mid == "weatherball":
        mtype = {"sunnyday": "Fire", "raindance": "Water", "sandstorm": "Rock", "snow": "Ice"}.get(fld.weather_id, "Normal")
    elif mid == "terrainpulse" and grounded(attacker):
        mtype = {"electricterrain": "Electric", "grassyterrain": "Grass", "psychicterrain": "Psychic",
                 "mistyterrain": "Fairy"}.get(fld.terrain_id, "Normal")
    elif mid == "ivycudgel":
        mtype = {"wellspringmask": "Water", "hearthflamemask": "Fire", "cornerstonemask": "Rock"}.get(attacker.item, "Grass")
    elif mid == "ragingbull" and len(attacker.mon.types) > 1:
        mtype = attacker.mon.types[1]
    elif mtype == "Normal" and attacker.mon.ability_id in SKIN_ABILITIES:
        mtype = SKIN_ABILITIES[attacker.mon.ability_id]
    return mtype


def is_immune(attacker: Battler, defender: Battler, mv: dict, mtype: str, ally_side_ability: str = "") -> bool:
    """The move does nothing to this defender (type, ability or item immunity)."""
    if mv["cat"] == "Status":
        return False
    dab = defender.mon.ability_id
    if attacker.mon.ability_id in ("moldbreaker", "teravolt", "turboblaze", "myceliummight"):
        dab = ""
    if IMMUNE_ABILITIES.get(dab) == mtype:
        return True
    if mtype == "Ground" and not grounded(defender) and to_id(mv["name"]) != "thousandarrows":
        return True
    flags = mv.get("flags", ())
    if dab == "bulletproof" and "bullet" in flags or dab == "soundproof" and "sound" in flags:
        return True
    if dab == "windrider" and "wind" in flags:
        return True
    if dab == "telepathy" and ally_side_ability:
        return True
    if mv.get("fixed") or to_id(mv["name"]) in ("superfang", "ruination", "finalgambit"):
        eff = effectiveness(mtype, defender.mon.types)
        return eff == 0
    return effectiveness(mtype, defender.mon.types) == 0


def base_power(attacker: Battler, defender: Battler, mv: dict, fld: Field) -> float:
    mid = to_id(mv["name"])
    bp = float(mv.get("bp") or 0)
    if mid in ("lowkick", "grassknot"):
        w = defender.mon.weight
        bp = 20 if w < 10 else 40 if w < 25 else 60 if w < 50 else 80 if w < 100 else 100 if w < 200 else 120
    elif mid in ("heavyslam", "heatcrash"):
        ratio = attacker.mon.weight / max(defender.mon.weight, 0.1)
        bp = 120 if ratio >= 5 else 100 if ratio >= 4 else 80 if ratio >= 3 else 60 if ratio >= 2 else 40
    elif mid == "gyroball":
        bp = min(150, 25 * defender.mon.stats["spe"] / max(1, attacker.mon.stats["spe"]) + 1)
    elif mid == "electroball":
        r = attacker.mon.stats["spe"] / max(1, defender.mon.stats["spe"])
        bp = 150 if r >= 4 else 120 if r >= 3 else 80 if r >= 2 else 60 if r >= 1 else 40
    elif mid in ("eruption", "waterspout", "dragonenergy"):
        bp = max(1, 150 * attacker.hp)
    elif mid == "facade" and attacker.status:
        bp = 140
    elif mid == "hex" and defender.status:
        bp = 130
    elif mid == "acrobatics" and not attacker.item:
        bp = 110
    elif mid == "knockoff" and defender.item:
        bp = 97.5
    elif mid in ("lastrespects", "ragefist"):
        bp = 75
    elif mid == "terablast":
        bp = 80
    elif mid == "weatherball" and fld.weather_id:
        bp = 100
    elif mid == "expandingforce" and fld.terrain_id == "psychicterrain":
        bp = 120
    elif mid == "tripleaxel":
        bp = 40  # 20+40+60 over three hits
    elif mid == "boltbeak" or mid == "fishiousrend":
        bp = 85 * 1.5  # usually moves first in doubles... half the time
    elif mid in ("storedpower", "powertrip"):
        bp = 20 + 20 * sum(max(0, v) for v in attacker.boosts.values())
    elif mid == "temperflare" or mid == "stompingtantrum":
        bp = mv.get("bp") or 75
    if attacker.mon.ability_id == "technician" and bp <= 60:
        bp *= 1.5
    return bp


def hits(attacker: Battler, mv: dict) -> float:
    mh = mv.get("multihit")
    if not mh:
        return 1.0
    if isinstance(mh, list):
        if attacker.mon.ability_id == "skilllink" or attacker.item == "loadeddice":
            return float(mh[1]) if attacker.mon.ability_id == "skilllink" else 4.5
        if mh == [2, 5]:
            return 3.1
        return (mh[0] + mh[1]) / 2
    if to_id(mv["name"]) == "populationbomb":
        return 9.0 if attacker.item == "widelens" or attacker.mon.ability_id == "skilllink" else 6.0
    return float(mh)


def accuracy(attacker: Battler, mv: dict, fld: Field) -> float:
    acc = mv.get("acc") or 0
    if not acc:
        return 1.0
    mid = to_id(mv["name"])
    if mid in ("thunder", "hurricane") and fld.weather_id == "raindance":
        return 1.0
    if mid == "blizzard" and fld.weather_id == "snow":
        return 1.0
    if attacker.mon.ability_id == "noguard":
        return 1.0
    a = acc / 100
    if attacker.mon.ability_id == "compoundeyes":
        a *= 1.3
    if attacker.mon.ability_id == "hustle" and mv["cat"] == "Physical":
        a *= 0.8
    if attacker.item == "widelens":
        a *= 1.1
    a *= boost_mult(attacker.boosts.get("accuracy", 0)) if attacker.boosts.get("accuracy") else 1
    return min(1.0, a)


@dataclass
class Hit:
    """An expected hit: damage as a fraction of the defender's max HP."""

    expected: float  # mean roll, before accuracy
    low: float  # lowest roll
    high: float  # highest roll
    accuracy: float
    effectiveness: float

    @property
    def mean(self) -> float:
        return self.expected * self.accuracy


NO_HIT = Hit(0.0, 0.0, 0.0, 1.0, 0.0)


def damage(attacker: Battler, defender: Battler, mv: dict | str, fld: Field | None = None,
           defender_side: Side | None = None, *, spread: bool | None = None, helping_hand: bool = False) -> Hit:
    """``attacker`` hitting ``defender`` with ``mv``. ``spread``: the move hits more than one target
    (the 0.75 doubles modifier); by default, from the move's target type."""
    fld = fld or Field()
    defender_side = defender_side or Side()
    if isinstance(mv, str):
        mv = move_data(mv)
    if not mv or mv["cat"] == "Status":
        return NO_HIT
    mid = to_id(mv["name"])
    mtype = move_type(attacker, mv, fld)
    if is_immune(attacker, defender, mv, mtype):
        return NO_HIT
    acc = accuracy(attacker, mv, fld)
    dmax = defender.mon.stats["hp"]
    eff = effectiveness(mtype, defender.mon.types)
    if mv.get("fixed") == "level" or mid in ("seismictoss", "nightshade"):
        f = 50 / dmax
        return Hit(f, f, f, acc, 1.0)
    if mid in ("superfang", "ruination", "naturesmadness"):
        f = defender.hp / 2
        return Hit(f, f, f, acc, 1.0)
    if mid == "finalgambit":
        f = attacker.hp * attacker.mon.stats["hp"] / dmax
        return Hit(f, f, f, acc, 1.0)
    if isinstance(mv.get("fixed"), (int, float)):
        f = float(mv["fixed"]) / dmax
        return Hit(f, f, f, acc, 1.0)
    bp = base_power(attacker, defender, mv, fld)
    if bp <= 0:
        return NO_HIT

    physical = mv["cat"] == "Physical"
    off_stat = mv.get("offStat") or ("atk" if physical else "spa")
    def_stat = mv.get("defStat") or ("def" if physical else "spd")
    source = defender if mv.get("offMon") == "target" else attacker
    crit = bool(mv.get("crit"))
    atk_boost = source.boosts.get(off_stat, 0)
    def_boost = defender.boosts.get(def_stat, 0)
    if crit:
        atk_boost, def_boost = max(0, atk_boost), min(0, def_boost)
    if attacker.mon.ability_id == "unaware":
        def_boost = 0
    if defender.mon.ability_id == "unaware":
        atk_boost = 0
    a = source.mon.stats[off_stat] * boost_mult(atk_boost)
    d = defender.mon.stats[def_stat] * boost_mult(def_boost)

    ab, dab, item, ditem = attacker.mon.ability_id, defender.mon.ability_id, attacker.item, defender.item
    if ab in ("moldbreaker", "teravolt", "turboblaze", "myceliummight"):
        dab = ""
    w, t = fld.weather_id, fld.terrain_id
    if physical:
        if ab in ("hugepower", "purepower"):
            a *= 2
        if ab == "hustle":
            a *= 1.5
        if ab == "guts" and attacker.status:
            a *= 1.5
        if ab == "orichalcumpulse" and w == "sunnyday":
            a *= 1.333
        if item == "choiceband":
            a *= 1.5
        if dab == "furcoat":
            d *= 2
        if dab == "tabletsofruin":
            a *= 0.75
        if ab == "swordofruin":
            d *= 0.75
        if w == "snow" and "Ice" in defender.mon.types and def_stat == "def":
            d *= 1.5
    else:
        if item == "choicespecs":
            a *= 1.5
        if ab == "solarpower" and w == "sunnyday":
            a *= 1.5
        if ab == "hadronengine" and t == "electricterrain":
            a *= 1.333
        if ditem == "assaultvest" and def_stat == "spd":
            d *= 1.5
        if dab == "vesselofruin":
            a *= 0.75
        if ab == "beadsofruin" and def_stat == "spd":
            d *= 0.75
        if w == "sandstorm" and "Rock" in defender.mon.types and def_stat == "spd":
            d *= 1.5
    if ab in ("protosynthesis", "quarkdrive") and (
        item == "boosterenergy" or (ab == "protosynthesis" and w == "sunnyday")
        or (ab == "quarkdrive" and t == "electricterrain")
    ):
        best = max(("atk", "def", "spa", "spd", "spe"), key=lambda s: attacker.mon.stats[s])
        if best == off_stat:
            a *= 1.3

    base = (22 * bp * a / max(d, 1)) / 50 + 2

    mod = 1.0
    spread = mv["target"] in SPREAD_TARGETS if spread is None else spread
    if spread:
        mod *= 0.75
    if w == "sunnyday":
        mod *= 1.5 if mtype == "Fire" or mid == "hydrosteam" else 0.5 if mtype == "Water" else 1
    elif w == "raindance":
        mod *= 1.5 if mtype == "Water" else 0.5 if mtype == "Fire" else 1
    if crit:
        mod *= 1.5
    stab = mtype in attacker.mon.types
    if stab:
        mod *= 2.0 if ab == "adaptability" else 1.5
    mod *= eff
    if attacker.status == "brn" and physical and ab != "guts" and mid != "facade":
        mod *= 0.5
    # Screens (doubles: 2732/4096), unless the move breaks or ignores them.
    if not crit and ab != "infiltrator" and mid not in ("brickbreak", "psychicfangs", "ragingbull"):
        if defender_side.aurora_veil or (physical and defender_side.reflect) or (not physical and defender_side.light_screen):
            mod *= 0.667

    # Base power and attack modifiers that act like a multiplier on the result.
    flags = mv.get("flags", ())
    if ab == "toughclaws" and "contact" in flags:
        mod *= 1.3
    if ab == "sheerforce" and mv.get("sec"):
        mod *= 1.3
    if ab == "ironfist" and "punch" in flags:
        mod *= 1.2
    if ab == "strongjaw" and "bite" in flags:
        mod *= 1.5
    if ab == "megalauncher" and "pulse" in flags:
        mod *= 1.5
    if ab == "sharpness" and "slicing" in flags:
        mod *= 1.5
    if ab == "punkrock" and "sound" in flags:
        mod *= 1.3
    if ab in SKIN_ABILITIES and mv["type"] == "Normal":
        mod *= 1.2
    if ab == "transistor" and mtype == "Electric":
        mod *= 1.3
    if ab == "dragonsmaw" and mtype == "Dragon":
        mod *= 1.5
    if ab in ("steelworker", "steelyspirit") and mtype == "Steel":
        mod *= 1.5
    if ab == "rockypayload" and mtype == "Rock":
        mod *= 1.5
    if ab in ("blaze", "torrent", "overgrow", "swarm") and attacker.hp <= 1 / 3:
        if mtype == {"blaze": "Fire", "torrent": "Water", "overgrow": "Grass", "swarm": "Bug"}[ab]:
            mod *= 1.5
    if item == "lifeorb":
        mod *= 1.3
    if item == "expertbelt" and eff > 1:
        mod *= 1.2
    if TYPE_ITEMS.get(item) == mtype or item in MASKS:
        mod *= 1.2
    if item == "punchingglove" and "punch" in flags:
        mod *= 1.1
    if helping_hand:
        mod *= 1.5
    if grounded(attacker):
        if (t, mtype) in (("electricterrain", "Electric"), ("grassyterrain", "Grass"), ("psychicterrain", "Psychic")):
            mod *= 1.3
    if t == "mistyterrain" and mtype == "Dragon" and grounded(defender):
        mod *= 0.5
    if t == "grassyterrain" and mid in ("earthquake", "bulldoze") and grounded(defender):
        mod *= 0.5

    # Defender abilities and items.
    if dab in ("thickfat",) and mtype in ("Fire", "Ice"):
        mod *= 0.5
    if dab in ("heatproof", "waterbubble") and mtype == "Fire":
        mod *= 0.5
    if dab == "purifyingsalt" and mtype == "Ghost":
        mod *= 0.5
    if dab in ("multiscale", "shadowshield") and defender.hp >= 0.999:
        mod *= 0.5
    if dab == "icescales" and not physical:
        mod *= 0.5
    if dab == "fluffy":
        if "contact" in flags:
            mod *= 0.5
        if mtype == "Fire":
            mod *= 2
    if dab in ("filter", "solidrock", "prismarmor") and eff > 1:
        mod *= 0.75
    if dab == "dryskin" and mtype == "Fire":
        mod *= 1.25
    if RESIST_BERRIES.get(ditem) == mtype and (eff > 1 or mtype == "Normal"):
        mod *= 0.5

    n = hits(attacker, mv)
    mean = base * mod * 0.925 * n / dmax
    return Hit(mean, base * mod * 0.85 * n / dmax, base * mod * n / dmax, acc, eff)
