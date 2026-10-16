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
    from lab.batch import slot_count

    return slot_count()  # (more workers than the machine's game slots would only wait)


def _split(text: str) -> list[str]:
    """Bot names separated by commas; a ``knobs:a=1,b=2`` name keeps its own commas."""
    out: list[str] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        if out and out[-1].startswith("knobs:") and "=" in part and ":" not in part:
            out[-1] += "," + part
        else:
            out.append(part)
    return out


def _expand(names: list[str]) -> list[str]:
    """Bot names, with ``@suite`` replaced by the suite's bots (lab/suites.json)."""
    suites = json.loads((ROOT / "lab" / "suites.json").read_text())
    out: list[str] = []
    for n in names:
        if n.startswith("@"):
            if n[1:] not in suites or n[1:].startswith("_"):
                raise SystemExit(f"no suite {n}: {', '.join('@' + k for k in suites if not k.startswith('_'))}")
            out += suites[n[1:]]
        else:
            out.append(n)
    return list(dict.fromkeys(out))


def _check_bots(names: list[str]) -> None:
    table = registry.load()
    for n in names:
        registry.get(n, table)


def _play(db: LabDB, jobs: list[dict], a, kind: str, params: dict, sequential=None) -> int:
    """``sequential(run_id, jobs)``: a test (lab/sprt.py ``Sequential``) that decides after every game
    which jobs still need playing."""
    from lab import batch

    jev_bots = sorted({n for j in jobs for n in j["bots"] if _uses_jev(n)})
    if jev_bots:
        from bots.jev_bot import API_URL, Jev

        if Jev().url == API_URL:  # (paid: TypeSafe's credits; a local server, JEV_URL, is free)
            paid = sum(1 for j in jobs if any(_uses_jev(n) for n in j["bots"]))
            allowed = batch.paid_jev_allowed()
            if paid > allowed:
                print(f"[lab] {paid} game(s) would ask paid Jev ({', '.join(jev_bots)}; ~250 calls a seat in a 20-minute "
                      f"game), and this run may play {allowed}. Paid credits are for experiments that need Jev's "
                      f"answers: probe a moment or play a few games, not a suite. To go ahead: "
                      f"{batch.PAID_JEV}={paid} lab ... (or point JEV_URL at a local server). Not starting.", flush=True)
                raise SystemExit(2)
            from lab import jevlog

            seats, cost = batch.jev_estimate(jobs, _uses_jev)
            spent, budget = jevlog.spent(), batch.jev_budget_per_hour()
            if spent + cost > budget:
                print(f"[lab] paid Jev budget: ${budget:.2f} an hour (all sessions); spent in the last hour "
                      f"${spent:.3f}, and this run's {seats} Jev seat(s) would cost up to ${cost:.3f}. Play fewer "
                      f"games, or wait. Not starting.", flush=True)
                raise SystemExit(2)
            params = {**params, "paid_jev_games": paid, "paid_jev_estimate": round(cost, 4)}
            print(f"[lab] paid Jev: {paid} game(s) (allowed {allowed}), {seats} seat(s), up to ${cost:.3f}; "
                  f"last hour ${spent:.3f} of ${budget:.2f}", flush=True)
        problem = Jev().check()
        if problem:
            print(f"[lab] {', '.join(jev_bots)} need Jev, and {problem}: they would play on frozen decisions. "
                  f"Not starting.", flush=True)
            raise SystemExit(2)
    reused: list[str] = []
    if kind in REUSE_KINDS and not getattr(a, "no_reuse", False):
        from lab import reuse

        jobs, reused = reuse.split(db, jobs, registry.load(), _uses_jev)
    run_id = db.start_run(kind, _command(), params, note=a.note or "", experiment=a.experiment or "")
    if reused:
        from lab import reuse

        reuse.link(db, run_id, reused)
        print(f"[lab] run {run_id}: {len(reused)} game(s) already played the same way (same bot versions, seats, map, "
              f"seed, length and engine build): reused, not played again (--no-reuse plays them)", flush=True)
    workers = max(1, min(a.workers or _quota(), len(jobs)))
    if jobs:
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

    test = _play.last_sequential = sequential(run_id, jobs) if sequential else None
    if test is not None:
        def progress(msg, k, n, _show=progress):
            _show(msg, k, n)
            before = dict(test.verdict)
            test.update()
            for c, v in test.verdict.items():
                if v and not before.get(c):
                    print(f"[lab] --sprt: {c} is {v.upper()} after {test.pairs[c]} pairs: its remaining games are "
                          f"not played", flush=True)

    status = "done"
    try:
        if jobs:
            batch.run(jobs, workers=workers, run_id=run_id, record=a.record, on_result=progress,
                      keep=test.keep if test is not None else None)
    except KeyboardInterrupt:
        status = "interrupted"
        print("[lab] interrupted", flush=True)
        if test is not None:
            test.stopped = True
    finally:
        db.end_run(run_id, status)
    wall = time.monotonic() - t0
    n = db.one("SELECT COUNT(*) AS n FROM games WHERE run_id = ?", run_id)["n"]
    print(f"[lab] run {run_id}: {n} games in {wall:.0f} s ({wall / max(n, 1):.1f} s a game)"
          + (f", plus {len(reused)} reused" if reused else ""))
    if jev_bots:
        from lab import jevlog

        warning = jevlog.run_health(db, run_id)
        if warning:
            print(warning)
            db.add_note(warning, run_id=run_id, author="lab")
    return run_id


REUSE_KINDS = {"play", "league", "gate", "compare", "exploit"}  # (whole games: lab/reuse.py)


def _uses_jev(name: str) -> bool:
    """A bot that asks Jev (a module bot built on bots/jev_bot.py, frozen or not; not a forced one). A
    frozen council's factory path needn't say "jev" (bots.frozen.ff_council_champ_v10): ``oracle: jev``
    in its kwargs does."""
    try:
        bot = registry.get(name)
    except KeyError:
        return False
    factory = str(bot.params.get("factory", "")) if bot.kind == "module" else ""
    kwargs = bot.params.get("kwargs") or {}
    return (("jev" in factory or (factory and kwargs.get("oracle") == "jev")) and not kwargs.get("force")
            and kwargs.get("use_jev", True) and kwargs.get("oracle", "jev") == "jev")


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
        try:
            source = registry.owner(a.name)
        except ValueError as exc:
            raise SystemExit(str(exc))
        if a.name in table and table[a.name].source != source:
            raise SystemExit(f"{a.name} is registered in {table[a.name].source or 'lab/bots.json'}: replace it there")
        table[a.name] = registry.Bot(name=a.name, kind=a.kind, params=params, think=think, description=a.desc or "",
                                     tags=[t for t in (a.tags or "").split(",") if t], source=source)
        registry.save(table)
        print(f"added {a.name} to {f'lab/bots.d/{source}.json' if source else 'lab/bots.json'}: {table[a.name].version()}")
        return 0
    if a.action == "freeze":
        if not a.as_name:
            print("name the copy: lab bots freeze <bot> --as <new name>")
            return 1
        table = registry.load()
        if a.as_name in table:
            print(f"{a.as_name} exists")
            return 1
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                cwd=ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        try:
            frozen = registry.freeze(table, a.name, a.as_name, commit=commit + ("+changes" if dirty else ""))
        except ValueError as exc:
            raise SystemExit(str(exc))
        registry.save(table)
        print(f"froze {a.name} as {a.as_name}: {frozen.params['factory']} ({frozen.version()})")
        return 0
    if a.action == "tag":
        table = registry.load()
        if a.name not in table:
            raise SystemExit(f"no bot {a.name!r} in the registry")
        bot = table[a.name]
        from lab import FAMILY

        if FAMILY and bot.source != FAMILY:
            raise SystemExit(f"{a.name} isn't the {FAMILY} family's (AGENTS.md): tag your own bots")
        tags = set(bot.tags)
        for t in _split(a.tags or ""):
            (tags.discard if t.startswith("-") else tags.add)(t.lstrip("+-"))
        bot.tags = sorted(tags)
        registry.save(table)
        print(f"{a.name}: tags {', '.join(bot.tags) or '(none)'}")
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
    names = _expand(list(a.bots))
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


def sprt_args(s) -> None:
    s.add_argument("--sprt", action="store_true",
                   help="stop when clear: a sequential test of each candidate's paired points against the reference "
                        "(the champion) after every game (lab/sprt.py); --games is then the cap")
    s.add_argument("--bounds", default="0,0.1", help="--sprt: H0,H1 in points a game over the reference (default 0,0.1; "
                                                     "'no worse': -0.1,0)")
    s.add_argument("--alpha", type=float, default=0.05, help="--sprt: false 'better' rate")
    s.add_argument("--beta", type=float, default=0.05, help="--sprt: missed 'better' rate (at H1)")
    s.add_argument("--score", choices=["points", "margin"], default="margin",
                   help="--sprt: what a game is worth: points (1 / 0.5 / 0), or margin: points + 0.25 x net / 20,000, "
                        "capped at +-0.25 (bigger wins count more; a loss never above a win)")


def _sequential(a, db, candidates: list[str]):
    """``--sprt``: the test _play runs after every game (None without it)."""
    if not a.sprt:
        return None
    from lab import sprt

    t0, t1 = sprt.parse_bounds(a.bounds)
    return lambda run_id, jobs: sprt.Sequential(db, run_id, jobs, candidates, t0, t1, a.alpha, a.beta,
                                                margin=a.score == "margin")


def cmd_gate(a, db) -> int:
    baselines = _expand(_split(a.baselines))
    _check_bots([a.candidate, a.champion] + baselines)
    jobs = schedule.gate(a.candidate, a.champion, baselines, a.games, maps=_maps(a.map), seed=a.seed, minutes=a.minutes,
                         interleaved=bool(a.sprt))
    run_id = _play(db, jobs, a, "gate", vars(a), sequential=_sequential(a, db, [a.champion, a.candidate]))
    print()
    print(report.gate_report(db, run_id, a.candidate, a.champion))
    if a.sprt:
        print(_play.last_sequential.summary())
        print("(with --sprt the gate's answer is the test's, on the pairs against the baselines; the head-to-head "
              "games and each baseline's own interval above must not say worse)")
    return 0


def cmd_compare(a, db) -> int:
    candidates = _expand(_split(a.candidates))
    baselines = _expand(_split(a.baselines))
    _check_bots(candidates + baselines)
    if a.sprt and len(candidates) < 2:
        raise SystemExit("--sprt tests candidates against the first (the reference): name two or more")
    sequential = _sequential(a, db, candidates)
    jobs = schedule.compare(candidates, baselines, a.games, maps=_maps(a.map), seed=a.seed, minutes=a.minutes,
                            interleaved=bool(a.sprt), h2h=a.h2h)
    run_id = _play(db, jobs, a, "compare", vars(a), sequential=sequential)
    print()
    print(report.compare_report(db, run_id, candidates, baselines))
    if a.sprt:
        print(_play.last_sequential.summary())
    return 0


def cmd_exploit(a, db) -> int:
    from lab import exploit

    _check_bots([a.target])
    tries = exploit.sample(a.tries, seed=a.seed)
    print(f"stage 1: {len(tries)} settings x {a.games} games against {a.target}")
    run1 = _play(db, exploit.jobs(a.target, tries, a.games, maps=_maps(a.map), seed=a.seed, minutes=a.minutes,
                                  stage="1"), a, "exploit", vars(a))
    ranked = exploit.ranking(db, [run1], a.target, tries)
    print(exploit.ranking_text(ranked))
    finalists = [r["bot"] for r in ranked[:a.finalists]]
    if not finalists or a.final_games <= 0:
        return 0
    print(f"\nstage 2: the best {len(finalists)} x {a.final_games} more games")
    run2 = _play(db, exploit.jobs(a.target, finalists, a.final_games, maps=_maps(a.map), seed=a.seed + 101,
                                  minutes=a.minutes, stage="2"), a, "exploit", vars(a))
    final = exploit.ranking(db, [run1, run2], a.target, finalists)
    print(exploit.ranking_text(final))
    best = final[0]
    print(f"\nbest: {best['bot']} scored {100 * best['score']:.0f}% against {a.target} over {best['games']} games")
    print(f"keep it: lab bots add exploit-NAME --kind ff --knobs '{json.dumps(registry.parse_knobs(best['bot'][6:]))}' "
          f"--tags exploiter --desc 'beats {a.target} {100 * best['score']:.0f}%'")
    return 0


def cmd_branch(a, db) -> int:
    from lab.branch import branch_jobs, parse_time

    seconds = parse_time(a.at) / 25
    variants = _split(a.variants)
    _check_bots(variants + ([a.opponent] if a.opponent else []))
    # A branch from one moment replays one way (the engine keeps the game's seed), so repeats branch
    # 2 s apart: T, T+2 s, ... (each a real trajectory of its own).
    if a.repeats > 1:
        print(f"note: {a.repeats} repeats branch at {', '.join(clock(seconds + 2 * r) for r in range(a.repeats))} "
              f"(repeats of one moment would replay identically)")
    jobs = []
    for r in range(max(1, a.repeats)):
        jobs += branch_jobs(db, a.game, seconds + 2.0 * r, variants, seat=a.seat, opponent=a.opponent, repeats=1,
                            seed=a.seed + r, stop_after=a.stop_after, control=not a.no_control)
    _play(db, jobs, a, "branch", vars(a))
    print()
    print(report.branch_report(db, a.game))
    return 0


def cmd_moments(a, db) -> int:
    """Named sets of moments to probe from (lab/moments.py)."""
    from lab import moments
    from lab.branch import parse_time

    if a.action == "list" or (a.action == "show" and not a.name):
        rows = moments.sets(db)
        if not rows:
            print("no moment sets (lab moments find NAME --kind wiped ...)")
        for r in rows:
            print(f"@{r['set_name']:24} {r['n']:4} moments  ({r['kinds']})")
        return 0
    if not a.name:
        raise SystemExit("name the set: lab moments find NAME ...")
    name = a.name.lstrip("@")
    if a.action == "show":
        rows = moments.moments(db, name)
        for m in rows:
            print(f"{m['id']:5}  {report.short(m['game_id'])}  seat {m['seat']}  {clock(m['seconds'])}  {m['kind']:13} {m['why']}")
        print(f"@{name}: {len(rows)} moments")
        return 0
    if a.action == "drop":
        n = moments.drop(db, name, [int(x) for x in _split(a.ids)] if a.ids else None)
        print(f"@{name}: dropped {n} moment(s)")
        return 0
    if a.action == "add":
        if not (a.game and a.at):
            raise SystemExit("lab moments add NAME --game GAME --at 13:00 [--seat 0] [--why ...]")
        g = db.find_game(a.game)
        if g is None:
            raise SystemExit(f"no game {a.game!r}")
        found = [{"game_id": g["id"], "seat": a.seat or 0, "seconds": parse_time(a.at) / 25, "kind": "manual",
                  "why": a.why or ""}]
    else:  # find
        f = {"runs": [int(x) for x in _split(a.runs)] if a.runs else None, "bot": a.bot, "vs": a.vs, "map": a.map}
        found = moments.find(db, a.kind, f, lead=a.lead, at=parse_time(a.at) / 25 if a.at else None, drop=a.drop,
                             window=a.window, after=parse_time(a.after) / 25 if a.after else None,
                             before=parse_time(a.before) / 25 if a.before else None)
        if a.limit:
            found = found[:a.limit]
        for m in found:
            print(f"  {report.short(m['game_id'])}  seat {m['seat']}  {clock(m['seconds'])}  {m['why']}")
    if a.dry_run:
        print(f"{len(found)} moment(s) found (dry run: not saved)")
        return 0
    new = moments.save(db, name, found)
    print(f"@{name}: {new} new moment(s), {len(moments.moments(db, name))} in the set")
    return 0


def cmd_probe(a, db) -> int:
    """Branch every moment of a set with the variants and the control, and compare (lab/moments.py)."""
    from lab import moments

    name = a.set.lstrip("@")
    rule = {"stop_after": a.stop_after, "stop_quiet": a.stop_quiet}
    if a.report:
        print(moments.probe_report(db, name, [int(x) for x in _split(a.runs)] if a.runs else None, **rule))
        return 0
    variants = _split(a.variants or "")
    if not variants and not a.control_only:
        raise SystemExit("name the variants: lab probe SET --variants A,B (or --control-only)")
    _check_bots(variants)
    controls = {db.one("SELECT bot FROM seats WHERE game_id = ? AND position = ?", m["game_id"], m["seat"])["bot"]
                for m in moments.moments(db, name)}
    if set(variants) & controls:
        print(f"note: {', '.join(sorted(set(variants) & controls))} played some of these moments: there it is the "
              f"control, and only other moments test it as a variant")
    opts = dict(rule, spread=a.spread, seed=a.seed, control=not a.no_control, reuse=not a.no_reuse)

    def play(ids, alive, rnd=None):
        jobs, reused = moments.probe_jobs(db, name, alive, moment_ids=ids, limit=a.limit, **opts)
        print(f"[probe] {f'round {rnd}: ' if rnd else ''}{len(alive)} variant(s) on "
              f"{len(ids) if ids is not None else 'the'} moments: {len(jobs)} branch(es) to play, {reused} reused",
              flush=True)
        if jobs:
            _play(db, jobs, a, "probe", {**vars(a), "set": name, "round": rnd})

    chosen = [int(x) for x in _split(a.moments)] if a.moments else None
    if not a.halving:
        play(chosen, variants)
        print()
        print(moments.probe_report(db, name, **rule))
        return 0
    if a.no_control:
        raise SystemExit("--halving compares to the control: drop --no-control")
    order = moments.halving_order(db, name, a.seed)
    if chosen:
        order = [m for m in order if m in set(chosen)]
    if a.limit:
        order = order[:a.limit]
    m, alive, dropped, rnd = min(a.start, len(order)), list(variants), {}, 1
    while True:
        ids = order[:m]
        play(ids, alive, rnd)
        stats = moments.variant_stats(moments.outcomes(db, name, moment_ids=ids, **rule))
        for v in sorted(alive, key=lambda v: -stats.get(v, {"mean": float("-inf")})["mean"]):
            st = stats.get(v)
            print(f"   {v:32} " + (f"{st['mean']:+8,.0f} over {st['n']} ({st['verdict']})" if st else "no pairs"))
        if m >= len(order) or len(alive) <= 1:
            break
        keep = moments.survivors({v: stats[v] for v in alive if v in stats})
        for v in alive:
            if v not in keep:
                dropped[v] = rnd
        alive, m, rnd = keep, min(len(order), m * 2), rnd + 1
    print()
    print(moments.probe_report(db, name, dropped=dropped, **rule))
    return 0


def cmd_board(a, db) -> int:
    """The board: every family's entries against the champion on one schedule (lab/board.py)."""
    from lab import FAMILY, board

    cfg = board.config()
    if a.action == "show":
        print(board.report(db, cfg))
        return 0
    if a.action == "history":
        print(board.history(db, a.bot))
        return 0
    if a.action == "play":
        try:
            bots = _split(a.entries) if a.entries else board.entries(registry.load(), cfg)
        except ValueError as exc:
            raise SystemExit(str(exc))
        if not bots:
            print(f"no board entries in {'the ' + FAMILY + ' family' if FAMILY else 'this checkout'} "
                  f"(tag a frozen bot 'board': lab bots add ... --tags board, or freeze one)")
            return 0
        _check_bots(bots + cfg["baselines"])
        jobs, reused = board.plan(db, bots, cfg)
        print(f"[board] {', '.join(bots)}: {len(jobs)} game(s) to play, {reused} recorded already", flush=True)
        if jobs:
            _play(db, jobs, a, "board", {"entries": bots, "board": cfg})
        return 0
    # run: the coordinator plays the main checkout's entries, then each family's from its worktree.
    if FAMILY:
        raise SystemExit("lab board run is the coordinator's (AGENTS.md); a family plays its entries with lab board play")
    import subprocess

    from lab import MAIN_ROOT

    a.action = "play"
    a.entries = None
    cmd_board(a, db)
    worktrees = MAIN_ROOT / ".worktrees"
    for wt in sorted(worktrees.iterdir()) if worktrees.exists() else []:
        if not (wt / "scripts" / "lab.py").exists():
            continue
        print(f"[board] family {wt.name}:", flush=True)
        args = [sys.executable, str(wt / "scripts" / "lab.py"), "board", "play", "--note", a.note or "board run"]
        if a.workers:
            args += ["--workers", str(a.workers)]
        subprocess.run(args, cwd=wt, check=False)
    print()
    print(board.report(db, cfg))
    n = board.save_snapshot(db, cfg)
    top = board.standings(db, cfg)[:3]
    db.add_note("board: " + "; ".join(f"{r['bot']} {100 * r['points']:.0f}%" + (f" ({r['diff']:+.2f} vs ref)" if r["diff"] is not None else "")
                                      for r in top), author="main")
    print(f"(saved a snapshot of {n} entries: lab board history; the top 3 are in a lab note)")
    return 0


POSTURES = ("defend", "gather", "commit", "retreat")


def cmd_coach(a, db) -> int:
    """What would each posture have scored from this moment? Branches with the posture forced."""
    from lab.branch import branch_jobs, game_tick, parse_time

    seconds = parse_time(a.at) / 25
    variants = [f"ff-jev-force-{p}" for p in POSTURES]
    _check_bots(variants)
    # Branches from one moment replay the same way (the engine keeps the game's seed): the spread
    # comes from branching at a few nearby moments instead (T, T+2 s, T+4 s, ...).
    offsets = [2.0 * k for k in range(max(1, a.spread))]
    jobs = []
    for off in offsets:
        jobs += branch_jobs(db, a.game, seconds + off, variants, seat=a.seat, repeats=1, seed=a.seed,
                            stop_after=a.stop_after, control=True)
    _play(db, jobs, a, "coach", vars(a))
    g = db.find_game(a.game)
    origin = g["branch_of"] or g["id"]
    outcomes: dict = {}
    for off in offsets:
        for label, nets in report.branch_outcomes(db, origin, game_tick(db.find_game(origin), seconds + off)).items():
            outcomes.setdefault(label, []).extend(nets)
    print()
    print(report.branch_report(db, a.game))
    forced = {lab.split("ff-jev-force-")[1]: sum(v) / len(v) for lab, v in outcomes.items() if "ff-jev-force-" in lab and v}
    if not forced:
        return 1
    best = max(forced.values())
    good = [p for p, v in sorted(forced.items(), key=lambda kv: -kv[1]) if v >= best - a.margin]
    # Fidelity: does the control (the same bot, taking over fresh) play on as the original did?
    tick = game_tick(db.find_game(origin), seconds)
    seat_row = db.one("SELECT net FROM seats WHERE game_id = ? AND position = ?", origin, a.seat)
    at = db.one("SELECT kills_cost - deaths_cost AS net FROM timeline WHERE game_id = ? AND position = ? AND tick <= ? "
                "ORDER BY tick DESC LIMIT 1", origin, a.seat, tick)
    control = [v for lab, v in outcomes.items() if lab.startswith("control:")]
    faithful = True
    if seat_row and at and control and control[0]:
        original = (seat_row["net"] or 0) - (at["net"] or 0)
        mean_control = sum(control[0]) / len(control[0])
        faithful = abs(mean_control - original) <= max(1000, 0.25 * abs(original))
        print(f"fidelity: the original game went {original:+d} from here, the control branches {mean_control:+.0f}"
              + ("" if faithful else ": they don't reproduce it (a fresh bot, a re-seeded opponent): don't trust the labels"))
    print("\nforced postures, mean final net: " + ", ".join(f"{p} {v:+.0f}" for p, v in sorted(forced.items(), key=lambda kv: -kv[1])))
    print(f"best: {', '.join(good)} (within {a.margin} of the best)")
    if a.label and not faithful:
        print("no case saved (the branches don't reproduce the original game)")
    elif a.label:
        from lab import jev_eval, jevlog

        paths = jevlog.log_files(origin)
        if not paths:
            print(f"no Jev log for {origin}: no case")
            return 0
        rows = jevlog.read(paths[0] if len(paths) == 1 else next(p for p in paths if p.name.endswith(f"-{a.seat}.jsonl")))
        case = jev_eval.case_from_log(rows, seconds, {"posture": good}, game=origin,
                                      why="coach: " + ", ".join(f"{p} {v:+.0f}" for p, v in forced.items()))
        case["id"] = "coach-" + case["id"]
        cases = [c for c in jev_eval.load_cases() if c["id"] != case["id"]] + [case]
        jev_eval.save_cases(cases)
        print(f"case {case['id']} saved: posture {'/'.join(good)} (the bot said: {case['bot_said']})")
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


def cmd_watch(a, db) -> int:
    """The game as a time-lapse: every N seconds, both sides' score and the map of one seat."""
    from lab.branch import parse_time, timelapse
    from lab.views import minimap

    g = db.find_game(a.game)
    if g is None:
        print(f"no game {a.game!r}")
        return 1
    seats = {s["position"]: s for s in db.q("SELECT * FROM seats WHERE game_id = ?", g["id"])}
    start = parse_time(a.start) / 25 if a.start else 0.0
    end = parse_time(a.end) / 25 if a.end else None
    center = tuple(int(v) for v in a.zoom.split(",")[:2]) if a.zoom else None
    radius = int(a.zoom.split(",")[2]) if a.zoom and len(a.zoom.split(",")) > 2 else 12
    for seconds, obs in timelapse(db, g["id"], parse_time(a.every) / 25, start, end):
        parts = []
        for pos, o in sorted(obs.items()):
            mil, eco = o.get("military") or {}, o.get("economy") or {}
            combat = sum(1 for u in o.get("units") or [] if not u.get("wreck") and u["type"] not in ("harv", "mcv"))
            parts.append(f"seat {pos} {seats[pos]['bot']}: net {(mil.get('kills_cost') or 0) - (mil.get('deaths_cost') or 0):+d} "
                         f"army {combat} (${mil.get('army_value')}) cash {eco.get('cash')}+{eco.get('ore')} "
                         f"bldgs {len(o.get('buildings') or [])} enemies seen {sum(1 for e in o.get('visible_enemies') or [] if not e.get('wreck'))}")
        print(f"=== {clock(seconds)}  " + "  |  ".join(parts))
        if not a.no_map:
            pos = a.seat if a.seat is not None else min(obs)
            if pos in obs:
                print(minimap(obs[pos], g["map"], center=center, radius=radius))
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
    if a.stale or a.close_stale:
        from lab.batch import stale_runs

        stale = stale_runs(db)
        for r in stale:
            print(f"run {r['id']} ({json.loads(r['params'] or '{}').get('family', '?')}, started "
                  f"{report._when(r['started'])}, last game {report._when(r['last_game']) if r['last_game'] else 'none'}): "
                  f"{r['command'][:70]}")
        if not stale:
            print("no stale runs")
        if a.close_stale:
            for r in stale:
                db.end_run(r["id"], "stopped")
            print(f"marked {len(stale)} run(s) stopped (their games stay)")
        return 0
    print(report.runs_table(db, a.limit))
    return 0


def cmd_slots(a, db) -> int:
    """The machine's game slots for every session: ``lab slots`` (show), ``lab slots N``, ``lab slots off``."""
    from lab import FAMILY, batch

    if a.reserve:
        batch.reserve_forever()
        return 0
    if a.limit is not None:
        if FAMILY:
            print("the slot limit is the coordinator's (AGENTS.md: a fair share of the machine)")
            return 1
        batch.set_slot_limit(None if a.limit == "off" else int(a.limit))
        if batch.slot_limit() and not batch.reserver_pid():
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "slots", "--reserve"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(ROOT))
    limit, n = batch.slot_limit(), batch.all_slots()
    print(f"game slots: {batch.slot_count()} of the machine's {n}" + (f" (lab slots {limit}: the rest reserved"
          f"{', reserver pid ' + str(batch.reserver_pid()) if batch.reserver_pid() else ', reserver starting'})" if limit else ""))
    return 0


def cmd_superweapons(a, db) -> int:
    """Games an OpenRA AI's superweapon may have decided (lab/superweapons.py): flag, then list."""
    from lab import superweapons

    if a.flag:
        new = superweapons.flag_all(db)
        print(f"flagged {len(new)} more game(s)")
    rows = db.q("SELECT g.id, g.map, g.finished, g.branch_of, json_extract(g.info, '$.superweapon') AS hit FROM games g "
                "WHERE json_extract(g.info, '$.superweapon') IS NOT NULL ORDER BY g.finished")
    hits = [(r, json.loads(r["hit"])) for r in rows]
    if a.bot:
        hits = [(r, h) for r, h in hits
                if db.one("SELECT 1 FROM seats WHERE game_id = ? AND position = ? AND bot = ?", r["id"], h["position"], a.bot)]
    decided = [(r, h) for r, h in hits if h.get("decided")]
    print(f"{len(hits)} game(s) with a superweapon hit ({superweapons.MIN_BUILDINGS}+ buildings in "
          f"{superweapons.WINDOW // 25} s), {sum(1 for r, _ in hits if r['branch_of'] is None)} whole; "
          f"{len(decided)} likely decided by it (level or ahead before, lost)")
    table = [[report.short(r["id"]), r["map"], "branch" if r["branch_of"] else "", h["at"], h["buildings"],
              f"{h['net_before']:+,}" if h.get("net_before") is not None else "?", f"{h['net_end'] or 0:+,}", h["result"],
              "yes" if h.get("decided") else ""] for r, h in (decided if a.decided else hits)[-(a.limit or 40):]]
    if table:
        print(report._table(table, ["game", "map", "", "hit at", "bldgs", "net before", "net end", "result", "decided"]))
    return 0


def cmd_note(a, db) -> int:
    game = db.find_game(a.game)["id"] if a.game else None
    from lab import FAMILY

    nid = db.add_note(" ".join(a.text), game_id=game, run_id=a.run, experiment=a.experiment,
                      author=a.author or FAMILY or "main")
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
    from lab import FAMILY

    if a.run is not None:
        ids = [r["id"] for r in db.q("SELECT id FROM games WHERE run_id = ?", a.run)]
    else:
        g = db.find_game(a.game) if a.game else None
        ids = [g["id"]] if g else []
    if FAMILY and ids:  # (a family session forgets only its own runs' games: the lab is everyone's, AGENTS.md)
        rows = db.q(f"SELECT g.id, g.run_id, json_extract(r.params, '$.family') AS family FROM games g "
                    f"LEFT JOIN runs r ON r.id = g.run_id WHERE g.id IN ({','.join('?' * len(ids))})", *ids)
        theirs = sorted({str(r["run_id"]) for r in rows if r["family"] != FAMILY})
        if theirs:
            raise SystemExit(f"the {FAMILY} family forgets only games of its own runs (AGENTS.md): "
                             f"run(s) {', '.join(theirs)} aren't (ask the coordinator)")
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


def cmd_jev(a, db) -> int:
    from lab import jevlog
    from lab.branch import parse_time

    if a.attacks:
        print(jevlog.attack_table(db, jevlog.log_files()))
        return 0
    if a.game == "eval":
        from lab import jev_eval

        from bots.jev_bot import API_URL, Jev
        from lab import batch

        cases = jev_eval.load_cases()
        if Jev().url == API_URL and not batch.paid_jev_allowed():
            print(f"lab jev eval asks paid Jev once a case ({len(cases)} cases"
                  f"{', x3 loops' if a.council else ''}): run it as {batch.PAID_JEV}=1 lab jev eval ... "
                  f"(or point JEV_URL at a local server)")
            return 2
        variant = json.loads(Path(a.questions).read_text()) if a.questions else None
        print(f"{len(cases)} cases" + (f", variant {a.questions}" if a.questions else "")
              + (", asked as the council (bots/jev_council.py)" if a.council else ""))
        print(jev_eval.report(jev_eval.evaluate_council(cases) if a.council else jev_eval.evaluate(cases, variant)))
        return 0
    if a.game == "case":
        from lab import jev_eval

        if not (a.rest and a.at and a.expect):
            print("lab jev case GAME --at 7:40 --expect posture=defend,gather[;production=mixed] --why '...'")
            return 1
        g = db.find_game(a.rest[0])
        gid = g["id"] if g else a.rest[0]
        paths = jevlog.log_files(gid)
        if not paths:
            print(f"no Jev log for {gid}")
            return 1
        expect = {}
        for part in a.expect.split(";"):
            q, _, ok = part.partition("=")
            expect[q.strip()] = [x.strip() for x in ok.split(",") if x.strip()]
        case = jev_eval.case_from_log(jevlog.read(paths[a.seat or 0] if a.seat is not None else paths[0]),
                                      parse_time(a.at) / 25, expect, game=gid, why=a.why or "")
        cases = [c for c in jev_eval.load_cases() if c["id"] != case["id"]] + [case]
        jev_eval.save_cases(cases)
        print(f"case {case['id']} saved ({len(cases)} in all); the bot said: {case['bot_said']}")
        return 0
    if a.game is None:
        print(jevlog.usage(jevlog.log_files()))
        return 0
    g = db.find_game(a.game)
    gid = g["id"] if g else a.game
    paths = jevlog.log_files(gid)
    if not paths:
        print(f"no Jev log for {gid} (only games of a Jev bot have one)")
        return 1
    for path in paths:
        rows = jevlog.read(path)
        print(f"== {path.name}: {len(rows)} calls")
        print(jevlog.moment(rows, parse_time(a.at) / 25) if a.at else jevlog.timeline(rows, every=a.all))
    print(jevlog.usage(paths))
    return 0


def cmd_champion(a, db) -> int:
    import datetime

    path = ROOT / "lab" / "champions.json"
    data = json.loads(path.read_text())
    if a.action == "set":
        if not a.bot or not a.why:
            print("lab champion set BOT --run N --why '...'")
            return 1
        _check_bots([a.bot])
        data["ladder"].append({"bot": a.bot, "since": datetime.date.today().isoformat(), "run": a.run, "why": a.why})
        path.write_text(json.dumps(data, indent=1) + "\n")
        print(f"champion: {a.bot}")
        return 0
    for i, c in enumerate(data["ladder"]):
        mark = "*" if i == len(data["ladder"]) - 1 else " "
        print(f"{mark} {c['since']}  {c['bot']:<18} run {c['run'] if c['run'] is not None else '-':<4} {c['why']}")
    return 0


def cmd_curves(a, db) -> int:
    runs = [int(r) for r in a.runs.split(",")] if a.runs else None
    print(report.curves(db, runs, _split(a.bots) if a.bots else None))
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
    from lab import FAMILY, MAIN_ROOT
    from lab.batch import slot_count, slots_in_use, waiting

    print(f"this session: {'family ' + FAMILY if FAMILY else 'the main checkout'} ({ROOT}); lab shared at {LAB_DIR}"
          + (f" (main checkout {MAIN_ROOT})" if MAIN_ROOT != ROOT else ""))
    from lab.batch import RESERVED_LABEL, all_slots, slot_limit

    held = [h for h in slots_in_use() if h.get("label") != RESERVED_LABEL]
    limited = f" (lab slots {slot_limit()}: {all_slots() - slot_count()} reserved)" if slot_limit() else ""
    print(f"game slots: {len(held)} of {slot_count()} in use" + limited + ("" if held else " (all free)"))
    for h in held:
        since = f"{(time.time() - h['since']) / 60:.0f} min" if h.get("since") else "?"
        print(f"  slot {h['slot']}: {h.get('family', '?')}, run {h.get('run')}, {h.get('label', '')[:50]} (pid {h.get('pid')}, {since})")
    queue = waiting()
    if queue:
        print(f"waiting for a slot, first come first served: {len(queue)}")
        for k, w in enumerate(queue, 1):
            since = f"{(time.time() - w['since']) / 60:.0f} min" if w.get("since") else "?"
            print(f"  {k}. {w.get('family', '?')}, run {w.get('run')}, {w.get('label', '')[:50]} (pid {w.get('pid')}, {since})")
    notes = db.q("SELECT * FROM notes ORDER BY id DESC LIMIT 5")
    if notes:
        print("recent notes (lab note; every session's):")
        for n in notes[::-1]:
            print(f"  {report._when(n['created'])} {n['author'] or '?'}: {(n['text'] or '')[:110]}")
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
    job_json = None
    if a.moment is not None:  # (a moment of a set: the probe's branch there, played by hand)
        from lab import moments

        try:
            job, control = moments.human_job(db, a.moment, a.name, stop_after=a.stop_after, stop_quiet=a.stop_quiet)
        except ValueError as exc:
            raise SystemExit(str(exc))
        job_json = json.dumps(job)
        pos = job["extra"]["branch_seat"]
        played = moments.existing(db, control, moments._versions(), moments._engine())
        print(f"moment {a.moment} of @{job['extra']['probe']}: you play seat {pos} of {report.short(job['branch']['of'])} "
              f"from {clock(job['extra']['stop_at_seconds'] - a.stop_after)} against {job['bots'][1 - pos]}, "
              f"for {a.stop_after:g} s" + (f" (or {a.stop_quiet:g} s quiet)" if a.stop_quiet else ""))
        cmd_opts = f"--stop-after {a.stop_after:g}" + (f" --stop-quiet {a.stop_quiet:g}" if a.stop_quiet else "")
        print("  the control there: " + ("played already" if played else
              f"not played yet (lab probe @{job['extra']['probe']} --moments {a.moment} --control-only {cmd_opts})"))
        print(f"  afterwards: lab probe @{job['extra']['probe']} --report {cmd_opts}")
    else:
        _check_bots([a.vs])
    run_id = db.start_run("human", _command(), vars(a), note=a.note or "", experiment=a.experiment or "")
    log = LAB_DIR / "human" / f"session-{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "human", "serve", "--vs", a.vs, "--map", a.map,
           "--seat", str(a.seat), "--minutes", str(a.minutes), "--autopilot", a.autopilot, "--name", a.name,
           "--run-id", str(run_id)]
    if a.seed is not None:
        cmd += ["--seed", str(a.seed)]
    if job_json:
        cmd += ["--job-json", job_json]
    elif a.from_game:
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


def cmd_external(a, db) -> int:
    from bots import external

    table = registry.load()
    bots = [b for b in external.registered(table) if not a.names or b.name in a.names]
    unknown = set(a.names or []) - {b.name for b in bots}
    if unknown:
        print(f"not contestants' bots: {', '.join(sorted(unknown))}")
        return 1
    seen = set()
    for b in bots:
        kw = b.params.get("kwargs") or {}
        where = (kw.get("repo"), kw.get("commit"))
        if a.action == "fetch" and where not in seen:
            seen.add(where)
            print(external.fetch(kw["repo"], kw.get("url") or "", kw["commit"]))
        elif a.action == "list":
            try:
                external.check_checkout(kw["repo"], kw["commit"])
                there = "ready"
            except external.ExternalBotError as exc:
                there = f"not ready: {exc}"
            print(f"{b.name:18} {kw.get('repo')} @ {kw.get('commit')}  {there}")
    return 0


def cmd_advise(a, db) -> int:
    from lab import advise

    if a.action == "serve":
        a.run_id = a.run_id or None
        return advise.serve(a)
    if a.action == "status":
        try:
            print(advise.SESSION_FILE.read_text())
        except OSError:
            print("no advise session")
            return 1
        print(advise.client(["status"]))
        return 0
    if a.action == "stop":
        print(advise.client(["quit"]))
        try:
            os.kill(json.loads(advise.SESSION_FILE.read_text())["pid"], 0)
        except (OSError, ValueError, KeyError):
            advise.SESSION_FILE.unlink(missing_ok=True)  # (a session that died without cleaning up)
        return 0
    if advise.SESSION_FILE.exists():
        print("an advise session is running ('lab advise stop' first, or 'lab adv status')")
        return 1
    if not a.bot:
        print("which bot? lab advise start --bot ext-ryan-claude --vs ai-normal")
        return 1
    _check_bots([a.bot, a.vs])
    run_id = db.start_run("advise", _command(), vars(a), note=a.note or "", experiment=a.experiment or "")
    log = LAB_DIR / "advise" / f"session-{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "advise", "serve", "--bot", a.bot, "--vs", a.vs,
           "--map", a.map, "--seat", str(a.seat), "--minutes", str(a.minutes), "--run-id", str(run_id)]
    if a.seed is not None:
        cmd += ["--seed", str(a.seed)]
    subprocess.Popen(cmd, stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True, cwd=ROOT)
    for _ in range(240):
        time.sleep(0.25)
        if advise.SESSION_FILE.exists():
            print(f"advise session started (run {run_id}): {a.bot} vs {a.vs} on {a.map}, seat {a.seat}")
            print("next: lab adv wait  (then: brief LAYER | answer JSON | skip | look | log | detach; docs/LAB.md)")
            return 0
    print(f"the session did not start; see {log}")
    return 1


def cmd_adv(a, db) -> int:
    from lab import advise

    print(advise.client(a.argv))
    return 0


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

    def playing(sp, reuse=True):
        if reuse:
            sp.add_argument("--no-reuse", action="store_true",
                            help="play every game, even those already played the same way (lab/reuse.py)")
        sp.add_argument("--map", default="all", help="a map, maps separated by commas, or all (rotating)")
        sp.add_argument("--minutes", type=float, default=20)
        sp.add_argument("--seed", type=int, default=1)
        sp.add_argument("--workers", type=int, default=None, help="games at once (default: the CPU quota)")
        sp.add_argument("--note", default="")
        sp.add_argument("--experiment", default="")
        sp.add_argument("--record", action="store_true", help="also write our agent's recordings (recordings/)")

    s = sub.add_parser("bots", help="the bot registry")
    s.add_argument("action", nargs="?", default="list", choices=["list", "add", "show", "freeze", "tag"])
    s.add_argument("--as", dest="as_name", help="freeze: the name of the frozen copy")
    s.add_argument("name", nargs="?")
    s.add_argument("--kind", default="ff", choices=registry.KINDS)
    s.add_argument("--knobs", help="ff: JSON knobs")
    s.add_argument("--factory", help="module: package.module:function")
    s.add_argument("--kwargs", help="module: JSON keyword arguments")
    s.add_argument("--type", default="normal", help="ai: normal/rush/turtle/naval/beginner/easy/medium")
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
    s.add_argument("--baselines", default="@ais", help="bots or @suites (lab/suites.json), separated by commas")
    s.add_argument("--games", type=int, default=6, help="per opponent (with --sprt: at most)")
    sprt_args(s)
    playing(s)

    s = sub.add_parser("compare", help="several candidates against the same baselines on the same seeds")
    s.add_argument("candidates", help="bots separated by commas; the first is the reference")
    s.add_argument("--baselines", default="@standard", help="bots or @suites (lab/suites.json), separated by commas")
    s.add_argument("--games", type=int, default=6, help="per baseline and candidate (with --sprt: at most)")
    s.add_argument("--h2h", action="store_true",
                   help="also --games of each candidate against the reference (with --sprt they count as pairs)")
    sprt_args(s)
    playing(s)

    s = sub.add_parser("exploit", help="search settings of our scripted bot that beat a target bot")
    s.add_argument("target")
    s.add_argument("--tries", type=int, default=16, help="settings sampled in stage 1")
    s.add_argument("--games", type=int, default=2, help="games per setting in stage 1")
    s.add_argument("--finalists", type=int, default=4)
    s.add_argument("--final-games", type=int, default=6, help="more games per finalist in stage 2")
    playing(s)

    s = sub.add_parser("coach", help="from a moment of a Jev bot's game: what each posture, forced, would have scored")
    s.add_argument("game")
    s.add_argument("--at", required=True)
    s.add_argument("--seat", type=int, default=0)
    s.add_argument("--spread", type=int, default=3, help="branch at this many moments 2 s apart (one branch replays one way)")
    s.add_argument("--stop-after", type=float, default=None, help="end each branch this many seconds later")
    s.add_argument("--margin", type=int, default=300, help="postures this close to the best count as right")
    s.add_argument("--label", action="store_true", help="save the moment as a jev eval case labelled by the outcome")
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

    s = sub.add_parser("moments", help="named sets of moments to probe from (find, add, show, list, drop)")
    s.add_argument("action", choices=["find", "add", "show", "list", "drop"])
    s.add_argument("name", nargs="?", help="the set (with or without @)")
    s.add_argument("--kind", default="wiped", choices=["wiped", "building-lost", "swing", "at"])
    s.add_argument("--runs", help="find in these runs (separated by commas; default: every game)")
    s.add_argument("--bot", help="the seat to branch: this bot's")
    s.add_argument("--vs", help="only games against this bot")
    s.add_argument("--map")
    s.add_argument("--lead", type=float, help="seconds before the event (wiped 120, building-lost 30, swing 15)")
    s.add_argument("--at", help="kind at: this game time; add: the moment's time")
    s.add_argument("--drop", type=int, default=5000, help="swing: the score fall that counts")
    s.add_argument("--window", type=float, default=60.0, help="swing: within this many seconds")
    s.add_argument("--after", help="only moments after this game time (e.g. 10:00)")
    s.add_argument("--before", help="only moments before this game time")
    s.add_argument("--limit", type=int)
    s.add_argument("--dry-run", action="store_true", help="find: list, don't save")
    s.add_argument("--game", help="add: the game")
    s.add_argument("--seat", type=int, help="add: the seat to branch (default 0)")
    s.add_argument("--why", help="add: what the moment is")
    s.add_argument("--ids", help="drop: only these moment ids (default: the whole set)")

    s = sub.add_parser("probe", help="branch every moment of a set with variants and the control, and compare")
    s.add_argument("set")
    s.add_argument("--variants", help="bots for the branched seat, separated by commas")
    s.add_argument("--stop-after", type=float, default=300.0, help="end each branch this many seconds later")
    s.add_argument("--stop-quiet", type=float, default=None,
                   help="also end a branch once nothing was killed or lost for this many seconds (decided)")
    s.add_argument("--spread", type=int, default=1, help="branch each moment at this many points 2 s apart")
    s.add_argument("--no-control", action="store_true")
    s.add_argument("--limit", type=int, help="only the first N moments of the set")
    s.add_argument("--moments", help="only these moments (ids from lab moments show, separated by commas)")
    s.add_argument("--halving", action="store_true",
                   help="successive halving: all variants on --start moments, the better half on twice as many, ...")
    s.add_argument("--start", type=int, default=3, help="--halving: moments in the first round")
    s.add_argument("--no-reuse", action="store_true", help="play branches again even if played the same way before")
    s.add_argument("--control-only", action="store_true", help="play only the controls (e.g. for moments played by hand)")
    s.add_argument("--report", action="store_true", help="print the report of what was played, play nothing")
    s.add_argument("--runs", help="--report: only these probe runs")
    playing(s, reuse=False)

    s = sub.add_parser("board", help="the board: every family's entries against the champion (show, play, run, history)")
    s.add_argument("action", nargs="?", default="show", choices=["show", "play", "run", "history"])
    s.add_argument("bot", nargs="?", help="history: one bot's")
    s.add_argument("--entries", help="play: these bots instead of this checkout's entries")
    s.add_argument("--workers", type=int, default=None)
    s.add_argument("--note", default="")
    s.add_argument("--experiment", default="board")
    s.add_argument("--record", action="store_true")

    s = sub.add_parser("look", help="the board at a moment of a recorded game")
    s.add_argument("game")
    s.add_argument("--at", required=True)
    s.add_argument("--seat", type=int)
    s.add_argument("--units", action="store_true")
    s.add_argument("--no-map", action="store_true")

    s = sub.add_parser("watch", help="a recorded game as a time-lapse (score and map every N seconds)")
    s.add_argument("game")
    s.add_argument("--every", default="60", help="seconds between frames (or 1:00)")
    s.add_argument("--from", dest="start", help="game time to start at (e.g. 5:00)")
    s.add_argument("--to", dest="end", help="game time to stop at")
    s.add_argument("--seat", type=int, help="whose map (default: the first agent seat)")
    s.add_argument("--zoom", help="X,Y[,R]: a full-size window around a cell instead of the whole map")
    s.add_argument("--no-map", action="store_true", help="only the score lines")

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
    s.add_argument("--stale", action="store_true", help="runs still marked running whose process is gone")
    s.add_argument("--close-stale", action="store_true", help="mark those stopped (their games stay)")
    s = sub.add_parser("slots", help="every session's game slots: show, limit (lab slots N), lift (lab slots off)")
    s.add_argument("limit", nargs="?", help="N, or off")
    s.add_argument("--reserve", action="store_true", help=argparse.SUPPRESS)

    s = sub.add_parser("superweapons", help="games an OpenRA AI's superweapon may have decided (flag, list)")
    s.add_argument("--flag", action="store_true", help="flag the games not checked yet")
    s.add_argument("--bot", help="only hits on this bot's seat")
    s.add_argument("--decided", action="store_true", help="only games it likely decided")
    s.add_argument("--limit", type=int)

    s = sub.add_parser("note", help="write down an observation")
    s.add_argument("text", nargs="+")
    s.add_argument("--game")
    s.add_argument("--run", type=int)
    s.add_argument("--experiment")
    s.add_argument("--author", help="who writes (default: the session: its family, or main)")

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
    s.add_argument("--minutes", type=float, default=20)
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--autopilot", default="macro", choices=["off", "macro", "full"])
    s.add_argument("--name", default="claude")
    s.add_argument("--from", dest="from_game", help="start from a moment of this game (with --at)")
    s.add_argument("--at", default="0:00")
    s.add_argument("--from-game", dest="from_game")
    s.add_argument("--at-tick", type=int)
    s.add_argument("--run-id", type=int)
    s.add_argument("--moment", type=int, help="play this moment of a set by hand (lab moments show), like a probe branch")
    s.add_argument("--stop-after", type=float, default=300.0, help="--moment: the probe's stop rule")
    s.add_argument("--stop-quiet", type=float, default=None, help="--moment: the probe's stop rule")
    s.add_argument("--job-json", help=argparse.SUPPRESS)
    s.add_argument("--note", default="")
    s.add_argument("--experiment", default="")

    sub.add_parser("import", help="import the games our runtime played on the platform (recordings/)")
    sub.add_parser("status", help="the database, engine lanes and any human session")

    s = sub.add_parser("jev", help="what a Jev bot thought in a game (no game: calls, cost and latency so far); "
                                   "'jev eval': score Jev on labelled cases; 'jev case GAME': add one")
    s.add_argument("game", nargs="?")
    s.add_argument("rest", nargs="*")
    s.add_argument("--questions", help="eval: a variant (JSON: playbook / criteria / instructions)")
    s.add_argument("--council", action="store_true", help="eval: ask the cases as bots/jev_council.py's loops")
    s.add_argument("--expect", help="case: question=choice,choice;question=choice")
    s.add_argument("--why", help="case: the reason")
    s.add_argument("--seat", type=int, help="case: which seat's log (when both seats were Jev bots)")
    s.add_argument("--at", help="the full state and answers of the call nearest this game time")
    s.add_argument("--all", action="store_true", help="every call, not only changes of decision")
    s.add_argument("--attacks", action="store_true", help="every logged attack by opponent: did it gain?")
    s = sub.add_parser("champion", help="the champion over time (lab/champions.json); 'set' a new one")
    s.add_argument("action", nargs="?", default="show", choices=["show", "set"])
    s.add_argument("bot", nargs="?")
    s.add_argument("--run", type=int)
    s.add_argument("--why")
    s = sub.add_parser("curves", help="each bot's army, harvesters and assets at 3, 6, 9, 14 and 19 minutes")
    s.add_argument("--runs", help="runs separated by commas (default: every game)")
    s.add_argument("--bots", help="bots separated by commas")
    s = sub.add_parser("dashboard", help="the database as an HTML page (sim/lab/dashboard.html)")
    s.add_argument("--out")
    s.add_argument("--games", type=int, default=200, help="how many recent games it includes")

    s = sub.add_parser("forget", help="remove a run's games (or one game) from the database")
    s.add_argument("--run", type=int)
    s.add_argument("--game")
    s.add_argument("--yes", action="store_true")

    s = sub.add_parser("external", help="contestants' bots: fetch their pinned code / list them")
    s.add_argument("action", choices=["fetch", "list"])
    s.add_argument("names", nargs="*", help="bots (default: every contestant's bot)")
    s = sub.add_parser("advise", help="a game with Claude as a bot's advisor: start / status / stop")
    s.add_argument("action", choices=["start", "status", "stop", "serve"])
    s.add_argument("--bot", help="the advised bot (e.g. ext-ryan-claude)")
    s.add_argument("--vs", default="ai-normal")
    s.add_argument("--map", default="trapped")
    s.add_argument("--seat", type=int, default=0)
    s.add_argument("--minutes", type=float, default=20)
    s.add_argument("--seed", type=int, default=None)
    s.add_argument("--run-id", type=int)
    s.add_argument("--note", default="")
    s.add_argument("--experiment", default="")
    s = sub.add_parser("adv", help="a command for the advisor desk (see lab/advise.py)")
    s.add_argument("argv", nargs=argparse.REMAINDER)

    s = sub.add_parser("h", help="a command for the human seat (see lab/human.py)")
    s.add_argument("argv", nargs=argparse.REMAINDER)

    a = p.parse_args()
    db = LabDB()
    handlers = {"bots": cmd_bots, "play": cmd_play, "league": cmd_league, "gate": cmd_gate, "compare": cmd_compare, "exploit": cmd_exploit, "coach": cmd_coach, "branch": cmd_branch, "moments": cmd_moments, "probe": cmd_probe, "board": cmd_board,
                "look": cmd_look, "watch": cmd_watch, "replay": cmd_replay, "games": cmd_games, "show": cmd_show, "stats": cmd_stats,
                "h2h": cmd_h2h, "ratings": cmd_ratings, "report": cmd_report, "branches": cmd_branches,
                "runs": cmd_runs, "slots": cmd_slots, "note": cmd_note, "superweapons": cmd_superweapons, "import": cmd_import, "status": cmd_status, "forget": cmd_forget, "dashboard": cmd_dashboard, "curves": cmd_curves, "champion": cmd_champion, "jev": cmd_jev, "experiment": cmd_experiment, "sql": cmd_sql, "human": cmd_human,
                "h": cmd_h, "external": cmd_external, "advise": cmd_advise, "adv": cmd_adv}
    return handlers[a.command](a, db)


if __name__ == "__main__":
    raise SystemExit(main())
