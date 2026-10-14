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

import base64
import concurrent.futures
import ctypes
import fcntl
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import struct
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
# The runtime of the stepped engines (virtual time): .NET 10 when sim/openra-fork/setup.sh installed it
# (the same games step for step as on .NET 8, ~10 % faster ticks), else the engine's own .NET 8;
# FF_LOCKSTEP_DOTNET_ROOT picks another. Real-time engines (the platform's setup) keep .NET 8.
_DOTNET10 = SIM_HOME / "dotnet10"
LOCKSTEP_DOTNET_ROOT = Path(os.environ.get("FF_LOCKSTEP_DOTNET_ROOT")
                            or (_DOTNET10 if (_DOTNET10 / "shared" / "Microsoft.AspNetCore.App").exists()
                                and "FF_DOTNET_ROOT" not in os.environ else DOTNET_ROOT))
CONTENT_DIR = Path(os.environ.get("FF_RA_CONTENT", SIM_HOME / "src" / "Support" / "Content"))
RUNS_DIR = Path(os.environ.get("FF_SIM_RUNS", SIM_HOME / "runs"))
# Persistent lanes (HostPool(persist=True)): engines kept running between runs, so a run needn't
# load the game data and compile the engine's code again; one exits after FF_LANE_IDLE_EXIT seconds
# without a run.
LANES_DIR = Path(os.environ.get("FF_LANES_DIR", SIM_HOME / "lanes"))
LANE_IDLE_EXIT_SECONDS = int(os.environ.get("FF_LANE_IDLE_EXIT", "1800"))

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
# FF_ENGINE_TRACE=<dir>: each virtual-time game on an EngineHost writes <dir>/<session>.trace, every
# engine call it made (the session it created, each step's actions) and a digest of each step's
# views; scripts/ffbench.py replays it on any engine build to time the engine alone and to check
# that the build plays the game exactly the same.
TRACE_DIR = os.environ.get("FF_ENGINE_TRACE")


def views_digest(observations) -> str:
    """A digest of a step's views, without the session's random episode id."""
    h = hashlib.sha1()
    for o in observations:
        episode, o.observation.episode_id = o.observation.episode_id, ""
        h.update(o.SerializeToString(deterministic=True))
        o.observation.episode_id = episode
    return h.hexdigest()


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


def _engine_env(port: int, view_interval: int, view_phase: int, lockstep: bool, lobby: dict | None = None,
                dotnet_root: Path | None = None) -> dict:
    dotnet_root = dotnet_root or DOTNET_ROOT
    env = dict(os.environ, DOTNET_ROOT=str(dotnet_root), DOTNET_ROLL_FORWARD="LatestMajor",
               DOTNET_CLI_TELEMETRY_OPTOUT="1", RL_GRPC_PORT=str(port), RL_OBS_INTERVAL=str(view_interval),
               RL_OBS_PHASE=str(view_phase), RL_LOCKSTEP="1" if lockstep else "0")
    if lockstep:
        env = {**LOCKSTEP_DOTNET, **env}
    if lobby:
        env["OPENRA_LOBBY_OPTIONS"] = ";".join(f"{k}={v}" for k, v in lobby.items())
    env["PATH"] = f"{dotnet_root}:{env.get('PATH', '')}"
    return env


def _detach() -> None:
    """Popen preexec_fn for a persistent lane's engine: its own session, outliving this process."""
    os.setsid()


def engine_build_id(engine_dir: Path) -> str:
    """The engine build in a folder: where it is and its assemblies' sizes and times."""
    h = hashlib.sha1(str(Path(engine_dir).resolve()).encode())
    for dll in sorted((Path(engine_dir) / "bin").glob("OpenRA*.dll")):
        st = dll.stat()
        h.update(f"{dll.name}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()[:16]


def _engine_alive(pid: int, port: int) -> bool:
    """Is ``pid`` still the engine that serves ``port`` (and not a zombie or a reused pid)?"""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, IndexError):
        return False
    return state != "Z" and f"Launch.MultiSession={port}".encode() in cmdline


class _Detached:
    """A persistent lane's engine started by an earlier run: as much of Popen as EngineHost uses."""

    def __init__(self, pid: int, port: int) -> None:
        self.pid, self.port = pid, port
        self.returncode = None
        self.checked = float("-inf")

    def poll(self):
        # (/proc is read at most once a second: EngineHost asks before every call)
        now = time.monotonic()
        if self.returncode is None and now - self.checked >= 1.0:
            self.checked = now
            if not _engine_alive(self.pid, self.port):
                self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("engine", timeout)
            time.sleep(0.05)
        return self.returncode


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

    # Calls go over the engine's plain TCP lockstep protocol (RL_RAW_PORT, ~1 ms of CPU less per
    # call on each side than gRPC); FF_ENGINE_GRPC=1 uses the gRPC service instead.
    RAW = os.environ.get("FF_ENGINE_GRPC") != "1"
    STEP, CREATE, DESTROY = 1, 2, 3

    def __init__(self, log_dir: Path, *, view_interval: int = VIEW_INTERVAL, engine_dir: Path | None = None,
                 env: dict | None = None, persist: bool = False) -> None:
        self.engine_dir = Path(engine_dir or ENGINE_DIR)
        self.extra_env = dict(env or {})
        self.dotnet_root = Path(self.extra_env.pop("FF_DOTNET_ROOT", None) or LOCKSTEP_DOTNET_ROOT)
        self.persist = persist  # a persistent lane: kept running after this run, and found again by the next
        self.view_interval = view_interval
        self.view_phase = VIEW_PHASE % view_interval
        self.dir = Path(log_dir).resolve()
        self.port = free_port()
        self.raw_port = free_port()
        self.sock: socket.socket | None = None
        self.sock_lock = threading.Lock()
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stub = None
        self.pool: HostPool | None = None  # a HostPool lane: one game at a time
        self.busy = False
        self.games = 0

    def _env(self) -> dict:
        env = {**_engine_env(self.port, self.view_interval, self.view_phase, True, dotnet_root=self.dotnet_root),
               **self.extra_env}
        if self.RAW:
            env["RL_RAW_PORT"] = str(self.raw_port)
        return env

    def _env_key(self) -> str:
        """What a persistent lane's engine must have been started with to be used again."""
        env = self._env()
        keep = sorted((k, v) for k, v in env.items() if k.startswith(("RL_", "DOTNET_", "OPENRA_"))
                      and k not in ("RL_GRPC_PORT", "RL_RAW_PORT", "RL_IDLE_EXIT_SECONDS"))
        return hashlib.sha1(json.dumps([engine_build_id(self.engine_dir), keep]).encode()).hexdigest()[:16]

    def _attach(self) -> bool:
        """A persistent lane: use the engine an earlier run left in this lane, if it is the same build."""
        try:
            info = json.loads((self.dir / "host.json").read_text())
        except (OSError, ValueError):
            return False
        if not _engine_alive(info.get("pid", 0), info.get("port", 0)):
            return False
        if info.get("key") != self._env_key() or not self.RAW:
            self._kill(info["pid"])
            return False
        try:
            sock = socket.create_connection(("127.0.0.1", info["raw_port"]), timeout=5)
        except OSError:
            self._kill(info["pid"])
            return False
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.port, self.raw_port = info["port"], info["raw_port"]
        self.games += info.get("games", 0)  # (plus any this run counted already)
        self.proc = _Detached(info["pid"], info["port"])
        self.sock = self.stub = sock
        return True

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def save(self) -> None:
        """A persistent lane: write down its engine for the next run."""
        if self.persist and self.proc is not None:
            info = {"pid": self.proc.pid, "port": self.port, "raw_port": self.raw_port, "key": self._env_key(),
                    "games": self.games, "engine_dir": str(self.engine_dir)}
            tmp = self.dir / "host.json.tmp"
            tmp.write_text(json.dumps(info))
            os.replace(tmp, self.dir / "host.json")

    def _ensure(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None and self.stub is not None:
                return self.stub
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.persist and self.proc is None and self._attach():
                return self.stub
            support = self.dir / "support"
            support.mkdir(parents=True, exist_ok=True)
            content = support / "Content"
            if not content.exists():
                content.symlink_to(CONTENT_DIR)
            env = self._env()
            if self.persist:
                env["RL_IDLE_EXIT_SECONDS"] = str(LANE_IDLE_EXIT_SECONDS)
            engine_dir = self.engine_dir
            cmd = [str(self.dotnet_root / "dotnet"), str(engine_dir / "bin" / "OpenRA.dll"), f"Engine.EngineDir={engine_dir}",
                   f"Engine.SupportDir={support}", "Game.Mod=ra", "Game.Platform=Null", "Sound.Mute=True",
                   f"Launch.MultiSession={self.port}"]
            self.log = open(self.dir / "engine-host.log", "ab")
            self.proc = _popen(cmd, cwd=engine_dir, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                               preexec_fn=_detach if self.persist else _die_with_parent)
            if self.RAW:
                _wait_port(self.raw_port, self.proc)
                self.sock = socket.create_connection(("127.0.0.1", self.raw_port))
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.stub = self.sock
                self.save()
                return self.stub
            _wait_port(self.port, self.proc)
            opts = [("grpc.max_receive_message_length", 64 << 20)]
            self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}", options=opts)
            self.stub = pb_grpc.RLBridgeStub(self.channel)
            return self.stub

    def _raw(self, kind: int, request, reply_cls):
        """One call over the plain TCP protocol: length, type, protobuf -> length, status, protobuf."""
        if self.sock is None:  # (once connected, a dead engine shows as a closed connection)
            self._ensure()
        body = request.SerializeToString()
        with self.sock_lock:
            sock = self.sock
            sock.sendall(struct.pack("<iB", len(body), kind) + body)
            head = self._recv(sock, 5)
            length, status = struct.unpack("<iB", head)
            data = self._recv(sock, length)
        if status != 0:
            raise RuntimeError("engine: " + data.decode("utf-8", "replace")[:2000])
        return reply_cls.FromString(data)

    @staticmethod
    def _recv(sock: socket.socket, n: int) -> bytes:
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            k = sock.recv_into(view[got:], n - got)
            if k == 0:
                raise ConnectionError("engine closed the connection")
            got += k
        return bytes(buf)

    def create(self, map_file: str, bots: str, seed: int, lobby: dict) -> str:
        options = ";".join(f"{k}={v}" for k, v in lobby.items())
        request = pb.CreateSessionRequest(map_name=map_file, bots=bots, seed=seed, lobby_options=options)
        if self.RAW:
            return self._raw(self.CREATE, request, pb.CreateSessionResponse).session_id
        return self._ensure().CreateSession(request, timeout=120).session_id

    def step(self, request: pb.StepRequest):
        if self.RAW:
            return self._raw(self.STEP, request, pb.StepReply)
        return self._ensure().Step(request, timeout=600)

    def destroy(self, session_id: str) -> None:
        if self.stub is None:
            return
        try:
            request = pb.DestroySessionRequest(session_id=session_id)
            if self.RAW:
                self._raw(self.DESTROY, request, pb.DestroySessionResponse)
            else:
                self.stub.DestroySession(request, timeout=30)
        except (grpc.RpcError, OSError, RuntimeError):
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def cpu_seconds(self) -> float:
        """CPU time the engine process has used so far (all its threads)."""
        try:
            fields = Path(f"/proc/{self.proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, AttributeError, IndexError, ValueError):
            return 0.0

    def stop(self, kill: bool | None = None) -> None:
        """Stop the engine; a persistent lane's only lets go of it (unless ``kill``)."""
        if kill is None:
            kill = not self.persist
        with self.lock:
            if kill and self.persist:
                try:
                    (self.dir / "host.json").unlink()
                except OSError:
                    pass
            if kill and self.proc and self.proc.poll() is None:
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
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


class HostPool:
    """Engine processes for virtual time, one game at a time each ("lanes").

    Sessions of one engine process must not run at the same time: with OpenRA's AI playing,
    concurrent sessions slow each other's economies (measured: half of six parallel sessions
    harvested their first 1000 ore ~1500 ticks late). One after the other they are fine, so
    each lane plays its games in turn, without reloading the game data (~5 s) each time,
    and is restarted every ``recycle`` games. A lane is loaded as soon as the pool is made.

    ``persist``: the lanes live in ``LANES_DIR`` and their engines keep running after the run (each
    exits after ``LANE_IDLE_EXIT_SECONDS`` unused), already loaded and with their code compiled, for
    the next run to take over; a lane is one run's at a time (a lock file), and an engine of another
    build or configuration is replaced."""

    def __init__(self, log_dir: Path, size: int = 1, recycle: int = 100, persist: bool = False) -> None:
        self.dir = Path(log_dir)
        self.recycle = recycle
        self.persist = persist
        self.cond = threading.Condition()
        self.lanes: list[EngineHost] = []
        self.claims: list[int] = []  # persistent lanes: the lock files held
        self.n = 0
        self.closed = False
        for _ in range(max(1, size)):
            self.lanes.append(self._new())

    def _claim(self) -> Path:
        """A persistent lane no other run is using (its lock file, held until this pool stops)."""
        LANES_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(1, 65):
            lane = LANES_DIR / f"lane-{i}"
            lane.mkdir(exist_ok=True)
            fd = os.open(lane / "lock", os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            self.claims.append(fd)
            return lane
        raise RuntimeError(f"no free lane in {LANES_DIR}")

    def _new(self, lane: Path | None = None) -> EngineHost:
        self.n += 1
        if self.persist:
            host = EngineHost(lane or self._claim(), persist=True)
        else:
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
                            if self.persist:
                                host.stop(kill=True)
                            else:
                                threading.Thread(target=host.stop, daemon=True).start()
                            host = self.lanes[i] = self._new(host.dir if self.persist else None)
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
            h.save()
            h.stop()
        for fd in self.claims:
            os.close(fd)  # (the lock goes with it)
        self.claims = []


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
        self.trace = None  # FF_ENGINE_TRACE

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
            if TRACE_DIR:
                Path(TRACE_DIR).mkdir(parents=True, exist_ok=True)
                self.trace = open(Path(TRACE_DIR) / f"{self.session_id}.trace", "w")
                self.trace.write(json.dumps({"map_file": self.map_file, "bots": self._bots(), "seed": self.seed,
                                             "lobby": self.lobby}) + "\n")
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
            if self.trace is not None:
                request = pb.StepRequest(until_tick=until_tick, actions=actions)
                self.trace.write(json.dumps([base64.b64encode(request.SerializeToString()).decode(), reply.tick,
                                             views_digest(reply.observations)]) + "\n")
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
            if self.trace is not None:
                self.trace.close()
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
