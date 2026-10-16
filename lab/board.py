"""The board: every family's best bots against the champion, on one fixed schedule (AGENTS.md).

Every entry plays the same games: each baseline of ``lab/board.json`` ``games`` times, the maps
rotating and the seats swapped, on the board's seeds, at 20 minutes. So any two entries are
compared game by game (paired), and the champion's games are the reference for all of them.

- **Entries:** the reference (the champion) and the ``fixed`` bots, from the main checkout, and
  each family's bots tagged ``board`` (module bots frozen; at most ``entries_per_family``), from the family's
  own worktree with its own code: a family's bots exist only on its branch until it is merged.
- **Played once:** a game already played the same way (the same bot versions, map, seed, length,
  seats and engine build) is not played again: lab games are deterministic, so the recorded result
  is the result. Rerunning the board plays only what changed.
- **Standings** come from the shared database: per entry its latest version's board games, points,
  the paired difference to the reference with a 95% interval, net, and points by baseline.
  ``lab board run`` saves them (``board_snapshots``), so the board has a history.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict

from . import FAMILY, MAIN_ROOT
from . import league

CONFIG = MAIN_ROOT / "lab" / "board.json"  # (the coordinator's settings: every session reads the main checkout's)
LABEL = "board"
POINTS = {"win": 1.0, "draw": 0.5, "loss": 0.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_snapshots (
  created REAL, bot TEXT, version TEXT, family TEXT, games INTEGER, expected INTEGER, points REAL,
  diff REAL, lo REAL, hi REAL, net REAL, detail TEXT
);
"""


def config(path=CONFIG) -> dict:
    cfg = json.loads(path.read_text())
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def schedule(bot: str, cfg: dict) -> list[dict]:
    """The board's games for one entry: every baseline, ``games`` times (the same for every entry)."""
    jobs = []
    for base in cfg["baselines"]:
        for j in league.matchup(bot, base, cfg["games"], maps=cfg["maps"], seed=cfg["seed"], minutes=cfg["minutes"],
                                label=LABEL):
            j["extra"] = {"board": bot, "baseline": base}
            jobs.append(j)
    return jobs


def entries(table: dict, cfg: dict, family: str | None = FAMILY) -> list[str]:
    """The entries this checkout plays: a family's own bots tagged ``board``; in the main checkout,
    the reference, the fixed bots and any merged bot tagged ``board``."""
    tagged = [n for n, b in table.items() if "board" in b.tags and (family is None or b.source == family)]
    for n in tagged:  # (a module bot's code must not change under the board: enter a frozen copy; a knobs
        # bot, kind ff, is its settings on the shared scripted bot and can't be frozen)
        if table[n].kind == "module" and "frozen" not in table[n].tags:
            raise ValueError(f"{n} is tagged board but isn't frozen: enter a frozen copy (lab bots freeze {n} --as ...)")
    if family:
        if len(tagged) > cfg["entries_per_family"]:
            raise ValueError(f"the {family} family has {len(tagged)} board entries ({', '.join(tagged)}): "
                             f"at most {cfg['entries_per_family']}")
        return tagged
    return list(dict.fromkeys([cfg["reference"], *cfg["fixed"], *tagged]))


def existing_game(db, job: dict, version, engine: str) -> str | None:
    """A whole game already played the same way (lab/reuse.py)."""
    from .reuse import existing_game as found

    return found(db, job, version, engine)


def plan(db, bots: list[str], cfg: dict) -> tuple[list[dict], int]:
    """The board games still to play for these entries, and how many are recorded already."""
    from .moments import _engine, _versions

    version, engine = _versions(), _engine()
    todo, reused = [], 0
    for bot in bots:
        for job in schedule(bot, cfg):
            if existing_game(db, job, version, engine):
                reused += 1
            else:
                todo.append(job)
    return todo, reused


# -- standings ------------------------------------------------------------------------------------


def _slots(cfg: dict) -> set:
    """The schedule's games, as (baseline, map, seed, the entry's seat): the same for every entry."""
    out = set()
    for job in schedule("ENTRY", cfg):
        out.add((job["extra"]["baseline"], job["map"], job["seed"], job["bots"].index("ENTRY")))
    return out


def results(db, cfg: dict) -> dict[str, dict]:
    """Each entry's latest version's board games: ``{bot: {"version", "family", "games": {slot: (points, net)}}}``."""
    slots = _slots(cfg)
    rows = db.q("SELECT g.id, g.map, g.seed, g.finished, json_extract(g.info, '$.board') AS entry, "
                "json_extract(g.info, '$.baseline') AS baseline, json_extract(r.params, '$.family') AS family "
                "FROM games g LEFT JOIN runs r ON r.id = g.run_id WHERE json_extract(g.info, '$.label') = ? "
                "AND g.minutes = ? AND g.branch_of IS NULL AND g.outcome != 'no_result' ORDER BY g.finished",
                LABEL, cfg["minutes"])
    by_version: dict[tuple[str, str], dict] = {}
    latest: dict[str, tuple[float, str]] = {}
    for g in rows:
        seat = db.one("SELECT * FROM seats WHERE game_id = ? AND bot = ?", g["id"], g["entry"])
        if seat is None:
            continue
        slot = (g["baseline"], g["map"], g["seed"], seat["position"])
        if slot not in slots:
            continue
        key = (g["entry"], seat["version"])
        entry = by_version.setdefault(key, {"version": seat["version"], "family": g["family"] or "main", "games": {}})
        entry["games"][slot] = (POINTS.get(seat["result"], 0.0), seat["net"] or 0)
        if g["finished"] and g["finished"] >= latest.get(g["entry"], (0.0, ""))[0]:
            latest[g["entry"]] = (g["finished"], seat["version"])
    return {bot: by_version[(bot, v)] for bot, (_, v) in latest.items()}


def standings(db, cfg: dict) -> list[dict]:
    from .moments import mean_ci

    res = results(db, cfg)
    expected = len(_slots(cfg))
    ref = res.get(cfg["reference"])
    rows = []
    for bot, r in res.items():
        games = r["games"]
        pts = [p for p, _ in games.values()]
        row = {"bot": bot, "version": r["version"], "family": r["family"], "games": len(games), "expected": expected,
               "points": sum(pts) / len(pts) if pts else 0.0,
               "net": sum(n for _, n in games.values()) / len(games) if games else 0.0,
               "by_baseline": {}, "diff": None, "lo": None, "hi": None, "pairs": 0}
        for base in cfg["baselines"]:
            mine = [p for (b, *_), (p, _) in games.items() if b == base]
            row["by_baseline"][base] = (sum(mine), len(mine))
        if ref is not None and bot != cfg["reference"]:
            diffs = [games[s][0] - ref["games"][s][0] for s in games if s in ref["games"]]
            if diffs:
                m, lo, hi = mean_ci(diffs)
                row.update(diff=m, lo=lo, hi=hi, pairs=len(diffs))
        rows.append(row)
    rows.sort(key=lambda r: (-r["points"], r["bot"]))
    return rows


def report(db, cfg: dict) -> str:
    from .report import _table

    rows = standings(db, cfg)
    if not rows:
        return "the board is empty: lab board run (the coordinator) or lab board play (a family's entries)"
    short = {"ai-normal": "normal", "ai-rush": "rush", "ai-turtle": "turtle", "exploit-eco-waves": "x-waves",
             "exploit-eco-tanks": "x-tanks", "ext-ryan": "ryan"}
    table = []
    for k, r in enumerate(rows, 1):
        if r["bot"] == cfg["reference"]:
            vs = "(reference)"
        elif r["diff"] is None:
            vs = "no pairs yet"
        else:
            verdict = "BETTER" if r["lo"] > 0 else "WORSE" if r["hi"] < 0 else "even"
            vs = f"{r['diff']:+.2f} ({r['lo']:+.2f}..{r['hi']:+.2f}) {verdict}"
        table.append([k, r["bot"][:30], r["family"][:12], f"{r['games']}/{r['expected']}", f"{100 * r['points']:.0f}%", vs,
                      f"{r['net']:+,.0f}"] + [f"{p:g}/{n}" if n else "-" for p, n in r["by_baseline"].values()])
    head = ["#", "bot", "family", "games", "points", "vs reference (points a game, 95% CI)", "net"] + \
           [short.get(b, b[:8]) for b in cfg["baselines"]]
    return "\n".join([f"the board: {len(cfg['baselines'])} baselines x {cfg['games']} games, seed {cfg['seed']}, "
                      f"{cfg['minutes']} min; reference {cfg['reference']}",
                      _table(table, head),
                      "(each entry's latest version; vs reference: the same games, paired; a verdict once the "
                      "interval clears 0)"])


def save_snapshot(db, cfg: dict) -> int:
    db.conn.executescript(SCHEMA)
    now = time.time()
    rows = standings(db, cfg)
    for r in rows:
        db.conn.execute("INSERT INTO board_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (now, r["bot"], r["version"], r["family"], r["games"], r["expected"], r["points"], r["diff"],
                         r["lo"], r["hi"], r["net"], json.dumps({"by_baseline": r["by_baseline"], "config": cfg})))
    return len(rows)


def history(db, bot: str | None = None) -> str:
    from .report import _table

    db.conn.executescript(SCHEMA)
    rows = db.q("SELECT * FROM board_snapshots" + (" WHERE bot = ?" if bot else "") + " ORDER BY created, points DESC",
                *([bot] if bot else []))
    if not rows:
        return "no board snapshots yet (lab board run saves one)"
    by: dict[float, list] = defaultdict(list)
    for r in rows:
        by[r["created"]].append(r)
    table = []
    for created, rs in by.items():
        for r in rs:
            table.append([time.strftime("%m-%d %H:%M", time.localtime(created)), r["bot"][:30], r["version"][-8:],
                          f"{r['games']}/{r['expected']}", f"{100 * r['points']:.0f}%",
                          "" if r["diff"] is None else f"{r['diff']:+.2f}"])
    return _table(table, ["when", "bot", "version", "games", "points", "vs ref"])
