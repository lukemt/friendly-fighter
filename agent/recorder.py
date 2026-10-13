"""Match recorder: saves what the real game server sends, for offline study.

Every match writes ``recordings/<session_id>/seat<N>.jsonl.gz``, one JSON
object per line:

- ``{"kind": "config", ...}``: the game's ``get_game_config`` reference
  (rules, order schema, maps), once per match;
- ``{"kind": "state", ...}``: each raw state the agent decided on;
- ``{"kind": "action", ...}``: each move the agent sent;
- ``{"kind": "result", ...}``: each ``play_action`` answer (or the error the
  runner recovered from).

These are the ground truth for building a local copy of the game server.
Nothing secret is in them: states, moves and answers carry no keys or seat
tokens. ``recordings/`` is gitignored. Set ``FF_RECORD=0`` to turn it off.
Recording never breaks play: any write error disables the recorder.
"""

from __future__ import annotations

import atexit
import gzip
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from altruagent.console import print_line

ROOT = Path(__file__).resolve().parent.parent / "recordings"
FLUSH_EVERY = 25  # records between flushes, so a killed worker loses little


def _safe(name: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", "unknown" if name is None or name == "" else str(name))[:80]


def _jsonable(value: Any) -> Any:
    """Moves may be WithReasoning or sentinels: keep what JSON can hold."""
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    action = getattr(value, "action", None)
    if action is not None:
        return {"action": _jsonable(action), "reasoning": getattr(value, "reasoning_summary", None)}
    return repr(value)


class Recorder:
    def __init__(self, root: Path = ROOT) -> None:
        self._root = root
        self._file: gzip.GzipFile | None = None
        self._enabled = os.environ.get("FF_RECORD", "1") != "0"
        self._pending = 0
        self._config_written = False

    def _open(self, context) -> gzip.GzipFile | None:
        if self._file is not None or not self._enabled:
            return self._file
        try:
            folder = self._root / _safe(context.session_id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"seat{_safe(context.seat_position)}.jsonl.gz"
            self._file = gzip.open(path, "ab")
            # Close (flush + gzip trailer) at exit: left to the garbage collector,
            # the underlying file can be finalized first and the tail is lost.
            atexit.register(self.close)
            print_line(f"[ff] recording to {path}")
        except OSError as exc:
            self._disable(exc)
        return self._file

    def _write(self, context, record: dict[str, Any]) -> None:
        handle = self._open(context)
        if handle is None:
            return
        try:
            record = {"t": round(time.time(), 3), **record}
            handle.write(json.dumps(record, separators=(",", ":"), default=repr).encode() + b"\n")
            self._pending += 1
            if self._pending >= FLUSH_EVERY:
                handle.flush()
                self._pending = 0
        except (OSError, ValueError, TypeError) as exc:
            self._disable(exc)

    def _disable(self, exc: Exception) -> None:
        self._enabled = False
        print_line(f"[ff] recorder off: {type(exc).__name__}: {exc}")
        try:
            if self._file is not None:
                self._file.close()
        except OSError:
            pass
        self._file = None

    # -- what the agent sees and does ---------------------------------------------------

    def state(self, state, context) -> None:
        if not self._config_written and getattr(context, "game_config", None) is not None:
            self._config_written = True
            self._write(context, {"kind": "config", "game_type": context.game_type, "config": context.game_config})
        self._write(context, {"kind": "state", "game_type": context.game_type, "raw": state.raw})

    def action(self, move, context) -> None:
        self._write(context, {"kind": "action", "move": _jsonable(move)})

    def result(self, result, context) -> None:
        self._write(context, {"kind": "result", "result": _jsonable(result)})

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
