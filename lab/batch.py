"""Many lab games at once: worker processes, each playing its games one after the other on an
engine lane of its own (``ffsim.engine_openra.HostPool``: the lanes stay loaded between runs).

A game's bots run inside its worker (``lab.arena``): one process per game at a time carries the
platform and both agents, as the platform server and the runtimes' match processes would. Four
workers keep this machine's four CPUs busy (docs/SPEED_LOOP.md). Every worker writes its games to
the database itself; its output (the bots' log lines) goes to ``sim/lab/logs/run-<id>-w<n>.log``.

**Game slots.** Several sessions share the machine (AGENTS.md): before each game a worker takes one
of the machine's game slots (``GameSlot``: a lock on a file in ``sim/lab/slots/``), so all sessions'
games together never outnumber the CPUs (``FF_LAB_SLOTS``, default the CPU quota). A worker whose
run is stopped dies with it (no orphan keeps playing, or keeps a slot).
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import multiprocessing as mp
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from . import FAMILY, LAB_DIR

LOG_DIR = LAB_DIR / "logs"
SLOTS_DIR = LAB_DIR / "slots"


def cpu_quota() -> int:
    """This container's CPUs (its cgroup quota), else the machine's."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    return os.cpu_count() or 1


def slot_count() -> int:
    return max(1, int(os.environ.get("FF_LAB_SLOTS") or cpu_quota()))


class GameSlot:
    """One of the machine's game slots, held for one game: a lock on ``sim/lab/slots/slot-<k>``
    (freed when the game ends, or by the kernel when its process dies). The file says who holds it."""

    def __init__(self, run_id: int | None = None, label: str = "", poll: float = 1.0, log=print) -> None:
        self.run_id, self.label, self.poll, self.log = run_id, label, poll, log
        self.fd: int | None = None

    def __enter__(self) -> "GameSlot":
        SLOTS_DIR.mkdir(parents=True, exist_ok=True)
        waited = False
        while True:
            for k in range(slot_count()):
                fd = os.open(SLOTS_DIR / f"slot-{k}", os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps({"pid": os.getpid(), "family": FAMILY or "main", "run": self.run_id,
                                         "label": self.label, "since": time.time()}).encode())
                self.fd = fd
                return self
            if not waited:
                self.log(f"[lab] all {slot_count()} game slots are taken (lab status): waiting", flush=True)
                waited = True
            time.sleep(self.poll)

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def slots_in_use() -> list[dict]:
    """Who holds which slot now (for lab status)."""
    out = []
    for k in range(slot_count()):
        path = SLOTS_DIR / f"slot-{k}"
        if not path.exists():
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            try:
                out.append({"slot": k, **json.loads(path.read_text() or "{}")})
            except ValueError:
                out.append({"slot": k})
        finally:
            os.close(fd)
    return out


def _die_with_parent() -> None:
    """A worker gets SIGKILL when the run that started it dies (a stopped run leaves no orphans)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass
    if os.getppid() == 1:  # (the run died before the line above)
        os._exit(1)


def lab_env() -> None:
    """The environment of lab processes: reproducible, quiet, and apart from the live runtime."""
    os.environ["PYTHONHASHSEED"] = "0"
    os.environ.setdefault("FF_REMOTE_DIR", str(LAB_DIR / "remote"))
    os.environ.setdefault("FF_RECORD", "0")
    os.environ.pop("FF_THINK_MS", None)
    os.environ.pop("FF_KNOBS", None)
    if not os.environ.get("FF_SIM_OPENAI"):
        os.environ["OPENAI_API_KEY"] = ""  # no LLM commander in lab games unless FF_SIM_OPENAI=1
    if not paid_jev_allowed():
        # Paid Jev (TypeSafe's API, real credits) only for a run that asks for it: every Jev client,
        # frozen copies too, reads the key from the environment first. "off" is no key (a frozen client
        # sends it and is refused, for free). A local server (JEV_URL) needs no key and isn't affected.
        os.environ["TYPESAFE_API_KEY"] = "off"


PAID_JEV = "FF_LAB_PAID_JEV"


def paid_jev_allowed() -> int:
    """How many games a lab run may play on paid Jev: ``FF_LAB_PAID_JEV=N`` on the command (default 0)."""
    try:
        return max(0, int(os.environ.get(PAID_JEV) or 0))
    except ValueError:
        return 0


NICE = 10  # lab games yield the CPU to anything else (the live tournament runtime above all)


def _worker(n: int, jobs, results, log_path: str, record: bool, db_path: str | None) -> None:
    _die_with_parent()
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
            try:
                with GameSlot(job.get("run_id"), job.get("label", "")):
                    t0 = time.monotonic()
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
