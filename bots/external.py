"""Other contestants' bots in the lab: a bot built on the AltruAgent starter, played in-process.

A contestant's repository is cloned into ``sim/external/<repo>`` at a pinned commit (``lab external
fetch``) and never copied into ours. ``create_agent`` loads its code for one seat of one game:

- **Under an alias.** Their ``agent`` package would clash with ours (``from agent.redalert_bot
  import ...`` would silently load *our* bot), so every seat gets its own copy of their modules
  under ``ext_<name>_<n>.agent``. Their absolute imports of their own packages are rewritten to
  relative ones as the source is compiled (in memory; the files are untouched). ``altruagent``
  stays ours: their vendored runtime is the same starter (docs/EXTERNAL_BOTS.md).
- **On the game's clock.** Their timers read ``time.monotonic()``. Lab games run in virtual time,
  ~50x faster than the wall clock, so each copy's ``time`` is the seat's game clock (tick / 25),
  which is what the bot sees on the platform, where both run together. Every thread of the copy
  (their advisor's too) reads the same clock.
- **With its own environment.** Their ``os.environ`` is the process environment overlaid with the
  bot's ``env``; ``ANTHROPIC_API_KEY`` is always set by us. Two variants share one process.
- **Without any API.** ``anthropic`` is replaced by a stub: with no advisor desk it refuses; with
  one (``advisor="desk"``, ``lab advise``) each model request becomes a question for whoever sits
  at the desk (Claude), and the game waits for the answer (lab/advise.py).
"""

from __future__ import annotations

import builtins
import collections
import importlib.abc
import importlib.util
import itertools
import json
import os as _os
import re
import subprocess
import sys
import threading as _threading
import time as _time
import types
from functools import lru_cache
from pathlib import Path

from lab import LAB_DIR, MAIN_ROOT  # (shared by every session: AGENTS.md)

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_DIR = MAIN_ROOT / "sim" / "external"
LOG_DIR = LAB_DIR / "external"
TICKS_PER_SECOND = 25
KEEP_COPIES = 2  # loaded copies kept in sys.modules per process: a game has two seats; older ones go (each copy of a
# big contestant bot holds ~50-100 MB in a lab worker: 8 kept made workers 200-340 MB, 2026-10-16)

_counter = itertools.count(1)
_copies: collections.deque = collections.deque()
_code_cache: dict[tuple[str, int, int], types.CodeType] = {}


class ExternalBotError(RuntimeError):
    pass


# -- the checkout ---------------------------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(path), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@lru_cache(maxsize=None)
def check_checkout(repo: str, commit: str) -> Path:
    """``sim/external/<repo>`` at ``commit`` with no local changes, or an error saying what to run."""
    path = EXTERNAL_DIR / repo
    if not (path / ".git").exists():
        raise ExternalBotError(f"{path} is not there: run 'lab external fetch' first")
    try:
        head = _git(path, "rev-parse", "HEAD")
        dirty = _git(path, "status", "--porcelain", "--untracked-files=no")
    except subprocess.CalledProcessError as exc:
        raise ExternalBotError(f"{path}: git failed ({exc.stderr.strip()})") from exc
    if not head.startswith(commit):
        raise ExternalBotError(f"{path} is at {head[:12]}, the bot is pinned to {commit}: run 'lab external fetch'")
    if dirty:
        raise ExternalBotError(f"{path} has local changes; the bot's version would not say what played:\n{dirty}")
    return path


def fetch(repo: str, url: str, commit: str) -> str:
    """Clone ``url`` into ``sim/external/<repo>`` and check out ``commit``."""
    path = EXTERNAL_DIR / repo
    if not (path / ".git").exists():
        EXTERNAL_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "safe.directory=*", "clone", "-q", url, str(path)], check=True)
    elif not _git(path, "rev-parse", "HEAD").startswith(commit):
        _git(path, "fetch", "-q", "origin")
    if not _git(path, "rev-parse", "HEAD").startswith(commit):
        _git(path, "checkout", "-q", commit)
    check_checkout.cache_clear()
    check_checkout(repo, commit)
    return f"{repo} at {commit} in {path}"


# -- what one seat's copy sees instead of time, os, threading and print ---------------------------


class GameClock:
    """``time`` for one seat's copy: the game's seconds (tick / 25) instead of the wall clock."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def advance(self, seconds: float) -> None:
        if seconds > self.seconds:
            self.seconds = seconds

    def monotonic(self) -> float:
        return self.seconds

    time = perf_counter = monotonic

    def __getattr__(self, name):  # (sleep, strftime, ...: the real ones)
        return getattr(_time, name)


class SeatOs:
    """``os`` with the bot's own environment: the process's, overlaid with the bot's settings."""

    def __init__(self, env: dict[str, str]) -> None:
        self.environ = {**_os.environ, **env}

    def getenv(self, key: str, default=None):
        return self.environ.get(key, default)

    def __getattr__(self, name):
        return getattr(_os, name)


class InlineThreading:
    """``threading`` whose threads run at once, on the caller's thread: an advisor's request then
    holds the seat's decision until the desk answers (the game waits: the seat's think is paused)."""

    class Thread:
        def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None) -> None:
            self._target, self._args, self._kwargs = target, args, kwargs or {}
            self.name, self.daemon = name, daemon

        def start(self) -> None:
            if self._target is not None:
                self._target(*self._args, **self._kwargs)

        def run(self) -> None:
            self.start()

        def join(self, timeout=None) -> None:
            return None

        def is_alive(self) -> bool:
            return False

    def __getattr__(self, name):
        return getattr(_threading, name)


def anthropic_stub(name: str, seat: "Seat") -> types.ModuleType:
    """What ``import anthropic`` gives a copy: no API ever; with a desk, the desk answers."""
    mod = types.ModuleType(name)

    class APIError(Exception):
        pass

    class _Messages:
        def create(self, **kw):
            from lab.advise import Skipped

            system = kw.get("system")
            if isinstance(system, list):
                system = "\n\n".join(str(b.get("text", "")) for b in system if isinstance(b, dict))
            content = (kw.get("messages") or [{}])[-1].get("content")
            try:
                payload = json.loads(content) if isinstance(content, str) else content
            except ValueError:
                payload = content
            schema = ((kw.get("output_config") or {}).get("format") or {}).get("schema") or {}
            try:
                answer = seat.desk.ask(system=str(system or ""), payload=payload, schema=schema, seat=seat,
                                       model=str(kw.get("model") or ""))
            except Skipped as exc:
                raise APIError(str(exc) or "skipped") from None
            usage = types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0)
            return types.SimpleNamespace(stop_reason="end_turn", usage=usage,
                                         content=[types.SimpleNamespace(type="text", text=json.dumps(answer))])

    class Anthropic:
        def __init__(self, *args, **kwargs) -> None:
            if seat.desk is None:
                raise APIError("external bots call no API in the lab (advisor='desk' plays Claude at a desk)")
            self.messages = _Messages()
            self.beta = types.SimpleNamespace(messages=self.messages)

        def with_options(self, **kwargs) -> "Anthropic":
            return self

    mod.APIError = APIError
    mod.APIStatusError = mod.APIConnectionError = mod.APITimeoutError = APIError
    mod.Anthropic = Anthropic
    return mod


# -- one seat's copy of the bot -------------------------------------------------------------------


class Seat:
    """One seat's copy of a contestant's bot: its alias, clock, environment, log and desk."""

    def __init__(self, name: str, root: Path, packages: tuple[str, ...], env: dict[str, str], desk=None,
                 log_files: bool = False) -> None:
        self.name = name
        self.root = root
        self.packages = packages
        slug = re.sub(r"[^0-9a-zA-Z]+", "_", name)
        self.alias = f"{slug if slug.startswith('ext_') else 'ext_' + slug}_{next(_counter)}"
        self.clock = GameClock()
        self.desk = desk
        self.os = SeatOs(env)
        self.lines: collections.deque[str] = collections.deque(maxlen=2000)
        self.log_files = log_files
        self.session = ""
        self.position: int | None = None
        self.last_raw: dict | None = None
        self.map_id = ""
        self._log_file = None

    # -- the log: their print() --

    def print(self, *args, sep=" ", end="\n", file=None, flush=False) -> None:
        if file is not None and file not in (sys.stdout, sys.stderr):
            builtins.print(*args, sep=sep, end=end, file=file, flush=flush)
            return
        line = sep.join(str(a) for a in args)
        self.lines.append(f"{self.clock.seconds:7.1f}s {line}")
        if self.log_files and self.session:
            if self._log_file is None:
                folder = LOG_DIR / self.name
                folder.mkdir(parents=True, exist_ok=True)
                self._log_file = open(folder / f"{self.session}-seat{self.position}.log", "a", encoding="utf-8")
            self._log_file.write(f"{self.clock.seconds:7.1f}s {line}\n")
            self._log_file.flush()

    # -- loading --

    def load(self, module: str) -> types.ModuleType:
        top = types.ModuleType(self.alias)
        top.__path__ = []
        top.__package__ = self.alias
        stub = anthropic_stub(f"{self.alias}._anthropic", self)
        top._anthropic = stub
        sys.modules[self.alias] = top
        sys.modules[stub.__name__] = stub
        sys.meta_path.insert(0, _Finder(self))
        _copies.append(self.alias)
        while len(_copies) > KEEP_COPIES:
            _forget(_copies.popleft())
        return importlib.import_module(f"{self.alias}.{module}")

    def patch(self, module: types.ModuleType) -> None:
        """After a module ran: its time, os (and threading, at a desk) are the seat's; print, too."""
        ns = module.__dict__
        for key, value in list(ns.items()):
            if value is _time:
                ns[key] = self.clock
            elif value is _os:
                ns[key] = self.os
            elif value is _threading and self.desk is not None:
                ns[key] = InlineThreading()
            elif key == "load_dotenv" and callable(value):
                ns[key] = lambda *a, **k: False  # (their .env loader would read ours)
        ns["print"] = self.print

    def rewrite(self, source: str, module_parts: list[str], is_package: bool, path: str) -> str:
        """Their absolute imports of their own packages (and of anthropic) become relative ones."""
        package = module_parts if is_package else module_parts[:-1]
        up = "." * (len(package) + 1)  # (from the module's package up to the alias)
        for bad, why in ((r"^\s*from\s+time\s+import\b", "from time import ... reads the wall clock"),
                         (r"^\s*from\s+os\s+import\b.*\b(environ|getenv)\b", "from os import environ skips the bot's env")):
            if re.search(bad, source, re.M):
                raise ExternalBotError(f"{path}: {why}; extend bots/external.py to cover it")
        for pkg in self.packages:
            source = re.sub(rf"^(\s*)from\s+{pkg}(\.[\w.]+)?\s+import\b",
                            lambda m: f"{m.group(1)}from {up}{pkg}{m.group(2) or ''} import", source, flags=re.M)
            def plain(m):
                dotted = m.group(2).split(".")
                return f"{m.group(1)}from {up}{'.'.join(dotted[:-1])} import {dotted[-1]} as {m.group(3)}"
            source = re.sub(rf"^(\s*)import\s+({pkg}(?:\.\w+)+)\s+as\s+(\w+)\s*$", plain, source, flags=re.M)
            if re.search(rf"^\s*import\s+{pkg}\b", source, re.M):
                raise ExternalBotError(f"{path}: 'import {pkg}...' without 'as' can't be relocated")
        source = re.sub(r"^(\s*)import\s+anthropic\s*$", rf"\1from {up} import _anthropic as anthropic", source, flags=re.M)
        source = re.sub(r"^(\s*)from\s+anthropic\s+import\b", rf"\1from {up}_anthropic import", source, flags=re.M)
        return source


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, seat: Seat) -> None:
        self.seat = seat

    def find_spec(self, fullname, path=None, target=None):
        prefix = self.seat.alias + "."
        if not fullname.startswith(prefix):
            return None
        parts = fullname[len(prefix):].split(".")
        if parts[0] not in self.seat.packages:
            return None
        base = self.seat.root.joinpath(*parts)
        if (base / "__init__.py").is_file():
            return importlib.util.spec_from_file_location(
                fullname, base / "__init__.py", loader=_Loader(self.seat, base / "__init__.py", parts, True),
                submodule_search_locations=[str(base)])
        if base.with_suffix(".py").is_file():
            file = base.with_suffix(".py")
            return importlib.util.spec_from_file_location(fullname, file, loader=_Loader(self.seat, file, parts, False))
        return None


class _Loader(importlib.abc.SourceLoader):
    """Their source, rewritten and compiled in memory (never cached to disk: no .pyc in their tree)."""

    def __init__(self, seat: Seat, path: Path, parts: list[str], is_package: bool) -> None:
        self.seat, self.path, self.parts, self.package = seat, path, parts, is_package

    def get_filename(self, fullname):
        return str(self.path)

    def get_data(self, path):
        return Path(path).read_bytes()

    def get_code(self, fullname):
        st = self.path.stat()
        key = (str(self.path), st.st_mtime_ns, st.st_size)
        code = _code_cache.get(key)
        if code is None:  # (the rewrite is the same for every copy: compiled once per process)
            source = importlib.util.decode_source(self.path.read_bytes())
            code = compile(self.seat.rewrite(source, self.parts, self.package, str(self.path)), str(self.path), "exec",
                           dont_inherit=True)
            _code_cache[key] = code
        return code

    def exec_module(self, module):
        super().exec_module(module)
        self.seat.patch(module)


def _forget(alias: str) -> None:
    for name in [n for n in sys.modules if n == alias or n.startswith(alias + ".")]:
        del sys.modules[name]
    sys.meta_path[:] = [f for f in sys.meta_path if not (isinstance(f, _Finder) and f.seat.alias == alias)]
    import gc

    gc.collect()  # (a module's functions and globals form cycles: free the copy now, not at some later collection)


# -- the agent the lab seats ----------------------------------------------------------------------


class ExternalAgent:
    """What the runtime plays: their agent, with the seat's clock moved to each view's tick first."""

    def __init__(self, inner, seat: Seat) -> None:
        self.inner = inner
        self.seat = seat
        self._choose = inner if callable(inner) and not hasattr(inner, "choose_action") else inner.choose_action

    def choose_action(self, state, context):
        raw = getattr(state, "raw", None)
        obs = raw.get("observation") if isinstance(raw, dict) else None
        if isinstance(obs, dict) and isinstance(obs.get("tick"), int):
            self.seat.clock.advance(obs["tick"] / TICKS_PER_SECOND)
        if isinstance(raw, dict):
            self.seat.last_raw = raw
        if not self.seat.session:
            self.seat.session = str(getattr(context, "session_id", "") or "")
            self.seat.position = getattr(context, "seat_position", None)
            if self.seat.desk is not None:
                self.seat.desk.attach(self.seat)
        return self._choose(state, context)

    def on_action_result(self, result, context) -> None:
        method = getattr(self.inner, "on_action_result", None)
        if callable(method):
            method(result, context)


def create_agent(*, repo: str, commit: str, url: str = "", entry: str = "agent.agent:create_agent",
                 packages: list[str] | tuple[str, ...] = ("agent",), env: dict | None = None, name: str = "",
                 advisor: str | None = None, stubs: dict | None = None, log_files: bool | None = None,
                 subdir: str = "") -> ExternalAgent:
    """One seat's copy of a contestant's bot (see the module docstring).

    ``advisor="desk"``: their model calls go to the advisor desk of the running ``lab advise`` session
    (an error outside one, so leagues never wait for an answer); ``advisor="package.module:Class"``: a
    scripted advisor with ``Desk.ask``'s signature answers them (e.g. ``bots.ryan_plan:PlanDesk``). ``stubs``: ``{"module:function":
    value}`` replaces a function of theirs by one that returns ``value`` (e.g. a per-game dump). ``subdir``:
    where their packages are in the checkout (``src`` for a src layout; default the top)."""
    root = check_checkout(repo, commit) / subdir if subdir else check_checkout(repo, commit)
    name = name or repo
    desk = None
    if advisor == "desk":
        from lab.advise import current_desk

        desk = current_desk()
        if desk is None:
            raise ExternalBotError(f"{name} is advised by Claude: play it with 'lab advise start --bot {name}'")
    elif advisor and ":" in advisor:  # (a scripted advisor: "package.module:Class", one per seat)
        mod_name, _, cls = advisor.partition(":")
        desk = getattr(importlib.import_module(mod_name), cls)()
    elif advisor:
        raise ExternalBotError(f"unknown advisor {advisor!r} ('desk' or 'package.module:Class')")
    seat_env = {str(k): str(v) for k, v in (env or {}).items()}
    seat_env["ANTHROPIC_API_KEY"] = "desk" if desk is not None else ""
    seat_env.setdefault("REDALERT_DUMP_DIR", str(LOG_DIR / name))
    if log_files is None:
        log_files = desk is not None or _os.environ.get("LAB_EXTERNAL_LOGS") == "1"
    seat = Seat(name, root, tuple(packages), seat_env, desk=desk, log_files=log_files)
    module, _, func = entry.partition(":")
    factory = getattr(seat.load(module), func or "create_agent")
    for target, value in (stubs or {}).items():
        mod_name, _, attr = target.partition(":")
        setattr(importlib.import_module(f"{seat.alias}.{mod_name}"), attr, lambda *a, _v=value, **k: _v)
    return ExternalAgent(factory(), seat)


def registered(table: dict) -> list:
    """The registered bots played by this module."""
    return [b for b in table.values()
            if b.kind == "module" and str(b.params.get("factory", "")).startswith("bots.external:")]
