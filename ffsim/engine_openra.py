"""The real game: the platform's OpenRA fork (yxc20089/OpenRA @ 9b271c1 plus
sim/openra-fork/ff-sim.patch), headless, one process per match.

Each agent seat is an ``rl-agent`` bot whose ExternalBotBridge makes a view every
``RL_OBS_INTERVAL`` ticks (on ticks 5, 11, 17... like the platform's) and applies each
AgentAction at its ``apply_at_tick``. An ``ai:<type>`` seat is one of OpenRA's own bots
(``normal``, ``rush``, ``turtle``, ``naval``).

Two ways to run it:

- **real time** (``lockstep=False``): the engine runs at 25 ticks/s on the wall clock and
  streams the views over gRPC (one stream per seat, picked by the ``rl-player`` header);
- **virtual time** (``lockstep=True``, ``RL_LOCKSTEP=1``): the engine runs only the ticks
  ``step(until_tick)`` asks for, as fast as the CPU allows, and returns their views; the
  actions ``send`` queued go to the engine with the next step. ``ffsim.vclock`` owns the clock.
  With an ``EngineHost`` the game is a session of one shared engine process (the engine's
  multi-session mode: no local server, no network between the game and its orders, the map
  data loaded once); without one, each game starts its own engine with ``Launch.Map``.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import os
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import grpc

from .proto import rl_bridge_pb2 as pb
from .proto import rl_bridge_pb2_grpc as pb_grpc
from .rules import MAP_FILES

SIM_HOME = Path(os.environ.get("FF_SIM_HOME", Path.home() / "openra-sim"))
ENGINE_DIR = Path(os.environ.get("FF_ENGINE_DIR", SIM_HOME / "fork"))
DOTNET_ROOT = Path(os.environ.get("FF_DOTNET_ROOT", SIM_HOME / "dotnet8"))
CONTENT_DIR = Path(os.environ.get("FF_RA_CONTENT", SIM_HOME / "src" / "Support" / "Content"))
RUNS_DIR = Path(os.environ.get("FF_SIM_RUNS", SIM_HOME / "runs"))

VIEW_INTERVAL = 6
VIEW_PHASE = 5  # the platform's views fall on ticks 5, 11, 17... (state_version % 6 == 5 in every recording)
# Ticks between the bridge issuing an order and the engine running it (OpenRA's order
# latency in a local game): 4, measured with 1-tick views (an order issued at tick 30 shows
# its Move/Transform activity at 34). The GameAPI subtracts it so orders run 12 ticks
# after they arrive: on the platform an idle unit ordered to move already moves in the
# view 12 ticks after the batch arrived (docs/SIMULATOR.md, "Timing").
ORDER_LATENCY_TICKS = int(os.environ.get("FF_ENGINE_ORDER_LATENCY", "4"))
# The same in virtual time (RL_LOCKSTEP=1), with Launch.Map or as an EngineHost session: 6 (an order
# the bridge issues at tick 4 shows its Move activity at tick 10; scripts/ffsim.py latency).
LOCKSTEP_ORDER_LATENCY_TICKS = int(os.environ.get("FF_LOCKSTEP_ORDER_LATENCY", "6"))
DEFAULT_LOBBY = {"startingcash": "5000", "startingunits": "none", "explored": "False", "crates": "True",
                 "bounty": "False", "shortgame": "True"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cell_of(actor) -> tuple[int, int]:
    return actor.cell_x, actor.cell_y


def view_from_pb(obs: pb.GameObservation) -> dict:
    """The bridge's protobuf observation as the plain view dict ``ffsim.observe`` reads."""

    def unit(u) -> dict:
        x, y = _cell_of(u)
        return {"id": u.actor_id, "type": u.type, "x": x, "y": y, "hp_percent": u.hp_percent, "idle": u.is_idle,
                "activity": u.current_activity or None, "stance": u.stance, "is_husk": u.is_husk}

    def building(b) -> dict:
        return {"id": b.actor_id, "type": b.type, "x": b.cell_x, "y": b.cell_y, "hp_percent": b.hp_percent,
                "producing": b.producing_item or None, "powered_down": b.powered_down, "disabled": b.disabled,
                "repairing": b.is_repairing, "rally": [b.rally_x, b.rally_y]}

    e, m = obs.economy, obs.military
    return {
        "tick": obs.tick,
        "player": obs.player_name,
        "faction": obs.faction or None,
        "enemy_faction": obs.enemy_faction or None,
        "done": obs.done,
        "result": obs.result,
        "winner": obs.winner,
        "explored_percent": obs.explored_percent,
        "economy": {"cash": e.cash, "ore": e.ore, "power_provided": e.power_provided,
                    "power_drained": e.power_drained, "resource_capacity": e.resource_capacity,
                    "harvesters": e.harvester_count},
        "military": {"kills_cost": m.kills_cost, "deaths_cost": m.deaths_cost, "assets_value": m.assets_value,
                     "army_value": m.army_value, "units_killed": m.units_killed, "units_lost": m.units_lost,
                     "buildings_killed": m.buildings_killed, "buildings_lost": m.buildings_lost},
        "units": [unit(u) for u in obs.units],
        "buildings": [building(b) for b in obs.buildings],
        "production": [{"queue": p.queue_type, "item": p.item, "progress": p.progress,
                        "remaining_ticks": p.remaining_ticks, "paused": p.paused} for p in obs.production],
        "available_production": list(obs.available_production),
        "visible_enemies": [unit(u) for u in obs.visible_enemies],
        "visible_enemy_buildings": [building(b) for b in obs.visible_enemy_buildings],
        "command_results": [{"batch": r.batch, "index": r.index, "actor_id": r.actor_id, "status": r.status,
                             "reason": r.reason, "tick": r.tick, "detail": r.detail} for r in obs.command_results],
    }


def command_pb(c: dict) -> pb.Command:
    return pb.Command(action=pb.ActionType.Value(c["action"]), actor_id=c.get("actor_id") or 0,
                      target_actor_id=c.get("target_actor_id") or 0, target_x=c.get("target_x") or 0,
                      target_y=c.get("target_y") or 0, item_type=c.get("item_type") or "",
                      queued=bool(c.get("queued")), index=c.get("index") or 0, count=c.get("count") or 0)


class _Stream:
    """One seat's bidirectional GameSession stream."""

    def __init__(self, stub, player: str, on_obs) -> None:
        self.outbox: queue.Queue = queue.Queue()
        self.player = player
        self.on_obs = on_obs
        self.call = stub.GameSession(self._requests(), metadata=(("rl-player", player),))
        self.thread = threading.Thread(target=self._read, name=f"rl-{player}", daemon=True)
        self.thread.start()

    def _requests(self):
        while True:
            item = self.outbox.get()
            if item is None:
                return
            yield item

    def _read(self) -> None:
        try:
            for obs in self.call:
                self.on_obs(self.player, obs)
        except grpc.RpcError:
            pass

    def close(self) -> None:
        self.outbox.put(None)
        try:
            self.call.cancel()
        except Exception:  # noqa: BLE001
            pass


# Virtual time runs the engine flat out, and many engines share the cores: a large gen0, tiered
# PGO without the precompiled (ReadyToRun) code, and no spin-waiting in the thread pool used ~12 %
# less CPU per game than the defaults (workstation GC: server GC spins on every core).
LOCKSTEP_DOTNET = {"DOTNET_gcServer": "0", "DOTNET_GCgen0size": "0x4000000", "DOTNET_TieredPGO": "1",
                   "DOTNET_ReadyToRun": "0", "DOTNET_ThreadPool_UnfairSemaphoreSpinLimit": "0"}


# Every engine is started from this one long-lived thread: Linux sends the parent-death signal when
# the *thread* that forked the child exits, and requests and warm-ups run on short-lived threads.
_SPAWNER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine-spawner")


def _popen(*args, **kwargs) -> subprocess.Popen:
    return _SPAWNER.submit(subprocess.Popen, *args, **kwargs).result()


def _die_with_parent() -> None:
    """Popen preexec_fn: the engine gets SIGKILL when the Python process that started it dies."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass
    os.setsid()


def _engine_env(port: int, view_interval: int, view_phase: int, lockstep: bool, lobby: dict | None = None) -> dict:
    env = dict(os.environ, DOTNET_ROOT=str(DOTNET_ROOT), DOTNET_ROLL_FORWARD="LatestMajor",
               DOTNET_CLI_TELEMETRY_OPTOUT="1", RL_GRPC_PORT=str(port), RL_OBS_INTERVAL=str(view_interval),
               RL_OBS_PHASE=str(view_phase), RL_LOCKSTEP="1" if lockstep else "0")
    if lockstep:
        env = {**LOCKSTEP_DOTNET, **env}
    if lobby:
        env["OPENRA_LOBBY_OPTIONS"] = ";".join(f"{k}={v}" for k, v in lobby.items())
    env["PATH"] = f"{DOTNET_ROOT}:{env.get('PATH', '')}"
    return env


def _wait_port(port: int, proc: subprocess.Popen, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"engine exited with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("engine did not open its port")


class EngineHost:
    """One engine process in multi-session mode, hosting many games (virtual time only): the
    map data is loaded once, a new game takes ~0.5 s, and a game's ticks run without the local
    server and its network round trip per tick."""

    order_latency_ticks = LOCKSTEP_ORDER_LATENCY_TICKS

    def __init__(self, log_dir: Path, *, view_interval: int = VIEW_INTERVAL) -> None:
        self.view_interval = view_interval
        self.view_phase = VIEW_PHASE % view_interval
        self.dir = Path(log_dir).resolve()
        self.port = free_port()
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stub = None
        self.pool: HostPool | None = None  # a HostPool lane: one game at a time
        self.busy = False
        self.games = 0

    def _ensure(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None and self.stub is not None:
                return self.stub
            support = self.dir / "support"
            support.mkdir(parents=True, exist_ok=True)
            content = support / "Content"
            if not content.exists():
                content.symlink_to(CONTENT_DIR)
            env = _engine_env(self.port, self.view_interval, self.view_phase, True)
            cmd = [str(DOTNET_ROOT / "dotnet"), str(ENGINE_DIR / "bin" / "OpenRA.dll"), f"Engine.EngineDir={ENGINE_DIR}",
                   f"Engine.SupportDir={support}", "Game.Mod=ra", "Game.Platform=Null", "Sound.Mute=True",
                   f"Launch.MultiSession={self.port}"]
            self.log = open(self.dir / "engine-host.log", "ab")
            self.proc = _popen(cmd, cwd=ENGINE_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                               preexec_fn=_die_with_parent)
            _wait_port(self.port, self.proc)
            opts = [("grpc.max_receive_message_length", 64 << 20)]
            self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}", options=opts)
            self.stub = pb_grpc.RLBridgeStub(self.channel)
            return self.stub

    def create(self, map_file: str, bots: str, seed: int, lobby: dict) -> str:
        stub = self._ensure()
        options = ";".join(f"{k}={v}" for k, v in lobby.items())
        return stub.CreateSession(pb.CreateSessionRequest(map_name=map_file, bots=bots, seed=seed,
                                                          lobby_options=options), timeout=120).session_id

    def step(self, request: pb.StepRequest):
        return self._ensure().Step(request, timeout=600)

    def destroy(self, session_id: str) -> None:
        try:
            if self.stub is not None:
                self.stub.DestroySession(pb.DestroySessionRequest(session_id=session_id), timeout=30)
        except grpc.RpcError:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    self.proc.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            self.proc = None
            self.stub = None


class HostPool:
    """Engine processes for virtual time, one game at a time each ("lanes").

    Sessions of one engine process must not run at the same time: with OpenRA's AI playing,
    concurrent sessions slow each other's economies (measured: half of six parallel sessions
    harvested their first 1000 ore ~1500 ticks late). One after the other they are fine, so
    each lane plays its games in turn, without reloading the game data (~5 s) each time,
    and is restarted every ``recycle`` games. A lane is loaded as soon as the pool is made."""

    def __init__(self, log_dir: Path, size: int = 1, recycle: int = 20) -> None:
        self.dir = Path(log_dir)
        self.recycle = recycle
        self.cond = threading.Condition()
        self.lanes: list[EngineHost] = []
        self.n = 0
        self.closed = False
        for _ in range(max(1, size)):
            self.lanes.append(self._new())

    def _new(self) -> EngineHost:
        self.n += 1
        host = EngineHost(self.dir / f"lane-{self.n}")
        host.pool = self
        threading.Thread(target=self._warm, args=(host,), name="engine-warm", daemon=True).start()
        return host

    @staticmethod
    def _warm(host: EngineHost) -> None:
        try:
            host._ensure()
        except Exception:  # noqa: BLE001  (the game that takes it will see the error)
            pass

    def take(self) -> EngineHost:
        with self.cond:
            while True:
                for i, host in enumerate(self.lanes):
                    if not host.busy:
                        if host.games >= self.recycle or (host.proc is not None and not host.alive()):
                            threading.Thread(target=host.stop, daemon=True).start()
                            host = self.lanes[i] = self._new()
                        host.busy = True
                        host.games += 1
                        return host
                self.cond.wait()

    def release(self, host: EngineHost) -> None:
        with self.cond:
            host.busy = False
            self.cond.notify_all()

    def stop(self) -> None:
        with self.cond:
            self.closed = True
            lanes = list(self.lanes)
        for h in lanes:
            h.stop()


class OpenRAEngine:
    order_latency_ticks = ORDER_LATENCY_TICKS

    def __init__(self, session_id: str, map_id: str, seat_kinds: list[str], *, lobby: dict | None = None,
                 view_interval: int = VIEW_INTERVAL, log_dir: Path | None = None, lockstep: bool = False,
                 host: EngineHost | None = None, seed: int = 0) -> None:
        self.session_id = session_id
        self.map_file = MAP_FILES.get(map_id, map_id)
        self.seat_kinds = seat_kinds
        self.lobby = {**DEFAULT_LOBBY, **(lobby or {})}
        self.view_interval = view_interval
        self.view_phase = VIEW_PHASE % view_interval
        self.run_dir = Path(log_dir or RUNS_DIR / session_id).resolve()
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.streams: dict[int, _Stream] = {}
        self.lock = threading.Lock()
        self.last_tick: int | None = None
        self.last_time = 0.0
        self.stopping = False
        self.on_view = None
        self.on_exit = None
        self.lockstep = lockstep or host is not None
        self.host = host
        self.seed = seed
        self.host_session: str | None = None
        if self.lockstep:
            self.order_latency_ticks = LOCKSTEP_ORDER_LATENCY_TICKS
        if host is not None:
            self.view_interval, self.view_phase = host.view_interval, host.view_phase
        self.stub = None
        self.outgoing: list = []  # lockstep: PlayerActions for the next step
        self.tick = 0  # lockstep: the world's tick after the last step
        self.step_wall = 0.0  # lockstep: wall seconds spent in steps (engine + transfer)
        self.engine_us = 0  # lockstep: microseconds the engine itself spent ticking
        self.steps = 0
        self.step_lock = threading.Lock()  # one step at a time; stop() waits for the last one

    # -- lifecycle ----------------------------------------------------------------------

    def _bots(self) -> str:
        bots = []
        for i, kind in enumerate(self.seat_kinds):
            bots.append(f"Multi{i}:{'rl-agent' if kind == 'agent' else kind.split(':', 1)[1]}")
        return ",".join(bots)

    def start(self, on_view, on_exit) -> None:
        self.on_view, self.on_exit = on_view, on_exit
        if self.host is not None:
            self.host_session = self.host.create(self.map_file, self._bots(), self.seed, self.lobby)
            return
        support = self.run_dir / "support"
        support.mkdir(parents=True, exist_ok=True)
        content = support / "Content"
        if not content.exists():
            content.symlink_to(CONTENT_DIR)
        env = _engine_env(self.port, self.view_interval, self.view_phase, self.lockstep, self.lobby)
        cmd = [str(DOTNET_ROOT / "dotnet"), str(ENGINE_DIR / "bin" / "OpenRA.dll"), f"Engine.EngineDir={ENGINE_DIR}",
               f"Engine.SupportDir={support}", "Game.Mod=ra", "Game.Platform=Null", f"Launch.Map={self.map_file}",
               f"Launch.Bots={self._bots()}", "Sound.Mute=True"]
        self.log = open(self.run_dir / "engine.log", "wb")
        self.proc = _popen(cmd, cwd=ENGINE_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                           preexec_fn=_die_with_parent)
        if not self.lockstep:
            threading.Thread(target=self._connect, name=f"connect-{self.session_id[:8]}", daemon=True).start()
        threading.Thread(target=self._watch, name=f"watch-{self.session_id[:8]}", daemon=True).start()

    def _connect(self) -> None:
        deadline = time.monotonic() + 120
        while not self.stopping and time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        if self.stopping:
            return
        self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}")
        stub = pb_grpc.RLBridgeStub(self.channel)
        for i, kind in enumerate(self.seat_kinds):
            if kind == "agent":
                self.streams[i] = _Stream(stub, f"Multi{i}", self._on_obs)

    # -- virtual time -------------------------------------------------------------------------

    def _lockstep_stub(self):
        if self.stub is None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"engine exited with code {self.proc.returncode}")
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                        break
                except OSError:
                    time.sleep(0.05)
            opts = [("grpc.max_receive_message_length", 64 << 20)]
            self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}", options=opts)
            self.stub = pb_grpc.RLBridgeStub(self.channel)
        return self.stub

    def step(self, until_tick: int) -> list[tuple[int, dict]]:
        """Virtual time: run the world up to ``until_tick`` (with the actions queued since the
        last step) and return the views those ticks made, as (seat position, view)."""
        with self.step_lock:
            if self.stopping:
                raise RuntimeError("engine stopped")
            with self.lock:
                actions, self.outgoing = self.outgoing, []
            t0 = time.perf_counter()
            if self.host is not None:
                reply = self.host.step(pb.StepRequest(until_tick=until_tick, actions=actions,
                                                      session_id=self.host_session))
            else:
                reply = self._lockstep_stub().Step(pb.StepRequest(until_tick=until_tick, actions=actions), timeout=600)
        self.step_wall += time.perf_counter() - t0
        self.engine_us += reply.elapsed_us
        self.steps += 1
        views = [(int(o.player[5:]), view_from_pb(o.observation)) for o in reply.observations]
        with self.lock:
            self.tick = reply.tick
            for _, v in views:
                self.last_tick = v["tick"]
        return views

    def _watch(self) -> None:
        code = self.proc.wait()
        if not self.stopping and self.on_exit:
            self.on_exit(f"engine exited with code {code}")

    def _on_obs(self, player: str, obs) -> None:
        view = view_from_pb(obs)
        with self.lock:
            if self.last_tick is None or view["tick"] >= self.last_tick:
                self.last_tick, self.last_time = view["tick"], time.monotonic()
        if self.on_view:
            self.on_view(int(player[5:]), view)

    def tick_now(self) -> int:
        with self.lock:
            if self.lockstep:
                return self.tick
            if self.last_tick is None:
                return 0
            elapsed = int((time.monotonic() - self.last_time) * 25)
            return self.last_tick + max(0, min(elapsed, self.view_interval))

    def send(self, position: int, batch: int, apply_at_tick: int, commands: list[dict]) -> None:
        if self.lockstep:
            action = pb.AgentAction(commands=[command_pb(c) for c in commands], batch=batch,
                                    apply_at_tick=max(0, apply_at_tick))
            with self.lock:
                self.outgoing.append(pb.PlayerAction(player=f"Multi{position}", action=action))
            return
        stream = self.streams.get(position)
        if stream is None:
            return
        stream.outbox.put(pb.AgentAction(commands=[command_pb(c) for c in commands], batch=batch,
                                         apply_at_tick=max(0, apply_at_tick)))

    def surrender(self, position: int) -> None:
        self.send(position, 0, 0, [{"action": "SURRENDER"}])

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        if self.host is not None:
            got = self.step_lock.acquire(timeout=60)  # a prefetch may still be running
            try:
                if self.host_session:
                    self.host.destroy(self.host_session)
            finally:
                if got:
                    self.step_lock.release()
            if self.host.pool is not None:
                self.host.pool.release(self.host)
            return
        for s in self.streams.values():
            s.close()
        if self.proc and self.proc.poll() is None:
            if self.lockstep:
                if self.outgoing:  # a surrender: let the engine run it (the game is over for us anyway)
                    try:
                        self.step(self.tick + 8)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                time.sleep(1.0)  # let a surrender / the last views through
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            self.log.close()
        except Exception:  # noqa: BLE001
            pass

    def cleanup(self) -> None:
        if self.host is not None:
            return
        shutil.rmtree(self.run_dir / "support", ignore_errors=True)
