"""Virtual time (ffsim.vclock): a match runs as fast as its agents allow, timed as on the platform.

Agents here are threads that call the Match the way the server does for an MCP call
(``clock.begin`` / tool / ``clock.end``), on the fake engine.
"""

from __future__ import annotations

import threading
import time

from ffsim.engine_fake import FakeEngine
from ffsim.gameapi import PLATFORM_TIMING, Match, SeatConfig

TPS = 25


def make_match(*, think=0.003, rtt=0.19, timing=PLATFORM_TIMING, seed=1, kinds=("agent", "agent"), max_minutes=20):
    engines = []

    def factory(match):
        e = FakeEngine(list(kinds))
        engines.append(e)
        return e

    seats = [SeatConfig(kind=k, agent_id=f"agent-{i}" if k == "agent" else "openra-ai", name=f"P{i}", token=f"tok{i}")
             for i, k in enumerate(kinds)]
    m = Match("sess-v", "trapped", seats, factory, max_minutes=max_minutes, virtual={"rtt": rtt, "think": think},
              timing=timing, seed=seed)
    return m, engines


class Agent:
    """One seat's runtime loop: read the state, maybe send a batch, wait for the next state."""

    def __init__(self, m: Match, position: int, *, until: float, play_every: int = 0, log=None) -> None:
        self.m, self.seat, self.until = m, m.seats[position], until
        self.play_every = play_every
        self.log = log if log is not None else []
        self.answers: list[dict] = []
        self.states: list[int] = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def call(self, tool, fn):
        call = self.m.vclock.begin(self.seat.position, tool)
        try:
            out = fn()
        finally:
            self.m.vclock.end(call)
        self.log.append((self.seat.position, tool, round(call.arrival, 6)))
        return out

    def run(self) -> None:
        m, seat = self.m, self.seat
        self.call("check_in", lambda: m.check_in(seat))
        st = self.call("get_game_state", lambda: m.state(seat))
        n = 0
        while not st["is_terminal"] and m.vclock.now() < self.until:
            if st["phase"] == "playing" and st["is_current_actor"]:
                self.states.append(st["state_version"])
                n += 1
                if self.play_every and n % self.play_every == 0:
                    sv = st["state_version"]
                    ans = self.call("play_action", lambda: m.play_action(
                        seat, sv, {"type": "orders", "orders": [{"cmd": "stop", "units": [100 + seat.position]}]}))
                    self.answers.append({"sv": sv, **ans})
                    st = self.call("get_game_state", lambda: m.state(seat))
                    continue
            sv, cur, ph = st["state_version"], st["is_current_actor"], st["phase"]
            st = self.call("wait_for_update", lambda: m.wait_for_update(seat, sv, cur, ph, 20.0))


def run_agents(m, until, play_every=(0, 0)):
    log: list = []
    agents = [Agent(m, i, until=until, play_every=play_every[i], log=log) for i in range(2)]
    t0 = time.monotonic()
    for a in agents:
        a.thread.start()
    for a in agents:
        a.thread.join(60)
    return agents, log, time.monotonic() - t0


def test_a_minute_of_game_takes_no_wall_time():
    m, engines = make_match()
    agents, _, wall = run_agents(m, 60.0, play_every=(10, 0))
    assert m.vclock.now() >= 60.0 and wall < 10
    e = engines[0]
    assert e.tick >= 60 * TPS - 6
    # Every view seen, every 6 ticks.
    gaps = {b - a for a, b in zip(agents[1].states, agents[1].states[1:])}
    assert gaps <= {6, 12}
    m.fail("cancelled")


def test_waits_poll_every_tenth_of_a_second_and_batches_land_a_view_late():
    m, _ = make_match()
    agents, _, _ = run_agents(m, 60.0, play_every=(4, 0))
    waits = m.vclock.durations["wait_for_update"][3:]  # (after the start)
    # The agent saw each wait take the round trip plus a whole number of polls.
    assert all(abs(((d - 0.19) / 0.1) - round((d - 0.19) / 0.1)) < 1e-6 for d in waits)
    assert {round((d - 0.19) / 0.1) for d in waits} >= {0, 1}
    lags = [a["state_version"] - a["sv"] for a in agents[0].answers]
    assert lags and set(lags) <= {0, 6, 12} and lags.count(6) > len(lags) / 2
    m.fail("cancelled")


def test_orders_are_stamped_with_a_view_tick_and_applied_twelve_ticks_later():
    m, engines = make_match()
    agents, _, _ = run_agents(m, 20.0, play_every=(5, 0))
    e = engines[0]
    for a in agents[0].answers:
        assert a["state_version"] % 6 == 0  # the fake engine's views fall on multiples of 6
    sent = e.sent
    assert sent and all(s["apply_at"] == a["state_version"] + 12 for s, a in zip(sent, agents[0].answers))
    m.fail("cancelled")


def test_the_hold_between_batches_is_virtual():
    m, _ = make_match(timing={})
    seat = m.seats[0]
    holds = []

    def agent():
        call = m.vclock.begin(0, "check_in")
        m.check_in(seat)
        m.check_in(m.seats[1])
        m.vclock.end(call)
        call = m.vclock.begin(0, "wait")
        while m.phase != "playing":
            m.wait_for_update(seat, m.state(seat)["state_version"], False, m.phase, 20)
        m.vclock.end(call)
        for _ in range(3):
            call = m.vclock.begin(0, "play_action")
            t0 = m.vclock.now()
            m.play_action(seat, m.latest_tick, {"type": "orders", "orders": [{"cmd": "stop", "units": [100]}]})
            holds.append(m.vclock.now() - t0)
            m.vclock.end(call)

    t = threading.Thread(target=agent, daemon=True)
    t0 = time.monotonic()
    t.start()
    # seat 1 never calls again: its clock runs on the wall clock (measured think)
    t.join(30)
    assert not t.is_alive()
    # First batch: only the 20 ms hand-over; then each one waits out the rest of the 0.2 s gap
    # (minus the round trip and hand-over the agent already spent).
    assert abs(holds[0] - 0.02) < 1e-9
    assert all(abs(h - 0.02) < 1e-9 or 0.02 < h <= 0.2 + 0.02 for h in holds[1:])
    assert time.monotonic() - t0 < 10
    m.fail("cancelled")


def test_fixed_think_is_deterministic():
    def once():
        m, engines = make_match(think=0.004, rtt=[0.19, 0.2, 0.23], seed=7)
        agents, log, _ = run_agents(m, 15.0, play_every=(3, 5))
        m.fail("cancelled")
        return sorted(log), [a["state_version"] for a in agents[0].answers], engines[0].tick

    assert once() == once()


def test_a_silent_seat_is_timed_by_the_wall_clock():
    m, engines = make_match(think="measured")
    seat = m.seats[0]

    def agent():
        call = m.vclock.begin(0, "check_in")
        m.check_in(seat)
        m.check_in(m.seats[1])
        m.vclock.end(call)
        time.sleep(0.5)  # "thinking" for half a second of wall time

    t0 = time.monotonic()
    threading.Thread(target=agent, daemon=True).start()
    time.sleep(1.5)
    # Nobody calls: the game still moves on, at about the wall clock's pace (never ahead of it).
    v = m.vclock.now()
    assert 0.4 <= v <= time.monotonic() - t0 + 1.0
    m.fail("cancelled")
