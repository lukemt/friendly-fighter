"""Many lab games at once: worker processes, each playing its games one after the other on an
engine lane of its own (``ffsim.engine_openra.HostPool``: the lanes stay loaded between runs).

A game's bots run inside its worker (``lab.arena``): one process per game at a time carries the
platform and both agents, as the platform server and the runtimes' match processes would. Four
workers keep this machine's four CPUs busy (docs/SPEED_LOOP.md). Every worker writes its games to
the database itself; its output (the bots' log lines) goes to ``sim/lab/logs/run-<id>-w<n>.log``.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
import traceback
from pathlib import Path

from . import LAB_DIR

LOG_DIR = LAB_DIR / "logs"


def lab_env() -> None:
    """The environment of lab processes: reproducible, quiet, and apart from the live runtime."""
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ.setdefault("FF_REMOTE_DIR", str(LAB_DIR / "remote"))
    os.environ.setdefault("FF_RECORD", "0")
    os.environ.pop("FF_THINK_MS", None)
    os.environ.pop("FF_KNOBS", None)
    if not os.environ.get("FF_SIM_OPENAI"):
        os.environ["OPENAI_API_KEY"] = ""  # no LLM commander in lab games unless FF_SIM_OPENAI=1


NICE = 10  # lab games yield the CPU to anything else (the live tournament runtime above all)


def _worker(n: int, jobs, results, log_path: str, record: bool, db_path: str | None) -> None:
    log = open(log_path, "a", buffering=1)
    sys.stdout = sys.stderr = log
    try:
        os.nice(NICE)
    except OSError:
        pass
    from .arena import Arena, GameJob
    from .db import LabDB

    arena = Arena(db=LabDB(db_path), record=record)
    try:
        while True:
            job = jobs.get()
            if job is None:
                break
            t0 = time.monotonic()
            try:
                game = arena.play(GameJob(**job))
                results.put({"ok": True, "job": job, "game": _brief(game), "wall": time.monotonic() - t0, "worker": n})
            except Exception as exc:  # noqa: BLE001
                print(traceback.format_exc(), flush=True)
                results.put({"ok": False, "job": job, "error": f"{type(exc).__name__}: {exc}", "worker": n})
    finally:
        arena.close()
        results.put({"worker_done": n})


def _brief(game: dict) -> dict:
    return {"id": game["id"], "map": game["map"], "termination": game["termination"], "winner": game["winner"],
            "outcome": game["outcome"], "game_seconds": game["game_seconds"], "wall_seconds": game["wall_seconds"],
            "seats": [{k: s[k] for k in ("bot", "faction", "result", "net", "kills_cost", "deaths_cost")}
                      for s in game["seats"]]}


def run(jobs: list[dict], *, workers: int, run_id: int, record: bool = False, db_path: str | None = None,
        on_result=None) -> list[dict]:
    """Play ``jobs`` (``GameJob`` dicts) on ``workers`` processes; returns the results in the order they end."""
    lab_env()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ctx = mp.get_context("spawn")
    job_q, res_q = ctx.Queue(), ctx.Queue()
    for job in jobs:
        job_q.put({**job, "run_id": run_id})
    workers = max(1, min(workers, len(jobs)))
    for _ in range(workers):
        job_q.put(None)
    procs = [ctx.Process(target=_worker, args=(i, job_q, res_q, str(LOG_DIR / f"run-{run_id}-w{i}.log"), record,
                                                db_path), name=f"lab-worker-{i}", daemon=False)
             for i in range(workers)]
    for p in procs:
        p.start()
    out: list[dict] = []
    done = 0
    try:
        while done < workers:
            try:
                msg = res_q.get(timeout=5.0)
            except Exception:  # noqa: BLE001  (queue.Empty)
                if not any(p.is_alive() for p in procs):
                    break
                continue
            if "worker_done" in msg:
                done += 1
                continue
            out.append(msg)
            if on_result is not None:
                on_result(msg, len(out), len(jobs))
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
        raise
    finally:
        for p in procs:
            p.join(30)
    return out
