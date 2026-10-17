"""Engine traces: what a virtual-time game asked of its engine, step by step.

A trace (``OpenRAEngine(trace_path=...)`` or ``FF_ENGINE_TRACE``) is one JSON header line (the
session: map, players, seed, lobby) and then one line per step: the ``StepRequest`` (the tick to
run to and the actions due), the tick the engine reached, and a digest of the views it returned.
The game is deterministic, so replaying the steps plays the same game again: that is how
``scripts/ffbench.py`` checks an engine build, and how the lab rebuilds any moment of a recorded
game (``lab look``) or starts new games from it (``lab branch``).
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

from .proto import rl_bridge_pb2 as pb


def _lines(path: Path) -> list[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as handle:
            return handle.read().splitlines()
    return path.read_text().splitlines()


def read_trace(path: Path | str) -> tuple[dict, list[tuple[pb.StepRequest, int, str]]]:
    """The header and the steps (request, tick reached, views digest); a trace cut short ends early."""
    lines = _lines(Path(path))
    head = json.loads(lines[0])
    steps = []
    for line in lines[1:]:
        try:
            req, tick, digest = json.loads(line)
        except ValueError:
            break
        steps.append((pb.StepRequest.FromString(base64.b64decode(req)), tick, digest))
    return head, steps


def prefix_until(steps: list, tick: int) -> list:
    """The steps up to ``tick`` (each step ends on a view: the snapshot is the last view at or before it)."""
    return [s for s in steps if s[0].until_tick <= tick]
