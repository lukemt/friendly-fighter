"""Codex as a bot's advisor: a scripted desk (``advisor="bots.codex_advisor:CodexDesk"``) that puts
each of a contestant bot's model questions to Codex through ``codex app-server`` (the user's ChatGPT
login, no API key).

    FF_LAB_CODEX=1 lab compare ff-council-champ-v10 --baselines ext-henry-codex --games 2 ...

- **Opt-in.** A desk is refused without ``FF_LAB_CODEX=1``: the game fails, loudly, instead of
  playing the bot without its advisor. Codex runs on the user's ChatGPT quota (AGENTS.md rule 11).
- **Models** (the user's call, 2026-10-16): GPT-6-Luna freely; never GPT-6-Astra. ``HenryCodexSol``
  is the second variant (there is no 6.1-sol on the account's model list).
- **As at the desk** (lab/advise.py): the bot's own system prompt, payload and answer schema; the
  seat's think is ``paused``, so no game time passes while Codex thinks: an advisor without latency,
  an upper bound. Each call's latency is logged, so how many would have missed a platform deadline
  is known (``sim/lab/advise/codex/<session>-seat<N>.jsonl``).
- **Codex only answers.** Each question is a new ephemeral thread in an empty folder, read-only
  sandbox, no approvals (any approval request is declined), no secrets in its environment (its own
  login is in ``~/.codex``). A failure, a timeout or an answer that doesn't fit the schema is a
  :class:`~lab.advise.Skipped`: the bot gets a model error and plays on by its rules.
- ``HenryCodexDesk`` adapts ext-henry's advisor to the lab without touching ``bots/external.py``
  (every external bot's version hashes it): see :meth:`HenryCodexDesk.adapt`.
"""

from __future__ import annotations

import gc
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import Future
from pathlib import Path

from lab import LAB_DIR
from lab.advise import Skipped, layer_of, validate

OPT_IN = "FF_LAB_CODEX"
CODEX_DIR = LAB_DIR / "advise" / "codex"
TIMEOUT_SECONDS = 120.0
INSTRUCTIONS = (
    "You are the advisor of a bot playing a real-time strategy game (Red Alert). Each message is one "
    "question from the bot: its instructions to its advisor, then the game state as JSON. Answer at once "
    "with the JSON object the answer schema asks for, and nothing else. Don't run commands, read files or "
    "search the web: everything you need is in the message."
)
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
# Codex's tools and add-ons, off: an advisor only answers (and each tool's description costs tokens in
# every call).
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "view_image", "sleep_tool", "multi_agent", "apps", "plugins",
    "browser_use", "browser_use_external", "computer_use", "image_generation", "goals", "skill_search",
    "tool_suggest", "hooks", "in_app_browser", "memories", "workspace_dependencies", "realtime_conversation",
)
COMMAND = ["codex", "app-server", *(a for f in DISABLED_FEATURES for a in ("--disable", f))]


def allowed() -> bool:
    return os.environ.get(OPT_IN, "").strip() == "1"


def child_env() -> dict[str, str]:
    """The environment for Codex: ours without anything that looks like a credential."""
    return {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in _SECRET_WORDS)}


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()  # (our own child, by its handle)
            proc.wait()


class CodexServer:
    """One ``codex app-server`` child, spoken to in JSON-RPC over stdio; one question at a time."""

    def __init__(self, command: list[str] | None = None, cwd: Path | None = None) -> None:
        self.command = list(command or COMMAND)
        self.cwd = Path(cwd or CODEX_DIR / "cwd")
        self.proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._events: queue.Queue = queue.Queue()
        self._write = threading.Lock()
        self._asking = threading.Lock()
        self.declined = 0  # (approval requests answered "decline")

    # -- the connection --

    def start(self) -> "CodexServer":
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.cwd, env=child_env(), text=True, bufsize=1)
        weakref.finalize(self, _stop, self.proc)
        threading.Thread(target=self._read, name="codex-reader", daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "ff-lab", "title": "friendly-fighter lab", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})
        return self

    def close(self) -> None:
        if self.proc is not None:
            _stop(self.proc)

    def _send(self, msg: dict) -> None:
        with self._write:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 30.0):
        rid = next(self._ids)
        fut: Future = Future()
        self._pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return fut.result(timeout)
        finally:
            self._pending.pop(rid, None)

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and "method" in msg:
                self._server_request(msg)
            elif "id" in msg:
                fut = self._pending.get(msg["id"])
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(f"codex: {(msg['error'] or {}).get('message', msg['error'])}"))
                    else:
                        fut.set_result(msg.get("result"))
            elif "method" in msg:
                self._events.put(msg)
        for fut in list(self._pending.values()):  # (codex is gone)
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server exited"))
        self._events.put(None)

    def _server_request(self, msg: dict) -> None:
        """Codex asks us something: approvals are declined, everything else is refused."""
        method = str(msg.get("method"))
        if method.endswith("requestApproval"):
            self.declined += 1
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"decision": "decline"}})
        elif method in ("execCommandApproval", "applyPatchApproval"):
            self.declined += 1
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"decision": "denied"}})
        else:
            self._send({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601, "message": "not available to the lab's advisor"}})

    # -- one question --

    def ask(self, *, instructions: str, text: str, schema: dict | None, model: str, effort: str,
            timeout: float = TIMEOUT_SECONDS) -> tuple[str, dict]:
        """Codex's final message for one question (a new thread), and what it cost. Codex's own agent
        instructions are replaced by :data:`INSTRUCTIONS` (``baseInstructions``: ~20k fewer tokens a call);
        ``instructions`` are the bot's own system prompt."""
        with self._asking:
            while not self._events.empty():  # (leftovers of an earlier question)
                self._events.get_nowait()
            started = self.request("thread/start", {
                "model": model, "cwd": str(self.cwd), "approvalPolicy": "never", "sandbox": "read-only",
                "baseInstructions": INSTRUCTIONS, "developerInstructions": instructions, "ephemeral": True})
            thread = (started.get("thread") or {}).get("id") or started.get("threadId")
            params = {"threadId": thread, "input": [{"type": "text", "text": text}], "effort": effort, "model": model}
            if schema:
                params["outputSchema"] = schema
            self.request("turn/start", params)
            deadline = time.monotonic() + timeout
            message, tokens, error = "", {}, ""
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    try:
                        self.request("turn/interrupt", {"threadId": thread}, timeout=5)
                    except Exception:  # noqa: BLE001 - it timed out anyway
                        pass
                    raise Skipped(f"codex: no answer in {timeout:g} s")
                try:
                    ev = self._events.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if ev is None:
                    raise Skipped("codex app-server exited")
                p = ev.get("params") or {}
                if p.get("threadId") not in (None, thread):
                    continue
                method = ev.get("method")
                if method == "item/completed" and (p.get("item") or {}).get("type") == "agentMessage":
                    message = p["item"].get("text") or ""
                elif method == "thread/tokenUsage/updated":
                    tokens = ((p.get("tokenUsage") or {}).get("total")) or tokens
                elif method == "error" and not p.get("willRetry"):
                    error = str((p.get("error") or {}).get("message") or p.get("error"))
                elif method == "turn/completed":
                    turn = p.get("turn") or {}
                    if turn.get("status") != "completed":
                        raise Skipped(f"codex: turn {turn.get('status')}: {error or (turn.get('error') or {}).get('message', '')}"[:300])
                    return message, {"tokens": tokens, "duration_ms": turn.get("durationMs")}

    def rate_limits(self) -> dict:
        return self.request("account/rateLimits/read", {}) or {}


def start_server() -> CodexServer:
    """A running ``codex app-server`` (tests replace this with a fake one)."""
    return CodexServer().start()


def parse_answer(text: str):
    """The JSON object in Codex's final message (a fenced block too)."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except ValueError:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            return json.loads(s[a:b + 1])
        raise


class CodexDesk:
    """A scripted desk (``Desk.ask``'s signature): Codex answers each question."""

    MODEL = "gpt-6-luna"
    EFFORT = "low"
    TIMEOUT = TIMEOUT_SECONDS

    def __init__(self, server_factory=None) -> None:
        if not allowed():
            raise RuntimeError(f"a Codex advisor uses the user's ChatGPT quota: play it with {OPT_IN}=1 "
                               "(AGENTS.md rule 11)")
        self._server_factory = server_factory
        self.server: CodexServer | None = None
        self.seat = None
        self.calls = 0
        self.records: list[dict] = []

    def attach(self, seat) -> None:
        self.seat = seat

    def adapt(self, seat) -> None:
        """A bot-specific fix-up on the first question of a seat (none here)."""

    def ask(self, *, system: str, payload, schema: dict, seat=None, model: str = "") -> dict:
        seat = seat if seat is not None else self.seat
        self.adapt(seat)
        self.calls += 1
        row = {"n": self.calls, "layer": layer_of(schema or {}), "model": self.MODEL, "effort": self.EFFORT,
               "asked_by": model, "game_seconds": float(getattr(getattr(seat, "clock", None), "seconds", 0.0)),
               "asked": time.time()}
        started = time.monotonic()
        try:
            if self.server is None:
                self.server = (self._server_factory or start_server)()
            text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"), default=str)
            message, info = self.server.ask(instructions=system or "", text=text,
                                            schema=schema or None, model=self.MODEL, effort=self.EFFORT,
                                            timeout=self.TIMEOUT)
            row.update(info)
            try:
                answer = parse_answer(message)
            except ValueError:
                raise Skipped(f"codex: not JSON: {message[:120]!r}") from None
            problems = validate(answer, schema) if schema else []
            if problems:
                raise Skipped(f"codex: the answer doesn't fit the schema: {'; '.join(problems[:3])}")
            row["answer"] = answer
            return answer
        except Skipped as exc:
            row["skipped"] = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - a model problem must never stall the game
            row["skipped"] = f"codex: {type(exc).__name__}: {exc}"[:300]
            raise Skipped(row["skipped"]) from None
        finally:
            row["latency"] = round(time.monotonic() - started, 2)
            self._record(row, seat)

    def _record(self, row: dict, seat) -> None:
        self.records.append(row)
        try:
            CODEX_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{getattr(seat, 'session', '') or 'nosession'}-seat{getattr(seat, 'position', None)}.jsonl"
            with open(CODEX_DIR / name, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass


class InlineExecutor:
    """``ThreadPoolExecutor`` that runs each call at once, on the caller's thread."""

    def submit(self, fn, *args, **kwargs) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - the future carries it, as a pool's would
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


class HenryCodexDesk(CodexDesk):
    """Codex as ext-henry's advisor (henrysue/ucla-steve-nash-equilibrium ``agent/redalert/advisor.py``).

    On the platform henry asks its model every 6 s of wall time on a worker thread and drops a late
    answer. A lab game runs ~20x faster than real time, so on its first question from a seat this
    desk finds that seat's ``Advisor`` and gives it the game's clock (``REDALERT_ADVISOR_SECONDS`` are
    then game seconds) and an inline executor (each later call holds henry's decision; the seat is
    ``paused``, so the game waits for the answer). The first call, at 0:00, still runs on henry's
    thread. Its ``anthropic`` stub gets the two names henry's provider reads on errors. The bot's
    registry entry stubs ``anthropic_credentials`` to True (the stub has no credentials to show).
    """

    ADVISOR_MODULE = "agent.redalert.advisor"

    def __init__(self, server_factory=None) -> None:
        super().__init__(server_factory)
        self._adapted: set[str] = set()

    def adapt(self, seat) -> None:
        alias = getattr(seat, "alias", None)
        if not alias or alias in self._adapted:
            return
        self._adapted.add(alias)
        stub = sys.modules.get(f"{alias}._anthropic")
        if stub is not None and not hasattr(stub, "BadRequestError"):
            stub.BadRequestError = type("BadRequestError", (stub.APIError,), {})  # (never raised by the stub)
            stub.NOT_GIVEN = None
        module = sys.modules.get(f"{alias}.{self.ADVISOR_MODULE}")
        cls = getattr(module, "Advisor", None)
        if cls is None:
            return
        now = float(seat.clock.seconds)
        for obj in gc.get_objects():
            if type(obj) is cls:
                obj._clock = seat.clock.monotonic
                obj._last_call = now
                obj._asked_at = now
                obj._backoff_until = 0.0
                obj._pool = InlineExecutor()


class HenryCodexSol(HenryCodexDesk):
    MODEL = "gpt-6-sol"


def main(argv: list[str]) -> int:
    """``python -m bots.codex_advisor limits``: the account's Codex rate limits (used %, resets)."""
    if argv[:1] != ["limits"]:
        print(__doc__)
        return 1
    server = CodexServer().start()
    try:
        print(json.dumps(server.rate_limits(), indent=1, default=str))
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
