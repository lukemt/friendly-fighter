"""Virtual time: a match that runs as fast as its players and engine allow, timed as on the platform.

On the platform the game runs on the wall clock: 25 ticks a second, whatever the agents do.
Locally the same game runs on a **virtual clock**: the engine (``OpenRAEngine(lockstep=True)``)
runs only the ticks ``VirtualClock`` asks for, and every timing rule of the platform is applied
in virtual seconds (``tick / 25``):

- the network: a call reaches GameAPI half a round trip after it was sent and its answer
  reaches the agent half a round trip after it left; ``rtt`` is either seconds or a list of
  round trips to draw from (``platform_rtts()``: the platform's, measured: median 0.195 s);
- the agent's own time: what it spends between an answer and its next call (``think``):
  ``"measured"`` charges the wall time it really took, a number charges that many seconds;
- GameAPI's own waits (the 0.2 s hold between batches, ``wait_for_update``'s long poll, the
  hand-over) are virtual sleeps.

The clock moves the game forward only as far as nothing could still happen before: never
past a call that is being handled, never past the earliest moment a seat's next call could
arrive. A seat that is "thinking" lets the clock run to ``answer left + rtt + think so far``:
so a seat that stalls is still timed like on the platform, where the game would not wait for
it. Everything else (the engine's ticks, waits, holds) costs no wall time at all.

One lock per match: the clock shares ``Match.lock``/``Match.cond``.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

TICKS_PER_SECOND = 25


def platform_rtts() -> list[float]:
    """Round trips of our calls to the hosted platform (its wire logs), to draw from."""
    data = json.loads((Path(__file__).parent / "data" / "platform_rtt.json").read_text())
    return [float(x) for x in data["samples"]]


class RealClock:
    """The wall clock (the platform's own): what ``Match`` uses unless it runs in virtual time."""

    virtual = False

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def wait(self, cond: threading.Condition, deadline: float) -> None:
        """Called holding ``cond``: wait for a notify or the deadline."""
        cond.wait(max(0.0, min(deadline - self.now(), 0.5)))

    def notify(self, cond: threading.Condition) -> None:
        cond.notify_all()


@dataclass
class _Call:
    seat: int
    arrival: float
    back: float = 0.0  # its answer's way back (half its round trip)
    tool: str = ""
    state: str = "arriving"  # arriving -> running <-> blocked
    wake: float = math.inf  # blocked: virtual time to wake at
    gen: int = 0  # blocked: the notify generation it last saw


@dataclass
class _SeatClock:
    departed: float | None = None  # virtual time the last answer left
    departed_wall: float = 0.0
    back: float = 0.0  # that answer's way back to the agent
    calls: list = field(default_factory=list)
    think: list = field(default_factory=list)  # charged think times (virtual seconds), for reports


class VirtualClock:
    """The clock of one match in virtual time. ``Match`` reads ``now``/``sleep``/``wait``/``notify``;
    the server brackets every MCP call with ``begin``/``end``; ``run`` (a thread) moves the engine."""

    virtual = True

    def __init__(self, cond: threading.Condition, seats: list[int], *, rtt: float | list[float] = 0.19,
                 think: str | float = "measured", patience: float = 5.0, wall=time.monotonic,
                 seed: int | str | None = None) -> None:
        self.cond = cond
        self.rtts = list(rtt) if isinstance(rtt, (list, tuple)) else [float(rtt)]
        self.min_half = min(self.rtts) / 2
        self.rng = random.Random(seed)
        self.think = think
        self.patience = patience  # fixed think: a seat silent this long (wall) is timed by the wall clock
        self.wall = wall
        self.t = 0.0
        self.gen = 0
        self.seats = {p: _SeatClock() for p in seats}
        self.local = threading.local()
        self.running = False  # the engine is under our control
        self.stopped = False
        self.engine = None
        self.on_views = None
        self.on_time = None
        self.on_error = None
        self.stats = {"steps": 0, "views": 0, "idle_waits": 0, "wall_waiting": 0.0}
        self.durations: dict[str, list[float]] = {}  # tool -> round trips as the agent saw them (virtual s)

    # -- what Match uses ------------------------------------------------------------------

    def now(self) -> float:
        return self.t

    def notify(self, cond: threading.Condition | None = None) -> None:
        """Something changed: every call waiting on a condition must look again before time moves."""
        self.gen += 1
        self.cond.notify_all()

    def wait(self, cond: threading.Condition, deadline: float) -> None:
        """Called holding the match lock: wait until something changes or the virtual deadline."""
        call = getattr(self.local, "call", None)
        if call is None or not self.running:
            # Not an MCP call (or the game is not running in virtual time): nothing can change
            # without a notify, which wakes us.
            self.cond.wait(0.5)
            return
        call.state, call.wake, call.gen = "blocked", deadline, self.gen
        self.cond.notify_all()
        while self.running and self.gen == call.gen and self.t < deadline:
            self.cond.wait()
        call.state = "running"

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self.cond:
            call = getattr(self.local, "call", None)
            idle = not self.running and not self.stopped  # the game has not started: wall time
            if call is None or not self.running:
                call = None
        if call is None:
            if idle:
                time.sleep(seconds)
            return
        with self.cond:
            if not self.running:
                return
            until = self.t + seconds
            call.state, call.wake, call.gen = "blocked", until, -1  # a sleep ignores notifies
            self.cond.notify_all()
            while self.running and self.t < until:
                self.cond.wait()
            call.state = "running"

    # -- what the server uses ----------------------------------------------------------------

    def begin(self, seat: int, tool: str = "", received: float | None = None) -> _Call:
        """An MCP call of ``seat`` reached the server (at wall time ``received``): wait until it
        reaches GameAPI in virtual time."""
        now_wall = self.wall() if received is None else received
        with self.cond:
            sc = self.seats[seat]
            half = self.rng.choice(self.rtts) / 2
            if sc.departed is None:
                arrival = self.t + half
            else:
                think = self._think(now_wall - sc.departed_wall)
                sc.think.append(think)
                arrival = sc.departed + sc.back + think + half
            call = _Call(seat, max(arrival, self.t), back=half, tool=tool)
            sc.calls.append(call)
            self.local.call = call
            self.cond.notify_all()
            while self.running and self.t < call.arrival:
                self.cond.wait()
            call.state = "running"
            return call

    def end(self, call: _Call) -> None:
        """Its answer left (the agent gets it ``rtt / 2`` later)."""
        with self.cond:
            sc = self.seats[call.seat]
            if call in sc.calls:
                sc.calls.remove(call)
            if not sc.calls:
                sc.departed, sc.departed_wall, sc.back = self.t, self.wall(), call.back
            if call.tool:
                self.durations.setdefault(call.tool, []).append(self.t - call.arrival + 2 * call.back)
            self.local.call = None
            self.cond.notify_all()

    def _think(self, wall_elapsed: float) -> float:
        if self.think == "measured":
            return wall_elapsed
        return float(self.think) + max(0.0, wall_elapsed - self.patience)

    # -- the conductor --------------------------------------------------------------------

    def horizon(self) -> float:
        """How far virtual time may move: no call may still arrive, wake or need to look before it."""
        h = math.inf
        now_wall = self.wall()
        for sc in self.seats.values():
            if sc.calls:
                for c in sc.calls:
                    if c.state == "running":
                        return self.t
                    if c.state == "arriving":
                        h = min(h, c.arrival)
                    elif c.gen != -1 and c.gen != self.gen:
                        return self.t  # woken by a change: let it look first
                    else:
                        h = min(h, c.wake)
            elif sc.departed is not None:
                h = min(h, sc.departed + sc.back + self._think(now_wall - sc.departed_wall) + self.min_half)
            else:
                h = min(h, self.t + self.min_half)  # a seat yet to call
        if h == math.inf:  # no agent seat (OpenRA bots only): any pace will do
            h = self.t + 1.0
        return max(h, self.t)

    def start(self, engine, on_views, on_error, on_time=None) -> None:
        self.engine, self.on_views, self.on_error, self.on_time = engine, on_views, on_error, on_time
        with self.cond:
            for sc in self.seats.values():
                if sc.departed is None and not sc.calls:  # (checked in, not calling): timed from now
                    sc.departed, sc.departed_wall = self.t, self.wall()
            self.running = True
            self.cond.notify_all()  # calls waiting since before the start: block properly now
        threading.Thread(target=self.run, name="vclock", daemon=True).start()

    def stop(self) -> None:
        with self.cond:
            self.stopped = True
            self.running = False
            self.cond.notify_all()

    def run(self) -> None:
        engine = self.engine
        try:
            views = engine.step(0)  # waits for the engine to load the map
            with self.cond:
                self.t = max(self.t, engine.tick / TICKS_PER_SECOND)
                if views:
                    self.on_views(views)
            interval, phase = engine.view_interval, engine.view_phase
            while True:
                with self.cond:
                    while True:
                        if self.stopped:
                            return
                        # The engine moves one view at a time: nothing between two views can be seen,
                        # and an order is never due before the view after the one it was stamped with.
                        target = engine.tick + 1 + (phase - engine.tick - 1) % interval
                        h = self.horizon()
                        # Time only jumps to what happens next: a call arriving, a wait ending, the view.
                        event = min(self._next_event(), target / TICKS_PER_SECOND)
                        if event <= h + 1e-9:
                            if event >= target / TICKS_PER_SECOND - 1e-9:
                                break
                            self.t = event
                            if self.on_time:
                                self.on_time()
                            self.cond.notify_all()
                            continue
                        self._idle_wait(h, event)
                views = engine.step(target)
                with self.cond:
                    self.stats["steps"] += 1
                    self.stats["views"] += len(views)
                    self.t = max(self.t, engine.tick / TICKS_PER_SECOND)
                    if views:
                        self.on_views(views)
                    if self.on_time:
                        self.on_time()
                    self.cond.notify_all()
        except Exception as exc:  # noqa: BLE001
            with self.cond:
                stopped = self.stopped
                self.running = False
                self.cond.notify_all()
            if not stopped and self.on_error:
                self.on_error(repr(exc))

    def _next_event(self) -> float:
        """The earliest moment after now that a call arrives or wakes."""
        ev = math.inf
        for sc in self.seats.values():
            for c in sc.calls:
                if c.state == "arriving" and c.arrival > self.t:
                    ev = min(ev, c.arrival)
                elif c.state == "blocked" and self.t < c.wake < math.inf:
                    ev = min(ev, c.wake)
        return ev

    def _idle_wait(self, h: float, event: float) -> None:
        """Time may not reach the next event yet: wait for a call to arrive, end or block, or for the
        wall clock to carry a thinking seat's horizon there."""
        timeout = None
        now_wall = self.wall()
        thinking = [sc for sc in self.seats.values() if not sc.calls and sc.departed is not None]
        if thinking and h > self.t - 1e-9 and not any(c.state == "running" for sc in self.seats.values() for c in sc.calls):
            if self.think == "measured":
                timeout = event - h  # the horizon grows with the wall clock
            else:
                left = min(self.patience - (now_wall - sc.departed_wall) for sc in thinking)
                timeout = left if left > 0 else event - h
            timeout = max(0.0005, timeout)
        self.stats["idle_waits"] += 1
        self.cond.wait(timeout)
        self.stats["wall_waiting"] += self.wall() - now_wall

    def report(self) -> dict:
        think = [x for sc in self.seats.values() for x in sc.think]
        think.sort()
        waits = sorted(self.durations.get("wait_for_update", []))
        hist = {}
        for d in waits:
            b = int(d * 100) * 10
            hist[b] = hist.get(b, 0) + 1
        return {**self.stats, "virtual_seconds": round(self.t, 3),
                "wait_ms_histogram": {k: v for k, v in sorted(hist.items()) if v * 100 >= len(waits)},
                "think_ms_p50": round(think[len(think) // 2] * 1000, 2) if think else None,
                "think_ms_p90": round(think[int(len(think) * 0.9)] * 1000, 2) if think else None,
                "rtt_ms_p50": round(sorted(self.rtts)[len(self.rtts) // 2] * 1000, 1), "think": self.think}
