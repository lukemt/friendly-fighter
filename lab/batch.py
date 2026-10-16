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

from . import FAMILY, LAB_DIR, MAIN_ROOT

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


def all_slots() -> int:
    """The machine's game slots: ``FF_LAB_SLOTS``, default the CPU quota."""
    return max(1, int(os.environ.get("FF_LAB_SLOTS") or cpu_quota()))


# A limit for every session at once (``lab slots N``, 2026-10-16: a game is an engine and a bot process,
# so 5 slots kept ~10 processes busy on 5.5 CPUs, load 12-20, next to the live runtime and other games'
# simulators). Read at every slot taken, so it applies to running runs too; slots at or above it are held
# by a reserver process (``reserve_step``), which also holds them against sessions on older code.
LIMIT_FILE_NAME = "limit"
RESERVER_FILE_NAME = "reserver.pid"
RESERVED_LABEL = "reserved (lab slots)"


def slot_limit() -> int | None:
    try:
        return max(1, int((SLOTS_DIR / LIMIT_FILE_NAME).read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def slot_count() -> int:
    """The slots games may take now: the machine's, or fewer under ``lab slots N``."""
    limit = slot_limit()
    return min(all_slots(), limit) if limit else all_slots()


def set_slot_limit(limit: int | None) -> None:
    SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SLOTS_DIR / LIMIT_FILE_NAME
    if limit is None or limit >= all_slots():
        path.unlink(missing_ok=True)
    else:
        path.write_text(f"{max(1, int(limit))}\n")


RESERVER_TICKET = "0" * 20  # (sorts before every waiter's ticket: first in the queue)


def _reserved_wanted() -> int:
    limit = slot_limit()
    return max(0, all_slots() - limit) if limit else 0


def reserve_step(held: dict[int, int], ticket: dict | None = None) -> bool:
    """One round of the reserver: hold ``all_slots() - limit`` slots (any of them) so that games get only
    the limit, whatever code a session runs. Short of them, it waits first in the queue (``ticket``: its
    place, kept between rounds), so a worker that finishes a game doesn't take the slot back (sessions
    on code from before the limit poll the slots too). ``held``: slot -> fd. False when there is nothing
    left to do (no limit, nothing held)."""
    ticket = ticket if ticket is not None else {}
    want = _reserved_wanted()
    for k in sorted(held, reverse=True)[:max(0, len(held) - want)]:
        os.close(held.pop(k))
    if len(held) < want and "fd" not in ticket:
        q = _queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        name = f"{RESERVER_TICKET}-{os.getpid()}-reserver"
        fd = os.open(q / name, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "family": "main", "run": None, "label": RESERVED_LABEL,
                                 "since": time.time()}).encode())
        ticket.update(fd=fd, name=name)
    for k in range(all_slots()):
        if len(held) >= want:
            break
        if k in held:
            continue
        fd = os.open(SLOTS_DIR / f"slot-{k}", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)  # (a game still plays there)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "family": "main", "run": None, "label": RESERVED_LABEL,
                                 "since": time.time()}).encode())
        held[k] = fd
    if len(held) >= want and "fd" in ticket:
        try:
            os.unlink(_queue_dir() / ticket["name"])
        except FileNotFoundError:
            pass
        os.close(ticket.pop("fd"))
        ticket.pop("name", None)
    return bool(want or held)


def reserver_pid() -> int | None:
    try:
        pid = int((SLOTS_DIR / RESERVER_FILE_NAME).read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def reserve_forever(poll: float = 2.0) -> None:
    """The reserver (``lab slots --reserve``, started by ``lab slots N``): until the limit is lifted."""
    (SLOTS_DIR / RESERVER_FILE_NAME).write_text(str(os.getpid()))
    held: dict[int, int] = {}
    ticket: dict = {}
    try:
        while reserve_step(held, ticket):
            time.sleep(0.2 if len(held) < _reserved_wanted() else poll)  # (short: as fast as any waiter)
    finally:
        for fd in held.values():
            os.close(fd)
        if "fd" in ticket:
            try:
                os.unlink(_queue_dir() / ticket["name"])
            except FileNotFoundError:
                pass
            os.close(ticket["fd"])
        try:
            if int((SLOTS_DIR / RESERVER_FILE_NAME).read_text()) == os.getpid():
                (SLOTS_DIR / RESERVER_FILE_NAME).unlink()
        except (OSError, ValueError):
            pass


class GameSlot:
    """One of the machine's game slots, held for one game: a lock on ``sim/lab/slots/slot-<k>``
    (freed when the game ends, or by the kernel when its process dies). The file says who holds it."""

    def __init__(self, run_id: int | None = None, label: str = "", poll: float = 1.0, log=print, on_wait=None) -> None:
        """``on_wait``: called once if all slots are taken, before waiting (a worker lets go of its engine)."""
        self.run_id, self.label, self.poll, self.log, self.on_wait = run_id, label, poll, log, on_wait
        self.fd: int | None = None

    def _me(self) -> bytes:
        return json.dumps({"pid": os.getpid(), "family": FAMILY or "main", "run": self.run_id, "label": self.label,
                           "since": time.time()}).encode()

    def _take(self) -> bool:
        if slot_limit() and sum(1 for h in slots_in_use() if h.get("label") != RESERVED_LABEL) >= slot_count():
            return False  # (as many games as the limit already play)
        for k in range(all_slots()):
            fd = os.open(SLOTS_DIR / f"slot-{k}", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            os.ftruncate(fd, 0)
            os.write(fd, self._me())
            self.fd = fd
            return True
        return False

    def __enter__(self) -> "GameSlot":
        """First come, first served: with nobody waiting, a free slot is taken at once; otherwise the
        worker queues (``waiting()``) and takes a slot when its turn comes. (Before 2026-10-16 every
        waiter polled the slots, and a worker that had just finished a game took its slot back for the
        next within milliseconds: runs holding slots kept them, and others waited for hours.)"""
        SLOTS_DIR.mkdir(parents=True, exist_ok=True)
        if not _live_tickets() and self._take():
            return self
        ticket = _Ticket(self._me())
        try:
            waited = False
            while True:
                if ticket.first() and self._take():
                    return self
                if not waited:
                    ahead = ticket.ahead()
                    self.log(f"[lab] all {slot_count()} game slots are taken (lab status): waiting"
                             + (f", {ahead} ahead in the queue" if ahead else ""), flush=True)
                    waited = True
                    if self.on_wait is not None:
                        self.on_wait()
                time.sleep(min(self.poll, 0.25))
        finally:
            ticket.close()

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


QUEUE_DIR_NAME = "queue"


def _queue_dir() -> Path:
    return SLOTS_DIR / QUEUE_DIR_NAME


class _Ticket:
    """A place in the queue for a slot: a file in ``sim/lab/slots/queue/`` named by when it was taken,
    locked by its owner while it waits (a dead owner's ticket is unlocked, and dropped by whoever
    sees it)."""

    def __init__(self, info: bytes) -> None:
        q = _queue_dir()
        q.mkdir(parents=True, exist_ok=True)
        tmp = q / f".new-{os.getpid()}-{id(self)}"
        self.fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        os.write(self.fd, info)
        self.name = f"{time.time_ns():020d}-{os.getpid()}-{id(self)}"
        os.rename(tmp, q / self.name)  # (locked before it is visible: never taken for a dead one's)

    def ahead(self) -> int:
        return sum(1 for name in _live_tickets() if name < self.name)

    def first(self) -> bool:
        live = _live_tickets()
        return not live or live[0] == self.name

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.unlink(_queue_dir() / self.name)
            except FileNotFoundError:
                pass
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _live_tickets() -> list[str]:
    """The queue's tickets in order; a dead owner's are removed."""
    q = _queue_dir()
    try:
        names = sorted(n for n in os.listdir(q) if not n.startswith("."))
    except FileNotFoundError:
        return []
    live = []
    for name in names:
        try:
            fd = os.open(q / name, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            live.append(name)  # (its owner holds it: waiting)
        else:
            try:
                os.unlink(q / name)  # (nobody holds it: its owner died)
            except FileNotFoundError:
                pass
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return live


def waiting() -> list[dict]:
    """Who waits for a slot, first first (for lab status)."""
    out = []
    for name in _live_tickets():
        try:
            out.append(json.loads((_queue_dir() / name).read_text() or "{}"))
        except (OSError, ValueError):
            out.append({})
    return out


def slots_in_use() -> list[dict]:
    """Who holds which slot now (for lab status), the reserver's included (``label`` RESERVED_LABEL)."""
    out = []
    for k in range(all_slots()):
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
    # Every Jev client's call log into the shared lab (frozen copies otherwise write next to their own
    # checkout: a family's worktree; lab jev and the paid budget, jevlog.spent, read the shared one).
    os.environ.setdefault("FF_JEV_LOG", str(LAB_DIR / "jev"))
    os.environ.pop("FF_THINK_MS", None)
    os.environ.pop("FF_KNOBS", None)
    if not os.environ.get("FF_SIM_OPENAI"):
        os.environ["OPENAI_API_KEY"] = ""  # no LLM commander in lab games unless FF_SIM_OPENAI=1
    if not paid_jev_allowed():
        # Paid Jev (TypeSafe's API, real credits) only for a run that asks for it: every Jev client,
        # frozen copies too, reads the key from the environment first. "off" is no key (a frozen client
        # sends it and is refused, for free). A local server (JEV_URL) needs no key and isn't affected.
        os.environ["TYPESAFE_API_KEY"] = "off"
    elif not os.environ.get("TYPESAFE_API_KEY"):
        # A run that asked for paid Jev: the key for every client. Frozen copies of older clients look only
        # in the environment and their own checkout's .env, and a family's worktree has no key there: they
        # played on with no answers and no error (the jev family, lab note 96). The main checkout's .env has it.
        key = _env_file_setting(MAIN_ROOT / ".env", "TYPESAFE_API_KEY")
        if key:
            os.environ["TYPESAFE_API_KEY"] = key


def _env_file_setting(path: Path, name: str) -> str | None:
    """A line ``NAME=value`` in a ``.env`` file (None when there is none)."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


PAID_JEV = "FF_LAB_PAID_JEV"


def paid_jev_allowed() -> int:
    """How many games a lab run may play on paid Jev: ``FF_LAB_PAID_JEV=N`` on the command (default 0)."""
    try:
        return max(0, int(os.environ.get(PAID_JEV) or 0))
    except ValueError:
        return 0


# The user's budget for paid Jev: $1 an hour, all lab sessions together (2026-10-16). A run is refused
# when its estimate and the last hour's spend (lab/jevlog.py: the tokens in call logs written within
# the hour) go over it. The estimate is per seat-minute: run 151 measured $0.0023 a 10-minute seat
# (52 calls, 53,855 input tokens); rounded up, about $0.006 a 20-minute seat.
JEV_BUDGET = "FF_LAB_JEV_BUDGET"
JEV_USD_PER_SEAT_MINUTE = 0.0003


def jev_budget_per_hour() -> float:
    try:
        return max(0.0, float(os.environ.get(JEV_BUDGET) or 1.0))
    except ValueError:
        return 1.0


def jev_estimate(jobs: list[dict], uses_jev) -> tuple[int, float]:
    """Paid Jev seats in ``jobs`` and what they would cost at most (dollars)."""
    seats = [(n, j.get("minutes", 20)) for j in jobs for n in j["bots"] if uses_jev(n)]
    return len(seats), sum(m for _, m in seats) * JEV_USD_PER_SEAT_MINUTE


NICE = 10  # lab games yield the CPU to anything else (the live tournament runtime above all)


PROFILE = "FF_LAB_PROFILE"


def _profiled(play, job, run_id, worker: int):
    """``FF_LAB_PROFILE=1 lab ...``: each game's Python time (the worker: the platform and both bots;
    not the engine, a process of its own) goes to ``sim/lab/logs/run-<id>-w<n>-<k>.prof`` (read with
    ``python -m pstats``)."""
    if os.environ.get(PROFILE) != "1":
        return play(job)
    import cProfile

    prof = cProfile.Profile()
    try:
        return prof.runcall(play, job)
    finally:
        _profiled.count = getattr(_profiled, "count", 0) + 1
        prof.dump_stats(str(LOG_DIR / f"run-{run_id}-w{worker}-{_profiled.count}.prof"))


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
                # (waiting for a slot, a worker lets go of its engine: another run's worker can take it over,
                # so the machine holds about one engine per slot, not one per worker)
                with GameSlot(job.get("run_id"), job.get("label", ""), on_wait=arena.release_engines):
                    t0 = time.monotonic()
                    game = _profiled(arena.play, GameJob(**job), job.get("run_id"), n)
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


class Feed:
    """Hands ``jobs`` to the workers' queue in order, ``count`` at a time, skipping those ``keep(index)``
    turns down by then; after the last, one ``None`` a worker (stop)."""

    def __init__(self, jobs: list[dict], keep, put, workers: int, run_id: int) -> None:
        self.jobs, self.keep, self.put, self.workers, self.run_id = jobs, keep, put, workers, run_id
        self.pending = list(range(len(jobs)))
        self.closed = False
        self.skipped = 0

    def __call__(self, count: int) -> None:
        while count > 0 and self.pending:
            i = self.pending.pop(0)
            if self.keep is None or self.keep(i):
                self.put({**self.jobs[i], "run_id": self.run_id})
                count -= 1
            else:
                self.skipped += 1
        if not self.pending and not self.closed:
            self.closed = True
            for _ in range(self.workers):
                self.put(None)


def run(jobs: list[dict], *, workers: int, run_id: int, record: bool = False, db_path: str | None = None,
        on_result=None, keep=None) -> list[dict]:
    """Play ``jobs`` (``GameJob`` dicts) on ``workers`` processes; returns the results in the order they end.

    ``keep(index)``: the jobs are handed out one at a time, in order, as workers free up, and a job
    is played only if ``keep`` still says so then (a sequential test stops what is decided). Without
    it every job is queued at the start."""
    lab_env()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ctx = mp.get_context("spawn")
    job_q, res_q = ctx.Queue(), ctx.Queue()
    workers = max(1, min(workers, len(jobs)))
    feed = Feed(jobs, keep, job_q.put, workers, run_id)
    feed(len(jobs) if keep is None else workers)
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
            if keep is not None:
                feed(1)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
        raise
    finally:
        for p in procs:
            p.join(30)
    return out


STALE_HOURS = 2.0


def stale_runs(db, now: float | None = None) -> list[dict]:
    """Runs still marked running whose process is gone: its pid (recorded since 2026-10-16) is dead, or,
    for older runs, it holds no game slot, waits for none, and has had no game for STALE_HOURS."""
    now = now if now is not None else time.time()
    live = {h.get("run") for h in slots_in_use()} | {w.get("run") for w in waiting()}
    out = []
    for r in db.q("SELECT * FROM runs WHERE status = 'running' ORDER BY id"):
        if r["id"] in live:
            continue
        pid = (json.loads(r["params"] or "{}") or {}).get("pid")
        if pid:
            try:
                os.kill(int(pid), 0)
                continue  # (alive)
            except ProcessLookupError:
                pass
            except (PermissionError, ValueError):
                continue
        last = db.one("SELECT MAX(COALESCE(finished, created)) AS t FROM games WHERE run_id = ?", r["id"])["t"]
        if not pid and now - max(last or 0, r["started"] or 0) < STALE_HOURS * 3600:
            continue
        out.append({**dict(r), "last_game": last})
    return out
