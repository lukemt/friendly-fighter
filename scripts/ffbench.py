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
    return {"steps": n, "ticks": steps[n - 1][0].until_tick if n else 0, "create_s": t_created - t0, "wall_s": wall,
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
                    print(f"[{names[i][-30:]}] {t.name[:24]:24} {r['ticks']:6d} ticks  wall {r['wall_s']:6.2f}s  "
                          f"engine {r['engine_s']:6.2f}s  cpu {r['cpu_s']:6.2f}s  "
                          f"({r['ticks'] / max(r['wall_s'], 1e-9):5.0f} ticks/s){diff}", flush=True)
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
    a = p.parse_args()
    return cmd_replay(a)


if __name__ == "__main__":
    raise SystemExit(main())
