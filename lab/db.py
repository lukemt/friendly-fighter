"""The lab's game database (SQLite): everything every lab game did, for queries and reports.

Tables:

- ``runs``: one per command that played games (what was run, when, at which git commit, and any
  note or experiment it belongs to);
- ``bot_versions``: each version of a bot that played (its settings and a hash of its code);
- ``games``: one per game (map, seed, length, how it ended, its engine trace; for a branch, the game
  and tick it started from);
- ``seats``: who played (bot, version, faction) and how it went (result, kills/losses, assets...);
- ``timeline``: each agent seat's state every few game seconds (economy, army, score);
- ``events``: units and buildings made and lost, enemies spotted and killed, attacks;
- ``decisions``: every batch of orders, its reasoning and verdicts (one compressed blob per seat);
- ``notes``, ``experiments``: what we wrote down about games, runs and experiments;
- ``human_log``: every command given at a human seat;
- ``moments``: named sets of moments (a seat of a game at a time) to probe from (``lab/moments.py``).

Several processes write (``lab.batch`` workers): WAL mode, and each game is one transaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path

from . import LAB_DIR

DB_PATH = LAB_DIR / "lab.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started REAL, ended REAL, kind TEXT, command TEXT, git_commit TEXT, git_dirty INTEGER,
  params TEXT, note TEXT, experiment TEXT, games INTEGER DEFAULT 0, status TEXT
);
CREATE TABLE IF NOT EXISTS run_games (run_id INTEGER, game_id TEXT, PRIMARY KEY (run_id, game_id));
CREATE TABLE IF NOT EXISTS bot_versions (
  version TEXT PRIMARY KEY, bot TEXT, kind TEXT, spec TEXT, code_hash TEXT, git_commit TEXT, first_seen REAL
);
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY, run_id INTEGER, created REAL, finished REAL, map TEXT, minutes INTEGER, seed INTEGER,
  time_mode TEXT, game_seconds REAL, wall_seconds REAL, termination TEXT, winner INTEGER, outcome TEXT,
  branch_of TEXT, branch_tick INTEGER, branch_label TEXT, trace TEXT, engine TEXT, info TEXT
);
CREATE TABLE IF NOT EXISTS seats (
  game_id TEXT, position INTEGER, bot TEXT, version TEXT, kind TEXT, faction TEXT, result TEXT, points REAL,
  kills_cost INTEGER, deaths_cost INTEGER, assets_value INTEGER, net INTEGER, army_value INTEGER,
  units_killed INTEGER, units_lost INTEGER, buildings_killed INTEGER, buildings_lost INTEGER,
  batches INTEGER, orders INTEGER, rejected INTEGER, think TEXT,
  PRIMARY KEY (game_id, position)
);
CREATE TABLE IF NOT EXISTS timeline (
  game_id TEXT, position INTEGER, tick INTEGER, cash INTEGER, ore INTEGER, power_provided INTEGER,
  power_drained INTEGER, harvesters INTEGER, units INTEGER, combat_units INTEGER, buildings INTEGER,
  army_value INTEGER, kills_cost INTEGER, deaths_cost INTEGER, assets_value INTEGER, explored REAL,
  enemies_visible INTEGER, enemy_buildings_visible INTEGER,
  PRIMARY KEY (game_id, position, tick)
);
CREATE TABLE IF NOT EXISTS events (
  game_id TEXT, position INTEGER, tick INTEGER, kind TEXT, type TEXT, x INTEGER, y INTEGER, count INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
  game_id TEXT, position INTEGER, batches INTEGER, data BLOB, PRIMARY KEY (game_id, position)
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, game_id TEXT, run_id INTEGER, experiment TEXT,
  author TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS experiments (
  name TEXT PRIMARY KEY, created REAL, hypothesis TEXT, status TEXT, conclusion TEXT, closed REAL
);
CREATE TABLE IF NOT EXISTS human_log (
  game_id TEXT, wall REAL, tick INTEGER, command TEXT, answer TEXT
);
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, set_name TEXT, game_id TEXT, seat INTEGER, seconds REAL, kind TEXT,
  why TEXT, created REAL, UNIQUE (set_name, game_id, seat, seconds)
);
CREATE INDEX IF NOT EXISTS seats_bot ON seats (bot);
CREATE INDEX IF NOT EXISTS games_run ON games (run_id);
CREATE INDEX IF NOT EXISTS games_branch ON games (branch_of);
CREATE INDEX IF NOT EXISTS events_game ON events (game_id);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit, explicit transactions; one connection may serve several threads (LabDB locks around it)
    conn = sqlite3.connect(str(path), timeout=60.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    return conn


class LabDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DB_PATH)
        self.conn = _Locked(connect(self.path))

    def close(self) -> None:
        self.conn.close()

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.fetch(sql, args)

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        rows = self.conn.fetch(sql, args, one=True)
        return rows[0] if rows else None

    # -- runs ---------------------------------------------------------------------------

    def start_run(self, kind: str, command: str, params: dict, *, note: str = "", experiment: str = "") -> int:
        from . import FAMILY

        commit, dirty = git_state()
        params = {**params, "family": FAMILY or "main"}  # (the session that ran it: AGENTS.md)
        cur = self.conn.execute(
            "INSERT INTO runs (started, kind, command, git_commit, git_dirty, params, note, experiment, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (time.time(), kind, command, commit, int(dirty), json.dumps(params, default=str), note, experiment or None))
        return int(cur.lastrowid)

    def end_run(self, run_id: int, status: str = "done") -> None:
        n = self.run_games(run_id)
        self.conn.execute("UPDATE runs SET ended = ?, status = ?, games = ? WHERE id = ?",
                          (time.time(), status, n, run_id))

    def run_games(self, run_id: int) -> int:
        """The run's games: played in it, or reused into it (lab/reuse.py)."""
        return self.one("SELECT (SELECT COUNT(*) FROM games WHERE run_id = ?) + "
                        "(SELECT COUNT(*) FROM run_games WHERE run_id = ?) AS n", run_id, run_id)["n"]

    # -- bots ---------------------------------------------------------------------------

    def note_version(self, version: str, bot: str, kind: str, spec: dict, code_hash: str) -> None:
        commit, _ = git_state()
        self.conn.execute(
            "INSERT OR IGNORE INTO bot_versions (version, bot, kind, spec, code_hash, git_commit, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, bot, kind, json.dumps(spec, sort_keys=True), code_hash, commit, time.time()))

    # -- games --------------------------------------------------------------------------

    def save_game(self, game: dict, seats: list[dict], timeline: list[tuple], events: list[tuple],
                  decisions: dict[int, list]) -> None:
        """One finished game, in one transaction."""
        with self.conn.lock:
            self._save_game(self.conn.raw, game, seats, timeline, events, decisions)

    @staticmethod
    def _save_game(c, game, seats, timeline, events, decisions) -> None:
        c.execute("BEGIN IMMEDIATE")
        try:
            cols = list(game)
            c.execute(f"INSERT OR REPLACE INTO games ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [game[k] for k in cols])
            for s in seats:
                cols = list(s)
                c.execute(f"INSERT OR REPLACE INTO seats ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          [s[k] for k in cols])
            c.executemany("INSERT OR REPLACE INTO timeline VALUES (" + ", ".join("?" * 18) + ")", timeline)
            c.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", events)
            for pos, rows in decisions.items():
                c.execute("INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?)",
                          (game["id"], pos, len(rows), pack(rows)))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def decisions(self, game_id: str, position: int) -> list[dict]:
        row = self.one("SELECT data FROM decisions WHERE game_id = ? AND position = ?", game_id, position)
        return unpack(row["data"]) if row else []

    def find_game(self, ref: str) -> sqlite3.Row | None:
        """A game by id, unique id prefix (with or without ``sim-``), or ``last`` / ``last-N``."""
        if ref == "last" or ref.startswith("last-"):
            n = int(ref[5:] or 0) if ref != "last" else 0
            return self.one("SELECT * FROM games ORDER BY finished DESC LIMIT 1 OFFSET ?", n)
        row = self.one("SELECT * FROM games WHERE id = ?", ref)
        if row:
            return row
        for prefix in (ref, "lab-" + ref, "sim-" + ref):
            rows = self.q("SELECT * FROM games WHERE id LIKE ? LIMIT 2", prefix + "%")
            if len(rows) == 1:
                return rows[0]
        return None

    # -- notes --------------------------------------------------------------------------

    def add_note(self, text: str, *, game_id: str | None = None, run_id: int | None = None,
                 experiment: str | None = None, author: str = "claude") -> int:
        cur = self.conn.execute(
            "INSERT INTO notes (created, game_id, run_id, experiment, author, text) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), game_id, run_id, experiment, author, text))
        return int(cur.lastrowid)

    def human_log(self, game_id: str, tick: int | None, command: str, answer: str) -> None:
        self.conn.execute("INSERT INTO human_log VALUES (?, ?, ?, ?, ?)",
                          (game_id, time.time(), tick, command, answer[:4000]))


class _Locked:
    """A connection used by several threads, one statement at a time."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        import threading

        self.raw = conn
        self.lock = threading.RLock()

    def execute(self, sql: str, args=()):
        with self.lock:
            return self.raw.execute(sql, args)

    def executescript(self, sql: str):
        with self.lock:
            return self.raw.executescript(sql)

    def fetch(self, sql: str, args=(), one: bool = False) -> list:
        with self.lock:
            cur = self.raw.execute(sql, args)
            if one:
                row = cur.fetchone()
                return [row] if row is not None else []
            return cur.fetchall()

    def close(self) -> None:
        with self.lock:
            self.raw.close()


def pack(rows: list) -> bytes:
    return zlib.compress(json.dumps(rows, separators=(",", ":"), default=str).encode(), 6)


def unpack(blob: bytes) -> list:
    return json.loads(zlib.decompress(blob))


_GIT: tuple[str, bool] | None = None


def git_state() -> tuple[str, bool]:
    """The checkout's commit and whether it has uncommitted changes (once per process)."""
    global _GIT
    if _GIT is None:
        import subprocess

        from . import MAIN_ROOT, ROOT

        def git(*args: str) -> str:
            try:
                return subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-c", f"safe.directory={MAIN_ROOT}", *args],
                                      cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return ""

        _GIT = (git("rev-parse", "--short", "HEAD") or "unknown", bool(git("status", "--porcelain", "--untracked-files=no")))
    return _GIT
