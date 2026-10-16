"""Games an OpenRA AI's superweapon may have decided (lab flag-superweapons; docs/STRATEGY.md).

No player on the platform can fire support powers (the API has no such order), so a tournament
opponent never nukes us. The OpenRA AIs in the lab do: their nuke takes 6-17 buildings in a second.
Since 2026-10-16 lab games are played with superweapons off (``LAB_LOBBY``), but the AIs' airfield
powers (parabombs, paratroopers, spy plane) remain, and every older game had the nuke.

A hit is ``MIN_BUILDINGS`` or more of an agent seat's buildings lost within ``WINDOW`` ticks, in a
game against an OpenRA AI (an army breaking a base takes them seconds apart; 2-3 at once is common,
5+ almost only from a nuke). The flag goes into the game's ``info.superweapon``: when, how many
buildings, the seat's net just before and at the end, and ``decided`` when the seat was level or
ahead before the hit and lost the game (the games that would likely have gone differently).
"""

from __future__ import annotations

import json

MIN_BUILDINGS = 5
WINDOW = 75  # ticks (3 s)
TPS = 25


def detect(db, game_id: str) -> dict | None:
    seats = {s["position"]: s for s in db.q("SELECT position, kind, result, net FROM seats WHERE game_id = ?", game_id)}
    if not any(s["kind"] == "ai" for s in seats.values()) or all(s["kind"] == "ai" for s in seats.values()):
        return None
    best = None
    for pos, seat in seats.items():
        if seat["kind"] == "ai":
            continue
        ticks = [r["tick"] for r in db.q("SELECT tick FROM events WHERE game_id = ? AND position = ? "
                                         "AND kind = 'building_lost' ORDER BY tick", game_id, pos)]
        for i, t in enumerate(ticks):
            n = sum(1 for t2 in ticks[i:] if t2 - t <= WINDOW)
            if n >= MIN_BUILDINGS and (best is None or n > best["buildings"]):
                best = {"position": pos, "tick": t, "at": f"{t // TPS // 60}:{t // TPS % 60:02d}", "buildings": n}
    if best is None:
        return None
    row = db.one("SELECT kills_cost - deaths_cost AS net FROM timeline WHERE game_id = ? AND position = ? AND tick < ? "
                 "ORDER BY tick DESC LIMIT 1", game_id, best["position"], best["tick"] - TPS)
    seat = seats[best["position"]]
    best["net_before"] = row["net"] if row else None
    best["net_end"] = seat["net"]
    best["result"] = seat["result"]
    best["decided"] = bool(seat["result"] == "loss" and row is not None and row["net"] >= 0)
    return best


def flag(db, game_id: str) -> dict | None:
    """Detect and store (``info.superweapon``); returns the flag or None."""
    hit = detect(db, game_id)
    if hit is not None:
        g = db.one("SELECT info FROM games WHERE id = ?", game_id)
        info = json.loads(g["info"] or "{}")
        info["superweapon"] = hit
        db.conn.execute("UPDATE games SET info = ? WHERE id = ?", (json.dumps(info), game_id))
    return hit


def flag_all(db) -> list[tuple[str, dict]]:
    rows = db.q("SELECT g.id FROM games g WHERE json_extract(g.info, '$.superweapon') IS NULL AND EXISTS "
                "(SELECT 1 FROM seats s WHERE s.game_id = g.id AND s.kind = 'ai')")
    out = []
    for r in rows:
        hit = flag(db, r["id"])
        if hit:
            out.append((r["id"], hit))
    return out
