"""The lab: play, record and study Red Alert bots locally (docs/LAB.md).

    python scripts/lab.py bots                                   # the bots, their versions and games
    python scripts/lab.py play ff-default ai-normal --games 12   # a matchup (maps rotate, seats swap)
    python scripts/lab.py league ff-default ff-turtle ai-normal ai-rush --games 40
    python scripts/lab.py gate ff-turtle --champion ff-default --baselines ai-normal,ai-rush --games 6
    python scripts/lab.py human start --vs ai-normal             # play yourself (then: lab.py h state / go / ...)
    python scripts/lab.py branch <game> --at 4:30 --variants ff-turtle,ff-rush --repeats 3
    python scripts/lab.py look <game> --at 4:30                  # the board at that moment
    python scripts/lab.py games | show <game> | stats <bot> --by opponent | ratings | report --last 40
    python scripts/lab.py note --game <game> "text"   |  experiment new NAME --hypothesis "..."
    python scripts/lab.py sql "select bot, count(*) from seats group by bot"

Every game is in sim/lab/lab.db with its engine trace (sim/lab/traces/): who played (bot and
version), the result, the score every 5 s, every event and every batch of orders.
"""

from __future__ import annotations

import os
import sys

# Lab processes are reproducible (set iteration order) and apart from the live runtime's files.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)

import argparse  # noqa: E402
import json  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lab import LAB_DIR  # noqa: E402
from lab.batch import lab_env  # noqa: E402

lab_env()

from lab import bots as registry  # noqa: E402
from lab import league as schedule  # noqa: E402
from lab import report  # noqa: E402
from lab.db import LabDB  # noqa: E402
from lab.views import clock  # noqa: E402

MAPS = schedule.MAPS


def _maps(text: str) -> list[str]:
    return MAPS if text in ("all", "random") else text.split(",")


def _filters(a) -> dict:
    return {k: getattr(a, k, None) for k in ("run", "map", "since", "branches", "version", "vs", "label", "failed")}


def _command() -> str:
    return "lab " + " ".join(shlex.quote(x) for x in sys.argv[1:])


def _quota() -> int:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or 1


def _check_bots(names: list[str]) -> None:
    table = registry.load()
    for n in names:
        registry.get(n, table)


def _play(db: LabDB, jobs: list[dict], a, kind: str, params: dict) -> int:
    from lab import batch

    run_id = db.start_run(kind, _command(), params, note=a.note or "", experiment=a.experiment or "")
    workers = min(a.workers or _quota(), len(jobs))
    print(f"[lab] run {run_id}: {len(jobs)} games on {workers} workers (logs: {batch.LOG_DIR}/run-{run_id}-w*.log)",
          flush=True)
    t0 = time.monotonic()

    def progress(msg, k, n):
        if not msg.get("ok"):
            print(f"[{k}/{n}] FAILED {msg['job']['bots']}: {msg['error']}", flush=True)
            return
        g = msg["game"]
        s0, s1 = g["seats"]
        print(f"[{k}/{n}] {report.short(g['id'])} {g['map']:14} {s0['bot']} ({(s0['faction'] or '?')[:3]}) "
              f"{report._res(s0['result'])}-{report._res(s1['result'])} {s1['bot']} ({(s1['faction'] or '?')[:3]})  "
              f"net {s0['net']:+d}  {clock(g['game_seconds'])} {g['termination']}  {msg['wall']:.0f}s", flush=True)

    status = "done"
    try:
        batch.run(jobs, workers=workers, run_id=run_id, record=a.record, on_result=progress)
    except KeyboardInterrupt:
        status = "interrupted"
        print("[lab] interrupted", flush=True)
    finally:
        db.end_run(run_id, status)
    wall = time.monotonic() - t0
    n = db.one("SELECT COUNT(*) AS n FROM games WHERE run_id = ?", run_id)["n"]
    print(f"[lab] run {run_id}: {n} games in {wall:.0f} s ({wall / max(n, 1):.1f} s a game)")
    return run_id


# -- commands ------------------------------------------------------------------------------------


def cmd_bots(a, db) -> int:
    if a.action == "add":
        table = registry.load()
        if a.name in table and not a.force:
            print(f"{a.name} exists (--force to replace)")
            return 1
        params = {}
        if a.kind == "ff":
            params = {"knobs": json.loads(a.knobs or "{}")}
        elif a.kind == "module":
            params = {"factory": a.factory, **({"kwargs": json.loads(a.kwargs)} if a.kwargs else {})}
        elif a.kind == "ai":
            params = {"type": a.type}
        think = a.think if a.think in ("measured", "paused") else float(a.think)
        table[a.name] = registry.Bot(name=a.name, kind=a.kind, params=params, think=think, description=a.desc or "",
                                     tags=[t for t in (a.tags or "").split(",") if t])
        registry.save(table)
        print(f"added {a.name}: {table[a.name].version()}")
        return 0
    if a.action == "show":
        b = registry.get(a.name)
        print(json.dumps({**b.spec(), "name": b.name, "version": b.version(), "description": b.description,
                          "tags": b.tags}, indent=1))
        rows = db.q("SELECT version, kind, git_commit, first_seen FROM bot_versions WHERE bot = ? ORDER BY first_seen", b.name)
        for r in rows:
            print(f"  {r['version']}  first played {report._when(r['first_seen'])} at {r['git_commit']}")
        return 0
    print(report.bots_table(db))
    return 0


def cmd_play(a, db) -> int:
    _check_bots([a.a, a.b])
    jobs = schedule.matchup(a.a, a.b, a.games, maps=_maps(a.map), seed=a.seed, minutes=a.minutes)
    run_id = _play(db, jobs, a, "play", vars(a))
    print()
    print(report.stats(db, a.a, {"run": run_id, "vs": a.b}, "map"))
    return 0


def cmd_league(a, db) -> int:
    table = registry.load()
    names = list(a.bots)
    if a.tag:
        names += [n for n, b in table.items() if a.tag in b.tags and n not in names]
    _check_bots(names)
    kinds = {n: registry.get(n, table).kind for n in names}
    played = None
    if a.balance:
        played = {}
        for r in db.q("SELECT a.bot AS x, b.bot AS y, COUNT(*) AS n FROM seats a JOIN seats b ON a.game_id = b.game_id "
                      "AND a.position = 0 AND b.position = 1 JOIN games g ON g.id = a.game_id WHERE g.branch_of IS NULL "
                      "GROUP BY a.bot, b.bot"):
            key = tuple(sorted((r["x"], r["y"])))
            played[key] = played.get(key, 0) + r["n"]
    jobs = schedule.league(names, a.games, kinds=kinds, maps=_maps(a.map), seed=a.seed, minutes=a.minutes, played=played)
    run_id = _play(db, jobs, a, "league", vars(a))
    print()
    print(report.ratings_table(db, {"run": run_id}))
    print()
    print(report.pairs_matrix(db, {"run": run_id}))
    print()
    print(report.ratings_table(db, {}, bots=names))
    return 0


def cmd_gate(a, db) -> int:
    baselines = [b for b in a.baselines.split(",") if b]
    _check_bots([a.candidate, a.champion] + baselines)
    jobs = schedule.gate(a.candidate, a.champion, baselines, a.games, maps=_maps(a.map), seed=a.seed, minutes=a.minutes)
    run_id = _play(db, jobs, a, "gate", vars(a))
    print()
    print(report.gate_report(db, run_id, a.candidate, a.champion))
    return 0


def cmd_branch(a, db) -> int:
    from lab.branch import branch_jobs, parse_time

    seconds = parse_time(a.at) / 25
    variants = [v for v in a.variants.split(",") if v]
    _check_bots(variants + ([a.opponent] if a.opponent else []))
    jobs = branch_jobs(db, a.game, seconds, variants, seat=a.seat, opponent=a.opponent, repeats=a.repeats,
                       seed=a.seed, stop_after=a.stop_after, control=not a.no_control)
    _play(db, jobs, a, "branch", vars(a))
    print()
    print(report.branch_report(db, a.game))
    return 0


def cmd_look(a, db) -> int:
    from lab.branch import parse_time, snapshot
    from lab.views import enemies_table, minimap, summary, units_table

    g = db.find_game(a.game)
    if g is None:
        print(f"no game {a.game!r}")
        return 1
    if g["time_mode"] == "platform":  # (no engine trace: the recorded states themselves)
        from lab.importer import states_at

        states = states_at(g["id"], parse_time(a.at) / 25)
        if not states:
            print("no recorded state at that time")
            return 1
        snap = {"game": g["id"], "tick": None, "check": "recorded on the platform", "obs": {}, "events": {},
                "seconds": min((r.get("time") or {}).get("game_seconds") or 0 for r in states.values())}
        for pos, raw in states.items():
            snap["obs"][pos] = raw["observation"]
            snap["events"][pos] = raw["observation"].get("events") or []
            snap["tick"] = raw["observation"].get("tick")
    else:
        snap = snapshot(db, a.game, parse_time(a.at) / 25)
    seats = {s["position"]: s for s in db.q("SELECT * FROM seats WHERE game_id = ?", g["id"])}
    print(f"game {snap['game']} at {clock(snap['seconds'])} (tick {snap['tick']}); replay check: {snap['check']}")
    for pos, obs in sorted(snap["obs"].items()):
        if a.seat is not None and pos != a.seat:
            continue
        print(f"\n=== seat {pos}: {seats[pos]['bot']} ===")
        print(summary(obs, time_info={"game_seconds": snap["seconds"],
                                      "remaining_seconds": g["minutes"] * 60 - snap["seconds"]}, seat=f"seat {pos}"))
        if a.units:
            print(units_table(obs))
            print("enemies:\n" + enemies_table(obs))
        print("recent events: " + "; ".join(f"{clock(e['tick'] / 25)} {e['kind']} {e['type'] or e['count'] or ''}"
                                             for e in snap["events"].get(pos, [])[-10:]))
        if not a.no_map:
            print(minimap(obs, g["map"]))
    return 0


def cmd_games(a, db) -> int:
    f = _filters(a)
    f["bot"] = a.bot
    print(report.games_table(db, f, limit=a.limit))
    return 0


def cmd_show(a, db) -> int:
    print(report.game_detail(db, a.game, events=a.events, decisions=a.decisions))
    return 0


def cmd_stats(a, db) -> int:
    print(report.stats(db, a.bot, _filters(a), a.by))
    return 0


def cmd_h2h(a, db) -> int:
    print(report.h2h(db, a.a, a.b, _filters(a)))
    print()
    print(report.h2h(db, a.b, a.a, _filters(a)))
    return 0


def cmd_ratings(a, db) -> int:
    bots = a.bots.split(",") if a.bots else None
    print(report.ratings_table(db, _filters(a), bots=bots, by_version=a.by_version))
    if a.matrix:
        print()
        print(report.pairs_matrix(db, _filters(a), bots=bots))
    return 0


def cmd_report(a, db) -> int:
    f = _filters(a)
    if a.last:
        rows = db.q("SELECT finished FROM games WHERE branch_of IS NULL ORDER BY finished DESC LIMIT 1 OFFSET ?", a.last - 1)
        if rows:
            f["since"] = None
            f["_since_ts"] = rows[0]["finished"]
    if f.get("_since_ts"):
        f["since"] = time.strftime("%Y-%m-%d", time.localtime(f["_since_ts"]))
        # (precise cut below)
    focus = a.bots.split(",") if a.bots else None
    if f.get("_since_ts"):
        ts = f.pop("_since_ts")
        orig = report.since_ts
        report.since_ts = lambda text, ts=ts: ts  # the last N games, to the second
        try:
            print(report.observer(db, f, focus))
        finally:
            report.since_ts = orig
        return 0
    print(report.observer(db, f, focus))
    return 0


def cmd_branches(a, db) -> int:
    print(report.branch_report(db, a.game))
    return 0


def cmd_runs(a, db) -> int:
    print(report.runs_table(db, a.limit))
    return 0


def cmd_note(a, db) -> int:
    game = db.find_game(a.game)["id"] if a.game else None
    nid = db.add_note(" ".join(a.text), game_id=game, run_id=a.run, experiment=a.experiment, author=a.author)
    print(f"note {nid} saved")
    return 0


def cmd_experiment(a, db) -> int:
    if a.action == "new":
        db.conn.execute("INSERT INTO experiments (name, created, hypothesis, status) VALUES (?, ?, ?, 'open')",
                        (a.name, time.time(), a.hypothesis or ""))
        print(f"experiment {a.name} opened; pass --experiment {a.name} to play/league/gate/branch")
    elif a.action == "close":
        db.conn.execute("UPDATE experiments SET status = 'closed', conclusion = ?, closed = ? WHERE name = ?",
                        (a.conclusion or "", time.time(), a.name))
        print(f"experiment {a.name} closed")
    elif a.action == "show":
        e = db.one("SELECT * FROM experiments WHERE name = ?", a.name)
        if e is None:
            print(f"no experiment {a.name}")
            return 1
        print(f"{e['name']} ({e['status']}, opened {report._when(e['created'])})\nhypothesis: {e['hypothesis']}\n"
              f"conclusion: {e['conclusion'] or '-'}")
        for r in db.q("SELECT * FROM runs WHERE experiment = ? ORDER BY id", a.name):
            print(f"  run {r['id']} {r['kind']} {r['games']} games: {r['command']}")
        for n in db.q("SELECT * FROM notes WHERE experiment = ? ORDER BY created", a.name):
            print(f"  note: {n['text']}")
    else:
        for e in db.q("SELECT * FROM experiments ORDER BY created DESC"):
            print(f"{e['name']:24} {e['status']:7} {report._when(e['created'])}  {e['hypothesis'][:70]}")
    return 0


def cmd_sql(a, db) -> int:
    rows = db.q(a.query)
    if not rows:
        print("(no rows)")
        return 0
    keys = rows[0].keys()
    print(report._table([[r[k] for k in keys] for r in rows], list(keys)))
    return 0


def cmd_replay(a, db) -> int:
    """Replay a game's engine trace and check every step against it."""
    from lab.branch import snapshot

    g = db.find_game(a.game)
    snap = snapshot(db, a.game, (g["game_seconds"] or 0) + 60)
    print(f"{g['id']}: replayed to tick {snap['tick']}: {snap['check']}")
    return 0 if snap["check"] == "ok" else 1


def cmd_forget(a, db) -> int:
    """Remove games from the database (a test run, a run with a bug): their rows and traces."""
    if a.run is not None:
        ids = [r["id"] for r in db.q("SELECT id FROM games WHERE run_id = ?", a.run)]
    else:
        g = db.find_game(a.game) if a.game else None
        ids = [g["id"]] if g else []
    if not ids:
        print("nothing to forget")
        return 1
    if not a.yes:
        print(f"would remove {len(ids)} game(s) and their traces: {', '.join(report.short(i) for i in ids[:10])}"
              f"{' ...' if len(ids) > 10 else ''}  (add --yes)")
        return 1
    for gid in ids:
        row = db.one("SELECT trace FROM games WHERE id = ?", gid)
        for table, col in (("timeline", "game_id"), ("events", "game_id"), ("decisions", "game_id"), ("seats", "game_id"),
                           ("notes", "game_id"), ("human_log", "game_id"), ("games", "id")):
            db.conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (gid,))
        if row and row["trace"]:
            Path(row["trace"]).unlink(missing_ok=True)
    if a.run is not None:
        db.conn.execute("UPDATE runs SET status = 'forgotten', games = 0 WHERE id = ?", (a.run,))
    print(f"removed {len(ids)} game(s)")
    return 0


def cmd_dashboard(a, db) -> int:
    from lab import dashboard

    out = Path(a.out or LAB_DIR / "dashboard.html")
    dashboard.write(db, out, a.games)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB): open it in a browser")
    return 0


def cmd_status(a, db) -> int:
    from ffsim.engine_openra import LANES_DIR, _engine_alive
    from lab import human

    n = db.one("SELECT COUNT(*) AS n, SUM(branch_of IS NOT NULL) AS b, SUM(time_mode = 'platform') AS p FROM games")
    print(f"database {db.path} ({db.path.stat().st_size / 1e6:.1f} MB): {n['n']} games "
          f"({n['b'] or 0} branches, {n['p'] or 0} from the platform), "
          f"{db.one('SELECT COUNT(*) AS n FROM runs')['n']} runs, "
          f"{db.one('SELECT COUNT(DISTINCT bot) AS n FROM seats')['n']} bots played")
    traces = list((LAB_DIR / "traces").glob("*.trace.gz"))
    print(f"traces: {len(traces)} ({sum(t.stat().st_size for t in traces) / 1e6:.1f} MB)")
    running = db.q("SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 5")
    for r in running:
        print(f"run {r['id']} still marked running ({r['kind']}, started {report._when(r['started'])}): {r['command'][:70]}")
    for lane in sorted(LANES_DIR.glob("lane-*"), key=lambda d: int(d.name.split("-")[1])):
        try:
            info = json.loads((lane / "host.json").read_text())
        except (OSError, ValueError):
            continue
        alive = _engine_alive(info["pid"], info["port"])
        print(f"{lane.name}: engine {'running' if alive else 'gone'}, {info.get('games', 0)} games")
    if human.SESSION_FILE.exists():
        print(f"human session: {human.SESSION_FILE.read_text()}")
    for pid in live_runtimes():
        print(f"the live tournament runtime is running (pid {pid}): lab games run at a lower priority (nice 10)")
    return 0


def live_runtimes() -> list[int]:
    """Processes running ``python -m agent --tournament`` (the live runtime)."""
    out = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"-m" in argv and b"agent" in argv and b"--tournament" in argv:
            out.append(int(proc.name))
    return out


def cmd_import(a, db) -> int:
    from lab.importer import import_recordings

    run_id = db.start_run("import", _command(), {})
    done = import_recordings(db, run_id=run_id)
    db.end_run(run_id)
    print(f"imported {len(done)} platform game(s)" + (": " + ", ".join(d[:8] for d in done) if done else ""))
    return 0


def cmd_human(a, db) -> int:
    from lab import human

    if a.action == "serve":
        a.run_id = a.run_id or None
        return human.serve(a)
    if a.action == "status":
        try:
            info = json.loads(human.SESSION_FILE.read_text())
        except (OSError, ValueError):
            print("no human session")
            return 1
        print(json.dumps(info))
        print(human.client(["state"]))
        return 0
    if a.action == "stop":
        print(human.client(["quit"]))
        try:
            pid = json.loads(human.SESSION_FILE.read_text())["pid"]
            os.kill(pid, 0)
        except (OSError, ValueError, KeyError):
            human.SESSION_FILE.unlink(missing_ok=True)  # (a session that died without cleaning up)
        return 0
    # start
    if human.SESSION_FILE.exists():
        print("a human session is running ('lab human stop' first, or 'lab h state')")
        return 1
    _check_bots([a.vs])
    run_id = db.start_run("human", _command(), vars(a), note=a.note or "", experiment=a.experiment or "")
    log = LAB_DIR / "human" / f"session-{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "human", "serve", "--vs", a.vs, "--map", a.map,
           "--seat", str(a.seat), "--minutes", str(a.minutes), "--autopilot", a.autopilot, "--name", a.name,
           "--run-id", str(run_id)]
    if a.seed is not None:
        cmd += ["--seed", str(a.seed)]
    if a.from_game:
        from lab.branch import game_tick, parse_time

        g = db.find_game(a.from_game)
        cmd += ["--from-game", g["id"], "--at-tick", str(game_tick(g, parse_time(a.at) / 25))]
    subprocess.Popen(cmd, stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True, cwd=ROOT)
    for _ in range(600):
        time.sleep(0.25)
        if human.SESSION_FILE.exists():
            text = human.client(["state"])
            if not text.startswith("starting") and not text.startswith("the game has not started"):
                print(text)
                print("\n(lab h go 5 | build X | train X N | attack X Y all | map | units | enemies | help in docs/LAB.md)")
                return 0
    print(f"the session did not start; see {log}")
    return 1


def cmd_h(a, db) -> int:
    from lab import human

    print(human.client(a.argv))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def filters(sp, vs=True):
        sp.add_argument("--run", type=int)
        sp.add_argument("--map")
        sp.add_argument("--since", help="2h / 3d / 2026-10-14")
        sp.add_argument("--branches", action="store_true", help="include games started from snapshots")
        sp.add_argument("--version", help="current / all / a version hash")
        sp.add_argument("--label", help="games whose label matches (SQL LIKE)")
        sp.add_argument("--failed", action="store_true", help="include games without a result")
        if vs:
            sp.add_argument("--vs", help="only games against this bot")

    def playing(sp):
        sp.add_argument("--map", default="all", help="a map, maps separated by commas, or all (rotating)")
        sp.add_argument("--minutes", type=float, default=10)
        sp.add_argument("--seed", type=int, default=1)
        sp.add_argument("--workers", type=int, default=None, help="games at once (default: the CPU quota)")
        sp.add_argument("--note", default="")
        sp.add_argument("--experiment", default="")
        sp.add_argument("--record", action="store_true", help="also write our agent's recordings (recordings/)")

    s = sub.add_parser("bots", help="the bot registry")
    s.add_argument("action", nargs="?", default="list", choices=["list", "add", "show"])
    s.add_argument("name", nargs="?")
    s.add_argument("--kind", default="ff", choices=registry.KINDS)
    s.add_argument("--knobs", help="ff: JSON knobs")
    s.add_argument("--factory", help="module: package.module:function")
    s.add_argument("--kwargs", help="module: JSON keyword arguments")
    s.add_argument("--type", default="normal", help="ai: normal/rush/turtle/naval")
    s.add_argument("--think", default=str(registry.DEFAULT_THINK), help="seconds of game time per decision")
    s.add_argument("--desc")
    s.add_argument("--tags")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("play", help="N games of A against B")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--games", type=int, default=6)
    playing(s)

    s = sub.add_parser("league", help="every pair of these bots, evenly")
    s.add_argument("bots", nargs="*")
    s.add_argument("--tag", help="add every bot with this tag")
    s.add_argument("--games", type=int, default=30)
    s.add_argument("--balance", action="store_true", help="even out with the games already in the database")
    playing(s)

    s = sub.add_parser("gate", help="is a candidate better than the champion?")
    s.add_argument("candidate")
    s.add_argument("--champion", default="ff-default")
    s.add_argument("--baselines", default="ai-normal,ai-rush,ai-turtle")
    s.add_argument("--games", type=int, default=6, help="per opponent")
    playing(s)

    s = sub.add_parser("branch", help="play on from a moment of a recorded game, with other bots")
    s.add_argument("game")
    s.add_argument("--at", required=True, help="game time: 4:30, 270 (s) or t6750 (tick)")
    s.add_argument("--variants", required=True, help="bots for the branched seat, separated by commas")
    s.add_argument("--seat", type=int, default=0)
    s.add_argument("--opponent", help="another bot for the other seat (an agent seat only)")
    s.add_argument("--repeats", type=int, default=3)
    s.add_argument("--stop-after", type=float, default=None, help="end each branch this many seconds later")
    s.add_argument("--no-control", action="store_true", help="don't add the original bot as a control branch")
    playing(s)

    s = sub.add_parser("look", help="the board at a moment of a recorded game")
    s.add_argument("game")
    s.add_argument("--at", required=True)
    s.add_argument("--seat", type=int)
    s.add_argument("--units", action="store_true")
    s.add_argument("--no-map", action="store_true")

    s = sub.add_parser("replay", help="check that a game's trace replays identically")
    s.add_argument("game")

    s = sub.add_parser("games", help="recent games")
    s.add_argument("--bot")
    s.add_argument("--limit", type=int, default=30)
    filters(s)

    s = sub.add_parser("show", help="one game in detail")
    s.add_argument("game")
    s.add_argument("--events", type=int, default=40)
    s.add_argument("--decisions", type=int, default=0, help="show this many of each seat's reasoning changes")

    s = sub.add_parser("stats", help="a bot's results")
    s.add_argument("bot")
    s.add_argument("--by", choices=["opponent", "map", "faction", "seat", "version", "opp_faction"])
    filters(s)

    s = sub.add_parser("h2h", help="two bots against each other")
    s.add_argument("a")
    s.add_argument("b")
    filters(s, vs=False)

    s = sub.add_parser("ratings", help="ratings of every bot (Bradley-Terry)")
    s.add_argument("--bots", help="only games among these (comma-separated)")
    s.add_argument("--by-version", action="store_true")
    s.add_argument("--matrix", action="store_true")
    filters(s)

    s = sub.add_parser("report", help="what a batch of games says (an observer's report)")
    s.add_argument("--last", type=int, help="the last N games")
    s.add_argument("--bots", help="focus on these bots")
    filters(s)

    s = sub.add_parser("branches", help="results of the branches of a game")
    s.add_argument("game")

    s = sub.add_parser("runs", help="recent runs")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("note", help="write down an observation")
    s.add_argument("text", nargs="+")
    s.add_argument("--game")
    s.add_argument("--run", type=int)
    s.add_argument("--experiment")
    s.add_argument("--author", default="claude")

    s = sub.add_parser("experiment", help="experiments: new / close / show / list")
    s.add_argument("action", choices=["new", "close", "show", "list"])
    s.add_argument("name", nargs="?")
    s.add_argument("--hypothesis")
    s.add_argument("--conclusion")

    s = sub.add_parser("sql", help="any query on the lab database")
    s.add_argument("query")

    s = sub.add_parser("human", help="a game with you at the controls: start / status / stop")
    s.add_argument("action", choices=["start", "status", "stop", "serve"])
    s.add_argument("--vs", default="ai-normal")
    s.add_argument("--map", default="trapped")
    s.add_argument("--seat", type=int, default=0)
    s.add_argument("--minutes", type=float, default=10)
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--autopilot", default="macro", choices=["off", "macro", "full"])
    s.add_argument("--name", default="claude")
    s.add_argument("--from", dest="from_game", help="start from a moment of this game (with --at)")
    s.add_argument("--at", default="0:00")
    s.add_argument("--from-game", dest="from_game")
    s.add_argument("--at-tick", type=int)
    s.add_argument("--run-id", type=int)
    s.add_argument("--note", default="")
    s.add_argument("--experiment", default="")

    sub.add_parser("import", help="import the games our runtime played on the platform (recordings/)")
    sub.add_parser("status", help="the database, engine lanes and any human session")

    s = sub.add_parser("dashboard", help="the database as an HTML page (sim/lab/dashboard.html)")
    s.add_argument("--out")
    s.add_argument("--games", type=int, default=200, help="how many recent games it includes")

    s = sub.add_parser("forget", help="remove a run's games (or one game) from the database")
    s.add_argument("--run", type=int)
    s.add_argument("--game")
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("h", help="a command for the human seat (see lab/human.py)")
    s.add_argument("argv", nargs=argparse.REMAINDER)

    a = p.parse_args()
    db = LabDB()
    handlers = {"bots": cmd_bots, "play": cmd_play, "league": cmd_league, "gate": cmd_gate, "branch": cmd_branch,
                "look": cmd_look, "replay": cmd_replay, "games": cmd_games, "show": cmd_show, "stats": cmd_stats,
                "h2h": cmd_h2h, "ratings": cmd_ratings, "report": cmd_report, "branches": cmd_branches,
                "runs": cmd_runs, "note": cmd_note, "import": cmd_import, "status": cmd_status, "forget": cmd_forget, "dashboard": cmd_dashboard, "experiment": cmd_experiment, "sql": cmd_sql, "human": cmd_human,
                "h": cmd_h}
    return handlers[a.command](a, db)


if __name__ == "__main__":
    raise SystemExit(main())
