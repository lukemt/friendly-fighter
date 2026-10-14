"""Text views of a seat's observation, for a person (or Claude) playing or studying a game.

- ``summary(obs, ...)``: economy, score, buildings, production, army, enemies, in a screenful;
- ``minimap(obs, map_id, ...)``: the map as text (terrain from the map's layout) with our units and
  buildings and the enemies we see, whole (scaled down) or a window around a cell;
- ``units_table`` / ``enemies_table``: every unit with its id, cell, health and activity.
"""

from __future__ import annotations

from collections import Counter

from ffsim.observe import NON_COMBAT, SHIPS
from ffsim.rules import Rules

# Symbols on the map. Ours: buildings B (construction yard F, defenses D), units by role; theirs:
# lower-case is a unit, upper-case a building.
OWN_BUILDING, OWN_FACT, OWN_DEFENSE, OWN_COMBAT, OWN_HARV, OWN_OTHER = "B", "F", "D", "U", "H", "u"
ENEMY_BUILDING, ENEMY_UNIT = "X", "x"
DEFENSES = {"pbox", "hbox", "gun", "ftur", "tsla", "agun", "sam", "gap", "iron"}
PRIORITY = {s: i for i, s in enumerate([ENEMY_UNIT, ENEMY_BUILDING, OWN_COMBAT, OWN_FACT, OWN_DEFENSE, OWN_BUILDING,
                                        OWN_HARV, OWN_OTHER])}
TERRAIN = {".": " ", ",": " ", "=": "=", "^": "^", "T": "T", "#": "#", "$": "$", "*": "*", "M": "M", "S": " "}


def clock(seconds) -> str:
    if seconds is None:
        return "-"
    s = int(round(float(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def _alive(units):
    return [u for u in units or [] if not u.get("wreck")]


def is_combat(u: dict) -> bool:
    return u["type"] not in NON_COMBAT and u["type"] not in SHIPS and not u.get("wreck")


def summary(obs: dict, *, time_info: dict | None = None, seat: str = "", extra: list[str] | None = None,
            remembered: dict | None = None) -> str:
    eco, mil = obs.get("economy") or {}, obs.get("military") or {}
    t = time_info or {}
    lines = [f"{clock(t.get('game_seconds'))} (left {clock(t.get('remaining_seconds'))})  "
             f"{obs.get('faction')} vs {obs.get('enemy_faction')}  {seat}  tick {obs.get('tick')}"]
    net = (mil.get("kills_cost") or 0) - (mil.get("deaths_cost") or 0)
    lines.append(f"cash {eco.get('cash')} + ore {eco.get('ore')} (cap {eco.get('resource_capacity')})  "
                 f"power {eco.get('power_provided')}/{eco.get('power_drained')}  harvesters {eco.get('harvesters')}  "
                 f"explored {obs.get('explored_percent')}%")
    lines.append(f"score: kills ${mil.get('kills_cost')} - losses ${mil.get('deaths_cost')} = net {net:+d}   "
                 f"assets ${mil.get('assets_value')}  army ${mil.get('army_value')}  "
                 f"(units {mil.get('units_killed')}/{mil.get('units_lost')} killed/lost, "
                 f"buildings {mil.get('buildings_killed')}/{mil.get('buildings_lost')})")
    lines.append(f"base {obs.get('base_center')}")
    bl = []
    for b in obs.get("buildings") or []:
        flags = "".join(f for f, on in (("!", b.get("hp", 100) < 50), ("r", b.get("repairing")),
                                         ("o", b.get("powered_down"))) if on)
        bl.append(f"{b['type']}#{b['id']}@{b['x']},{b['y']}" + (f"({b['hp']}%{flags})" if b.get("hp", 100) < 100 or flags else ""))
    lines.append("buildings: " + (" ".join(bl) or "-"))
    prod = [f"{p['queue']}:{p['item']} {int(round(p['progress'] * 100))}%" + (" READY" if p.get("ready") else "")
            + (" paused" if p.get("paused") else "") for p in obs.get("production") or []]
    lines.append("production: " + (", ".join(prod) or "-"))
    lines.append("can build: " + " ".join(obs.get("available_production") or []))
    units = _alive(obs.get("units"))
    combat = [u for u in units if is_combat(u)]
    by_type = Counter(u["type"] for u in combat)
    idle = sum(1 for u in combat if u.get("idle"))
    lines.append(f"army: {len(combat)} combat units ({idle} idle): "
                 + (", ".join(f"{k} x{v}" for k, v in by_type.most_common()) or "-"))
    others = Counter(u["type"] for u in units if not is_combat(u))
    if others:
        lines.append("other units: " + ", ".join(f"{k} x{v}" for k, v in others.most_common()))
    enemies = _alive(obs.get("visible_enemies"))
    eb = _alive(obs.get("visible_enemy_buildings"))
    lines.append(f"enemies in sight: {len(enemies)} units "
                 + ("(" + ", ".join(f"{k} x{v}" for k, v in Counter(e['type'] for e in enemies).most_common()) + ")" if enemies else "")
                 + f", {len(eb)} buildings " + (" ".join(f"{b['type']}#{b['id']}@{b['x']},{b['y']}" for b in eb[:12]) if eb else ""))
    if remembered:
        lines.append("enemy buildings seen before: " + " ".join(f"{t}#{i}@{c[0]},{c[1]}" for i, (c, t) in list(remembered.items())[:15]))
    for line in extra or []:
        lines.append(line)
    return "\n".join(lines)


def units_table(obs: dict, kind: str | None = None) -> str:
    rows = []
    for u in _alive(obs.get("units")):
        if kind and u["type"] != kind and not (kind == "combat" and is_combat(u)):
            continue
        rows.append(f"  #{u['id']:<6} {u['type']:6} @{u['x']},{u['y']:<4} {u['hp']:3d}%  "
                    f"{'idle' if u.get('idle') else (u.get('act') or 'busy')}")
    return "\n".join(rows) or "  (none)"


def enemies_table(obs: dict) -> str:
    rows = [f"  #{e['id']:<6} {e['type']:6} @{e['x']},{e['y']:<4} {e['hp']:3d}%" for e in _alive(obs.get("visible_enemies"))]
    rows += [f"  #{b['id']:<6} {b['type']:6} @{b['x']},{b['y']:<4} {b['hp']:3d}%  (building)"
             for b in _alive(obs.get("visible_enemy_buildings"))]
    return "\n".join(rows) or "  (none in sight)"


def _marks(obs: dict) -> dict[tuple[int, int], str]:
    marks: dict[tuple[int, int], str] = {}

    def put(x, y, sym):
        old = marks.get((x, y))
        if old is None or PRIORITY[sym] < PRIORITY[old]:
            marks[(x, y)] = sym

    for b in obs.get("buildings") or []:
        sym = OWN_FACT if b["type"] == "fact" else OWN_DEFENSE if b["type"] in DEFENSES else OWN_BUILDING
        put(b["x"], b["y"], sym)
    for u in _alive(obs.get("units")):
        put(u["x"], u["y"], OWN_COMBAT if is_combat(u) else OWN_HARV if u["type"] == "harv" else OWN_OTHER)
    for b in _alive(obs.get("visible_enemy_buildings")):
        put(b["x"], b["y"], ENEMY_BUILDING)
    for e in _alive(obs.get("visible_enemies")):
        put(e["x"], e["y"], ENEMY_UNIT)
    return marks


def minimap(obs: dict, map_id: str, *, center: tuple[int, int] | None = None, radius: int = 12,
            scale: int | None = None, marks: dict | None = None) -> str:
    """The whole map scaled down (``scale`` cells a character, default to fit ~50 columns), or the
    window of ``radius`` cells around ``center`` at full size, with a ruler."""
    m = Rules.load().map(map_id)
    lay = m["layout"]
    ox, oy = lay["origin"]
    rows = lay["rows"]
    marks = _marks(obs) if marks is None else marks

    def terrain(x, y):
        r, c = y - oy, x - ox
        if 0 <= r < len(rows) and 0 <= c < len(rows[r]):
            return TERRAIN.get(rows[r][c], " ")
        return " "

    if center is not None:
        x0, y0 = center[0] - radius, center[1] - radius
        size = 2 * radius + 1
        out = ["     " + "".join(str((x0 + i) % 10) for i in range(size))]
        for j in range(size):
            y = y0 + j
            out.append(f"{y:4d} " + "".join(marks.get((x0 + i, y)) or terrain(x0 + i, y) for i in range(size)))
        return "\n".join(out)
    bx, by, bw, bh = m["bounds"]
    scale = scale or max(1, -(-max(bw, bh) // 50))
    out = [f"map {map_id} {bw}x{bh}, {scale}x{scale} cells a character; columns from x={bx}, rows from y={by}"]
    ruler = "".join(str(((bx + i * scale) // 10) % 10) if (bx + i * scale) % 10 < scale else " " for i in range(-(-bw // scale)))
    out.append("     " + ruler)
    for j in range(-(-bh // scale)):
        y = by + j * scale
        line = []
        for i in range(-(-bw // scale)):
            x = bx + i * scale
            cell = [(marks.get((x + a, y + b)), terrain(x + a, y + b)) for a in range(scale) for b in range(scale)]
            syms = [s for s, _ in cell if s]
            if syms:
                line.append(min(syms, key=lambda s: PRIORITY[s]))
            else:
                ter = Counter(t for _, t in cell).most_common(1)[0][0]
                line.append(ter)
        out.append(f"{y:4d} " + "".join(line))
    out.append(f"legend: {OWN_FACT} our construction yard, {OWN_BUILDING} building, {OWN_DEFENSE} defense, "
               f"{OWN_COMBAT} combat unit, {OWN_HARV} harvester, {OWN_OTHER} other; {ENEMY_BUILDING} enemy building, "
               f"{ENEMY_UNIT} enemy unit; ^ rock, T trees, # obstacle, $ ore, * gems, = road/bridge")
    return "\n".join(out)
