"""Run Red Alert games locally against the replica platform (ffsim).

    python scripts/ffsim.py run                                # 1 game: our agent vs itself, trapped, 10 min
    python scripts/ffsim.py run --seat1 ai:normal --games 4    # vs OpenRA's own Normal AI
    python scripts/ffsim.py run --seat0 agent:A --seat1 agent:B --env B:FF_VARIANT=rush --games 6 --parallel 2
    python scripts/ffsim.py serve --port 8800                  # just the platform; create matches via POST /sim/matches

Seats: ``agent[:LABEL]`` is played by a runtime (``python -m agent --match``) started for
that label (one runtime per label; both seats under one label is self-play, like the
platform's Testing); ``ai:normal|rush|turtle|naval`` is an OpenRA bot. ``--agent LABEL:MODULE``
and ``--env LABEL:K=V`` configure a label's runtime. Maps: sidestep, trapped,
shadowfiend-ii or random. Results go to ``sim/results/<time>/`` (one JSON per game plus
summary.json); the runtimes also write their usual recordings under ``recordings/sim-*``.
Each local key is ``eak_live_`` + a hash of the label: it is only ever sent to this
local server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ffsim.engine_openra import OpenRAEngine  # noqa: E402
from ffsim.rules import Rules  # noqa: E402
from ffsim.server import Platform, serve  # noqa: E402

MAPS = ["sidestep", "trapped", "shadowfiend-ii"]


def local_key(label: str) -> str:
    return "eak_live_" + hashlib.sha256(f"ffsim:{label}".encode()).hexdigest()


def engine_factory(out: Path):
    def make(match, kinds, lobby):
        return OpenRAEngine(match.session_id, match.map_id, kinds, lobby=lobby, log_dir=out / "engine" / match.session_id)
    return make


def parse_seat(spec: str) -> dict:
    if spec.startswith("ai:"):
        return {"kind": spec}
    label = spec.split(":", 1)[1] if ":" in spec else "A"
    return {"kind": "agent", "label": label, "key": local_key(label)}


def start_runtime(label: str, url: str, out: Path, agent: str | None, env_extra: dict, probe_seat: int | None = None
                  ) -> subprocess.Popen:
    remote = out / f"remote-{label}"
    remote.mkdir(parents=True, exist_ok=True)
    if probe_seat is not None:
        (remote / "probe.json").write_text(json.dumps({"enabled": True, "seat": probe_seat, "resign_at_end": True}))
    env = dict(os.environ, ALTRUAGENT_CONTROL_URL=url, ALTRUAGENT_OFFICIAL_AGENT_KEY=local_key(label),
               FF_REMOTE_DIR=str(remote), FF_CONSOLE_BASE_PORT=str(8870 + 10 * (ord(label[0]) % 10)),
               PYTHONUNBUFFERED="1", **env_extra)
    if "OPENAI_API_KEY" not in env_extra and not os.environ.get("FF_SIM_OPENAI"):
        env["OPENAI_API_KEY"] = ""  # no LLM commander in local games unless FF_SIM_OPENAI=1 (dotenv won't override "")
    cmd = [sys.executable, "-u", "-m", "agent", "--match"] + (["--agent", agent] if agent else [])
    log = open(out / f"runtime-{label}.log", "ab")
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def summarize(results: list[dict]) -> dict:
    table: dict[str, dict] = {}
    for r in results:
        res = r.get("result") or {}
        returns = res.get("returns") or {}
        for s in r["seats"]:
            name = s.get("label") or s["kind"]
            row = table.setdefault(name, {"games": 0, "wins": 0, "draws": 0, "losses": 0, "no_result": 0, "points": 0.0})
            row["games"] += 1
            score = returns.get(s["agent_id"])
            if score is None:
                row["no_result"] += 1
            else:
                row["points"] += score
                row["wins" if score == 1.0 else "losses" if score == 0.0 else "draws"] += 1
    return table


def cmd_run(a) -> int:
    out = Path(a.out or ROOT / "sim" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    platform = Platform(engine_factory(out), results_dir=out, rtt_ms=a.rtt_ms)
    server = serve(platform, a.port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"[ffsim] platform at {url}; results in {out}")

    seats = [parse_seat(a.seat0), parse_seat(a.seat1)]
    agents = dict(x.split(":", 1) for x in a.agent)
    envs: dict[str, dict] = {}
    for x in a.env:
        label, kv = x.split(":", 1)
        k, v = kv.split("=", 1)
        envs.setdefault(label, {})[k] = v
    labels = sorted({s["label"] for s in seats if s["kind"] == "agent"})
    probe_label = seats[0].get("label") if a.probe else None
    runtimes = {lb: start_runtime(lb, url, out, agents.get(lb), envs.get(lb, {}), 0 if lb == probe_label else None)
                for lb in labels}

    done: list[dict] = []
    finished = threading.Event()

    def on_finish(info):
        for s, spec in zip(info["seats"], seats):
            s["label"] = spec.get("label") or spec["kind"]
        done.append(info)
        res = info.get("result") or {}
        sides = " vs ".join(f"{s['label']}({s['faction']})" for s in info["seats"])
        print(f"[ffsim] game {len(done)}/{a.games} {info['map']}: {sides} -> {res.get('winner_seat') or res.get('outcome')} "
              f"({info['termination_reason']}, {info['game_seconds']:.0f}s) tiebreak={[s['tiebreak'] for s in info['seats']]}",
              flush=True)
        if len(done) >= a.games:
            finished.set()

    platform.on_finish.append(on_finish)
    rng = random.Random(a.seed)
    started = 0

    def start_one():
        nonlocal started
        started += 1
        m = rng.choice(MAPS) if a.map == "random" else a.map
        order = seats if not (a.swap and started % 2 == 0) else list(reversed(seats))
        info = platform.create_match(map_id=m, max_minutes=a.minutes, seats=order, label=f"game{started}")
        print(f"[ffsim] game {started} {info['session_id']} on {m}: "
              + " vs ".join(s.get("label") or s["kind"] for s in order), flush=True)

    stop = {"flag": False}

    def handle(*_):
        stop["flag"] = True
        finished.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    try:
        for _ in range(min(a.parallel, a.games)):
            start_one()
        while not finished.is_set():
            finished.wait(1.0)
            with platform.lock:
                live = sum(1 for m in platform.matches.values() if m.phase != "finished")
            while not stop["flag"] and started < a.games and live < a.parallel:
                start_one()
                live += 1
            for lb, proc in runtimes.items():
                if proc.poll() is not None and not stop["flag"]:
                    print(f"[ffsim] runtime {lb} exited ({proc.returncode}); restarting", flush=True)
                    runtimes[lb] = start_runtime(lb, url, out, agents.get(lb), envs.get(lb, {}))
    finally:
        if not stop["flag"]:
            time.sleep(5)  # let the runtimes finish their last writes (recordings, the probe's last step)
        for proc in runtimes.values():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        with platform.lock:
            matches = list(platform.matches.values())
        for m in matches:
            if m.phase != "finished":
                m.fail("cancelled", "runner stopped")
            if m.engine is not None:
                m.engine.stop()
        server.shutdown()
    table = summarize(done)
    summary = {"games": len(done), "seats": [a.seat0, a.seat1], "map": a.map, "minutes": a.minutes, "table": table,
               "results": [{"session_id": d["session_id"], "map": d["map"], "result": d["result"],
                            "termination_reason": d["termination_reason"], "game_seconds": d["game_seconds"],
                            "seats": [{k: s.get(k) for k in ("seat", "label", "faction", "tiebreak", "agent_id")}
                                      for s in d["seats"]]} for d in done]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print("[ffsim] summary:")
    for name, row in table.items():
        print(f"  {name:12} {row}")
    return 0


def cmd_serve(a) -> int:
    out = Path(a.out or ROOT / "sim" / "results" / ("serve-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    platform = Platform(engine_factory(out), results_dir=out, rtt_ms=a.rtt_ms)
    server = serve(platform, a.port)
    print(f"[ffsim] platform at http://127.0.0.1:{server.server_address[1]} (maps {sorted(Rules.load().maps)})")
    print("  POST /sim/matches {\"map_id\": \"trapped\", \"max_minutes\": 10, \"seats\": [{\"kind\": \"agent\", \"key\": KEY}, "
          "{\"kind\": \"ai:normal\"}]}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="play games and report")
    r.add_argument("--seat0", default="agent:A")
    r.add_argument("--seat1", default="agent:A")
    r.add_argument("--map", default="trapped", choices=MAPS + ["random"])
    r.add_argument("--minutes", type=int, default=10)
    r.add_argument("--games", type=int, default=1)
    r.add_argument("--parallel", type=int, default=1)
    r.add_argument("--swap", action="store_true", help="swap seats every other game")
    r.add_argument("--agent", action="append", default=[], metavar="LABEL:MODULE")
    r.add_argument("--env", action="append", default=[], metavar="LABEL:K=V")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--probe", action="store_true", help="run agent/probe.py on seat 0 (it resigns when done)")
    r.add_argument("--rtt-ms", type=float, default=190.0,
                   help="simulated network round trip per MCP call (our real one to the platform: 190 ms)")
    r.add_argument("--port", type=int, default=0)
    r.add_argument("--out")
    s = sub.add_parser("serve", help="run the platform only")
    s.add_argument("--port", type=int, default=8800)
    s.add_argument("--rtt-ms", type=float, default=190.0)
    s.add_argument("--out")
    a = p.parse_args()
    return cmd_run(a) if a.command == "run" else cmd_serve(a)


if __name__ == "__main__":
    raise SystemExit(main())
