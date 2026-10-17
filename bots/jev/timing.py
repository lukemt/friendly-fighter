"""A measuring wrapper, not a bot to compete with: v10 (bots/jev/v10) that records what each decision costs.

``choose_action`` is timed twice, by the wall clock and by the process's CPU time, and the numbers are
written at every 100th call (and at the end of the game) to ``$FF_TIMING_LOG`` as one JSON line.
Used to see how much of the platform's real-time budget (a view about 5 times a second) the champion's
own code takes (bots/jev/README.md, experiment jev-think).
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

from .v10.bot import CouncilBot


class TimedBot(CouncilBot):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.wall: list[float] = []
        self.cpu: list[float] = []
        self.game = ""

    def choose_action(self, state, context):
        w, c = time.perf_counter(), time.process_time()
        try:
            return super().choose_action(state, context)
        finally:
            self.wall.append(time.perf_counter() - w)
            self.cpu.append(time.process_time() - c)
            self.game = getattr(context, "session_id", self.game)
            if len(self.wall) % 100 == 0:
                self.report()

    def report(self) -> None:
        path = os.environ.get("FF_TIMING_LOG")
        if not path or not self.wall:
            return
        def q(xs, p):
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(p * len(xs)))]
        row = {"game": self.game, "calls": len(self.wall),
               "wall_ms": {"mean": round(1000 * statistics.fmean(self.wall), 2), "p50": round(1000 * q(self.wall, .5), 2),
                           "p95": round(1000 * q(self.wall, .95), 2), "max": round(1000 * max(self.wall), 2)},
               "cpu_ms": {"mean": round(1000 * statistics.fmean(self.cpu), 2), "p50": round(1000 * q(self.cpu, .5), 2),
                          "p95": round(1000 * q(self.cpu, .95), 2), "max": round(1000 * max(self.cpu), 2)}}
        with Path(path).open("a") as f:
            f.write(json.dumps(row) + "\n")


def create_agent(**kwargs) -> TimedBot:
    return TimedBot(**kwargs)
