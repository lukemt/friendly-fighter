"""The real game: the platform's OpenRA fork (yxc20089/OpenRA @ 9b271c1 plus
sim/openra-fork/ff-sim.patch), headless, one process per match.

Each agent seat is an ``rl-agent`` bot whose ExternalBotBridge streams a view every
``RL_OBS_INTERVAL`` ticks over gRPC (one stream per seat, picked by the ``rl-player``
header) and applies each AgentAction at its ``apply_at_tick``. An ``ai:<type>`` seat is
one of OpenRA's own bots (``normal``, ``rush``, ``turtle``, ``naval``).
"""

from __future__ import annotations

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
# Ticks between the bridge issuing an order and the engine running it (OpenRA's order
# latency in a local game): 4, measured with 1-tick views (an order issued at tick 30 shows
# its Move/Transform activity at 34). The GameAPI subtracts it so orders run 12 ticks
# after they arrive: on the platform an idle unit ordered to move already moves in the
# view 12 ticks after the batch arrived (docs/SIMULATOR.md, "Timing").
ORDER_LATENCY_TICKS = int(os.environ.get("FF_ENGINE_ORDER_LATENCY", "4"))
DEFAULT_LOBBY = {"startingcash": "5000", "startingunits": "mcvonly", "explored": "False", "crates": "True",
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


class OpenRAEngine:
    order_latency_ticks = ORDER_LATENCY_TICKS

    def __init__(self, session_id: str, map_id: str, seat_kinds: list[str], *, lobby: dict | None = None,
                 view_interval: int = VIEW_INTERVAL, log_dir: Path | None = None) -> None:
        self.session_id = session_id
        self.map_file = MAP_FILES.get(map_id, map_id)
        self.seat_kinds = seat_kinds
        self.lobby = {**DEFAULT_LOBBY, **(lobby or {})}
        self.view_interval = view_interval
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

    # -- lifecycle ----------------------------------------------------------------------

    def _bots(self) -> str:
        bots = []
        for i, kind in enumerate(self.seat_kinds):
            bots.append(f"Multi{i}:{'rl-agent' if kind == 'agent' else kind.split(':', 1)[1]}")
        return ",".join(bots)

    def start(self, on_view, on_exit) -> None:
        self.on_view, self.on_exit = on_view, on_exit
        support = self.run_dir / "support"
        support.mkdir(parents=True, exist_ok=True)
        content = support / "Content"
        if not content.exists():
            content.symlink_to(CONTENT_DIR)
        env = dict(os.environ, DOTNET_ROOT=str(DOTNET_ROOT), DOTNET_ROLL_FORWARD="LatestMajor",
                   DOTNET_CLI_TELEMETRY_OPTOUT="1", RL_GRPC_PORT=str(self.port),
                   RL_OBS_INTERVAL=str(self.view_interval),
                   OPENRA_LOBBY_OPTIONS=";".join(f"{k}={v}" for k, v in self.lobby.items()))
        env["PATH"] = f"{DOTNET_ROOT}:{env.get('PATH', '')}"
        cmd = [str(DOTNET_ROOT / "dotnet"), str(ENGINE_DIR / "bin" / "OpenRA.dll"), f"Engine.EngineDir={ENGINE_DIR}",
               f"Engine.SupportDir={support}", "Game.Mod=ra", "Game.Platform=Null", f"Launch.Map={self.map_file}",
               f"Launch.Bots={self._bots()}", "Sound.Mute=True"]
        self.log = open(self.run_dir / "engine.log", "wb")
        self.proc = subprocess.Popen(cmd, cwd=ENGINE_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
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
            if self.last_tick is None:
                return 0
            elapsed = int((time.monotonic() - self.last_time) * 25)
            return self.last_tick + max(0, min(elapsed, self.view_interval))

    def send(self, position: int, batch: int, apply_at_tick: int, commands: list[dict]) -> None:
        stream = self.streams.get(position)
        if stream is None:
            return
        stream.outbox.put(pb.AgentAction(commands=[command_pb(c) for c in commands], batch=batch,
                                         apply_at_tick=max(0, apply_at_tick)))

    def surrender(self, position: int) -> None:
        self.send(position, 0, 0, [{"action": "SURRENDER"}])

    def stop(self) -> None:
        self.stopping = True
        for s in self.streams.values():
            s.close()
        if self.proc and self.proc.poll() is None:
            time.sleep(1.0)  # let a surrender / the last views through
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
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
        shutil.rmtree(self.run_dir / "support", ignore_errors=True)
