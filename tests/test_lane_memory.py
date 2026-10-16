"""Lane engines are restarted between games once they grow past FF_LANE_MAX_RSS_MB."""

from __future__ import annotations

import os

from ffsim import engine_openra as eo


class _Proc:
    pid = os.getpid()

    def poll(self):
        return None


def _pool(monkeypatch, rss):
    made = []

    def new(self, lane=None):
        host = eo.EngineHost.__new__(eo.EngineHost)
        host.busy, host.games, host.proc, host.dir = False, 0, _Proc(), lane
        host.stop = lambda kill=False: None
        made.append(host)
        return host

    monkeypatch.setattr(eo.HostPool, "_new", new)
    monkeypatch.setattr(eo.EngineHost, "rss_mb", lambda self: rss[0])
    return eo.HostPool(eo.Path("/nonexistent"), 1, persist=True), made


def test_a_big_engine_is_restarted_before_its_next_game(monkeypatch):
    rss = [300.0]
    pool, made = _pool(monkeypatch, rss)
    first = pool.take()
    pool.release(first)
    assert pool.take() is first and len(made) == 1
    pool.release(first)
    rss[0] = eo.LANE_MAX_RSS_MB + 1
    assert pool.take() is not first and len(made) == 2


def test_the_real_process_size_is_read():
    host = eo.EngineHost.__new__(eo.EngineHost)
    host.proc = _Proc()
    assert host.rss_mb() > 1
