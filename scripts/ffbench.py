"""Time the engine alone, and check that an engine build plays a game exactly like another.

A virtual-time run with ``FF_ENGINE_TRACE=<dir>`` writes one trace per game: the session the
game created and every step it made (the ticks it asked for and the agents' actions), with a
digest of the views each step returned. Replaying a trace sends the same calls again, without
the platform or the agents, so only the engine runs:

    FF_ENGINE_TRACE=sim/traces/x python scripts/ffsim.py run --seat1 ai:normal --think 3 --seed 1
    python scripts/ffbench.py replay sim/traces/x/*.trace                       # the current build
    python scripts/ffbench.py replay sim/traces/x/*.trace --engine ~/openra-sim/fork-base --engine ~/openra-sim/fork

Each trace is played on each build in turn (A B A B ..., one engine process per build, as a lane
plays its games); the report gives each build's wall and CPU seconds and whether every step's
views matched the trace's. A build that changes nothing in the game logic matches every step.

A standard set of recorded games (wipe-outs, self-play of two variants, a rush, the Normal AI;
seeded, so the same games every time) for checking engine changes:

    python scripts/ffbench.py corpus            # record it with the current build (sim/traces/corpus)
    python scripts/ffbench.py check             # replay it on the current build: every step must match
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ffsim.engine_openra import ENGINE_DIR, EngineHost, views_digest  # noqa: E402
from ffsim.proto import rl_bridge_pb2 as pb  # noqa: E402


def read_trace(path: Path) -> tuple[dict, list[tuple[pb.StepRequest, int, str]]]:
    lines = path.read_text().splitlines()
    head = json.loads(lines[0])
    steps = []
    for line in lines[1:]:
        try:
            req, tick, digest = json.loads(line)
        except ValueError:
            break  # a trace cut short
        steps.append((pb.StepRequest.FromString(base64.b64decode(req)), tick, digest))
    return head, steps


def replay(host: EngineHost, head: dict, steps, *, check: bool = True, until: int | None = None, pace: float = 0.0
           ) -> dict:
    cpu0, t0 = host.cpu_seconds(), time.perf_counter()
    session = host.create(head["map_file"], head["bots"], head["seed"], head["lobby"])
    t_created = time.perf_counter()
    engine_us, first_diff, n = 0, None, 0
    for req, tick, digest in steps:
        if until is not None and req.until_tick > until:
            break
        req.session_id = session
        reply = host.step(req)
        if pace:
            time.sleep(pace)
        engine_us += reply.elapsed_us
        n += 1
        if check and first_diff is None and (reply.tick != tick or views_digest(reply.observations) != digest):
            first_diff = {"step": n, "tick": reply.tick, "want_tick": tick}
    wall = time.perf_counter() - t_created
    host.destroy(session)
    try:
        rss = int(next(x for x in Path(f"/proc/{host.proc.pid}/status").read_text().splitlines()
                       if x.startswith("VmRSS")).split()[1]) // 1024
    except (OSError, StopIteration, ValueError):
        rss = 0
    return {"steps": n, "ticks": steps[n - 1][0].until_tick if n else 0, "create_s": t_created - t0, "wall_s": wall,
            "rss_mb": rss,
            "engine_s": engine_us / 1e6, "cpu_s": host.cpu_seconds() - cpu0, "match": first_diff is None,
            "first_diff": first_diff}


def cmd_replay(a) -> int:
    traces = [Path(t) for t in a.traces]
    env = dict(kv.split("=", 1) for kv in a.env)
    specs = a.engine or [str(ENGINE_DIR)]
    engines, envs = [], []
    for spec in specs:  # DIR or DIR;K=V;K=V (a build with its own engine environment)
        path, *kvs = spec.split(";")
        engines.append(Path(path or str(ENGINE_DIR)).expanduser())
        envs.append({**env, **dict(kv.split("=", 1) for kv in kvs)})
    names = [s if len(set(specs)) == len(specs) else f"{i}:{s}" for i, s in enumerate(specs)]
    names = [n.replace(str(Path.home()), "~") for n in names]
    tmp = Path(tempfile.mkdtemp(prefix="ffbench-"))
    hosts = [EngineHost(tmp / f"engine-{i}", engine_dir=e, env=envs[i]) for i, e in enumerate(engines)]
    for h in hosts:
        h._ensure()
        if a.affinity:
            cpus = {int(c) for c in a.affinity.split(",")}
            for tid in os.listdir(f"/proc/{h.proc.pid}/task"):
                os.sched_setaffinity(int(tid), cpus)
    loaded = [(t, *read_trace(t)) for t in traces]
    rows: list[dict] = []
    try:
        for rep in range(a.repeat):
            for k, (t, head, steps) in enumerate(loaded):
                order = list(range(len(hosts)))
                if (rep + k) % 2:
                    order.reverse()
                for i in order:
                    r = replay(hosts[i], head, steps, check=not a.no_check, until=a.until, pace=a.pace / 1000)
                    r.update(engine=i, trace=t.name, rep=rep, first=(rep == 0 and k == 0))
                    rows.append(r)
                    diff = "" if r["match"] else f"  DIFFERS from step {r['first_diff']['step']} (tick {r['first_diff']['tick']})"
                    print(f"[{names[i][-30:]}] {t.name[:24]:24} {r['ticks']:6d} ticks  create {r['create_s']:4.2f}s  wall {r['wall_s']:6.2f}s  "
                          f"engine {r['engine_s']:6.2f}s  cpu {r['cpu_s']:6.2f}s  "
                          f"({r['ticks'] / max(r['wall_s'], 1e-9):5.0f} ticks/s, {r['rss_mb']} MB){diff}", flush=True)
    finally:
        for h in hosts:
            h.stop()
    print()
    for i, e in enumerate(engines):
        mine = [r for r in rows if r["engine"] == i]
        warm = [r for r in mine if not r["first"]] or mine
        tot = {k: sum(r[k] for r in warm) for k in ("wall_s", "engine_s", "cpu_s", "ticks")}
        print(f"{names[i][-40:]:40} games {len(mine):3d}  matched {sum(r['match'] for r in mine):3d}  "
              f"(without the first game) wall {tot['wall_s']:7.2f}s  engine {tot['engine_s']:7.2f}s  "
              f"cpu {tot['cpu_s']:7.2f}s  = {tot['cpu_s'] / max(tot['ticks'], 1) * 15000:6.2f} cpu-s per 10-min game")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))
    return 0 if all(r["match"] for r in rows) else 1


# The standard games: (label, ffsim run arguments). Seeded with a fixed think time: reproducible.
CORPUS = [
    ("eco-vs-rush", ["--seat0", "agent:E", "--seat1", "ai:rush", "--map", "sidestep", "--games", "4", "--seed", "5",
                     "--env", 'E:FF_KNOBS={"posture":"eco","attack_threshold":14,"unit_focus":"vehicles","target":"enemy_base"}']),
    ("self-play", ["--seat0", "agent:A", "--seat1", "agent:T", "--map", "shadowfiend-ii", "--games", "2", "--seed", "6",
                   "--env", 'T:FF_KNOBS={"posture":"defend","attack_threshold":20}']),
    ("rush-vs-normal", ["--seat0", "agent:R", "--seat1", "ai:normal", "--games", "2", "--seed", "7",
                        "--env", 'R:FF_KNOBS={"posture":"attack","attack_threshold":4,"unit_focus":"infantry","target":"nearest"}']),
    ("default-vs-normal", ["--seat1", "ai:normal", "--games", "3", "--seed", "11"]),
]


def cmd_corpus(a) -> int:
    import subprocess

    out = Path(a.dir)
    out.mkdir(parents=True, exist_ok=True)
    for label, args in CORPUS:
        print(f"[corpus] {label}", flush=True)
        env = dict(os.environ, FF_ENGINE_TRACE=str(out))
        subprocess.run([sys.executable, str(ROOT / "scripts" / "ffsim.py"), "run", "--think", "3", "--fresh-engines",
                        "--out", str(ROOT / "sim" / "results" / f"corpus-{label}")] + args, env=env, check=True)
    print(f"[corpus] {len(list(out.glob('*.trace')))} traces in {out}")
    return 0


def cmd_check(a) -> int:
    traces = sorted(Path(a.dir).glob("*.trace"))
    if not traces:
        print(f"no traces in {a.dir}: record them with 'ffbench.py corpus' first")
        return 2
    a.traces, a.engine, a.repeat, a.until, a.no_check, a.pace, a.affinity, a.json = (
        [str(t) for t in traces], a.engine, 1, None, False, 0.0, None, None)
    return cmd_replay(a)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("replay", help="replay engine traces on one or more builds")
    r.add_argument("traces", nargs="+")
    r.add_argument("--engine", action="append",
                   help="engine dir, or DIR;K=V;... with extra engine environment (default: the current build); "
                        "repeat to compare")
    r.add_argument("--env", action="append", default=[], metavar="K=V", help="extra engine environment")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--until", type=int, default=None, help="stop each game at this tick")
    r.add_argument("--no-check", action="store_true")
    r.add_argument("--pace", type=float, default=0.0, help="ms to wait after each step (as the agents make it wait)")
    r.add_argument("--affinity", help="pin the engine processes to these CPUs (comma-separated)")
    r.add_argument("--json")
    c = sub.add_parser("corpus", help="record the standard games' traces with the current build")
    c.add_argument("--dir", default=str(ROOT / "sim" / "traces" / "corpus"))
    k = sub.add_parser("check", help="replay the standard games on a build: every step must match")
    k.add_argument("--dir", default=str(ROOT / "sim" / "traces" / "corpus"))
    k.add_argument("--engine", action="append", help="engine dir (default: the current build)")
    k.add_argument("--env", action="append", default=[], metavar="K=V")
    a = p.parse_args()
    return {"replay": cmd_replay, "corpus": cmd_corpus, "check": cmd_check}[a.command](a)


if __name__ == "__main__":
    raise SystemExit(main())
