"""Probe seat: scripted experiments against the real Red Alert GameAPI.

Turned on by ``remote/probe.json`` (``scripts/ff_probe.py on --seat 0``)::

    {"enabled": true, "seat": 0, "resign_at_end": true}

On that seat the agent runs the experiments below with its own MCP calls
(raw JSON-RPC, so errors come back as data and timing is under our control),
and lets the scripted bot play in between. Each experiment's requests,
answers and timings go to ``recordings/<session>/probe-seat<N>.jsonl``; a
one-line summary per step goes to the agent log. See docs/BACKEND_ANALYSIS.md
section 4 for why each one is here.

Phase A runs at the very first view (only the MCV exists), for about a
minute. Phase C waits for power, barracks, three infantry and tick 3200 (at
most until tick 9000). Then the probe resigns (unless
``resign_at_end`` is false), which records the end-of-game answers too.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import traceback
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from altruagent import WAIT
from altruagent import mcp_transport
from altruagent.console import print_line

from .redalert_bot import RedAlertBot

ROOT = Path(__file__).resolve().parent.parent
CONTROL = Path(os.environ.get("FF_REMOTE_DIR") or ROOT / "remote") / "probe.json"
INFANTRY = ("e1", "e2", "e3", "e4", "e6")
PHASE_C_LATEST_TICK = 9000  # 6 minutes: run phase C even if its base isn't there
POWER_DOWN_TYPES = ("mslo", "gap", "iron", "pdox", "tsla", "agun", "dome", "sam")
_ids = itertools.count(1)


def read_probe_control(path: Path | None = None) -> dict:
    try:
        return json.loads((path or CONTROL).read_text())
    except (OSError, ValueError):
        return {}


class ProbeError(Exception):
    pass


class Probe:
    def __init__(self, bot: RedAlertBot, context, *, resign_at_end: bool = True,
                 root: Path = ROOT / "recordings") -> None:
        self.bot = bot
        self.sid = context.session_id
        self.seat = context.seat_position
        self.resign_at_end = resign_at_end
        folder = root / "".join(c if c.isalnum() or c in "-_" else "_" for c in str(self.sid))
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"probe-seat{self.seat}.jsonl"
        self.http = httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0))
        self.steps: list[tuple[str, Callable[[dict], bool], Callable[[dict], dict]]] = self._plan()
        self.done: set[str] = set()
        self.finished = False
        self._log(f"[probe] on for seat {self.seat}: {len(self.steps)} steps -> {self.path}")

    # -- the runtime's hooks ---------------------------------------------------------

    def choose_action(self, state, context):
        raw = state.raw
        if self.finished:
            return self.bot.choose_action(state, context)
        for name, ready, run in self.steps:
            if name in self.done or not ready(raw):
                continue
            self.done.add(name)
            self._run(name, run, raw)
            return WAIT  # the runtime fetches a fresh view next
        if len(self.done) == len(self.steps):
            self.finished = True
            self._run("zz_end", self._end, raw)
            return WAIT
        return self.bot.choose_action(state, context)

    def on_action_result(self, result, context) -> None:
        self.bot.on_action_result(result, context)

    # -- plumbing ---------------------------------------------------------------------

    def _log(self, line: str) -> None:
        print_line(line)

    def _write(self, record: dict) -> None:
        try:
            with self.path.open("a") as handle:
                handle.write(json.dumps(record, default=repr) + "\n")
        except OSError:
            pass

    def _run(self, name: str, run: Callable[[dict], dict], raw: dict) -> None:
        started = time.time()
        try:
            result = run(raw)
            ok = True
        except Exception as exc:  # noqa: BLE001 - a failed experiment is data too
            result = {"exception": repr(exc), "traceback": traceback.format_exc()[-2000:]}
            ok = False
        record = {"step": name, "ok": ok, "t": round(started, 3), "took_s": round(time.time() - started, 2),
                  "tick": _tick(raw), "result": result}
        self._write(record)
        self._log(f"[probe] {name}: {'ok' if ok else 'FAILED'} {json.dumps(result.get('summary', result), default=repr)[:300]}")

    def rpc(self, method: str, params: dict, http: httpx.Client | None = None) -> dict:
        """One JSON-RPC call; returns {payload, dt, http, rpc_error} without raising on tool errors."""
        if mcp_transport.last_endpoint is None:
            raise ProbeError("no MCP endpoint seen yet")
        client, url = mcp_transport.last_endpoint
        token = client._current_access_token()
        body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
        sent = time.time()
        response = (http or self.http).post(url, json=body, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        dt = time.time() - sent
        try:
            envelope = mcp_transport._parse_rpc_body(response)
        except ValueError:
            envelope = {"unparsed": response.text[:2000]}
        result = envelope.get("result")
        payload: Any = None
        if isinstance(result, dict):
            payload = result.get("structuredContent")
            if payload is None and result.get("content"):
                text = result["content"][0].get("text") if isinstance(result["content"][0], dict) else None
                try:
                    payload = json.loads(text) if text else None
                except ValueError:
                    payload = text
            if method != "tools/call":
                payload = result
        answer = {"t": round(sent, 3), "dt": round(dt, 3), "http": response.status_code,
                  "payload": payload, "rpc_error": envelope.get("error")}
        mcp_transport.wire_log({"t": round(sent, 3), "dt": round(dt, 3), "probe": True, "method": method,
                                "session_id": self.sid, "params": params, "answer": payload,
                                "rpc_error": envelope.get("error"), "http": response.status_code})
        return answer

    def tool(self, name: str, http: httpx.Client | None = None, **arguments) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": {"session_id": self.sid, **arguments}}, http)

    def state(self) -> dict:
        payload = self.tool("get_game_state")["payload"]
        if not isinstance(payload, dict) or "error" in payload:
            raise ProbeError(f"get_game_state failed: {str(payload)[:300]}")
        return payload

    def play(self, orders: Any, sv: int, http: httpx.Client | None = None, **extra) -> dict:
        action = orders if isinstance(orders, dict) and "type" in orders else {"type": "orders", "orders": orders}
        return self.tool("play_action", http, state_version=sv, action=action, **extra)

    def settle(self, sv: Any, timeout: float = 6.0) -> dict:
        """Read views until a batch has its final verdict. ``sv`` is play_action's
        answer (its ``state_version`` is the tick the server stamped the batch
        with: the arrival tick, not the state_version we sent) or that tick."""
        if isinstance(sv, dict):
            payload = sv.get("payload", sv)
            sv = payload.get("state_version") if isinstance(payload, dict) else None
            if sv is None:
                return {"final": None, "not_accepted": True}
        seen = []
        end = time.time() + timeout
        while time.time() < end:
            st = self.state()
            lo = st.get("last_orders") or {}
            seen.append(st.get("state_version"))
            if lo.get("state_version") == sv and not lo.get("pending"):
                return {"final": lo, "seen_at": st.get("state_version"), "views": len(seen),
                        "problems": st.get("recent_order_problems")}
            time.sleep(0.05)
        return {"final": None, "timeout": True, "views": seen[-5:]}

    def gap(self) -> None:
        time.sleep(0.25)  # keep the 0.2 s batch spacing out of the measurements

    # -- the plan ---------------------------------------------------------------------

    def _plan(self):
        always = lambda raw: True  # noqa: E731
        return [
            ("a01_tools_list", always, self._tools_list),
            ("a02_legal_and_result", always, self._legal_and_result),
            ("a03_wait_for_update", always, self._wait_for_update),
            ("a04_latency_vs_delay", always, self._latency_series),
            ("a05_pacing_hold", always, self._pacing_hold),
            ("a06_concurrent", always, self._concurrent),
            ("a07_future_version", always, self._future_version),
            ("a08_order_matrix", always, self._order_matrix),
            ("a09_whole_batch_errors", always, self._batch_errors),
            ("c01_stale", self._phase_c, self._stale),
            ("c02_stance_guard_queue", self._phase_c, self._stance_guard_queue),
            ("c03_buildings_as_units", self._phase_c, self._buildings_as_units),
            ("c04_speed", self._phase_c, self._speed),
            ("c05_power_toggle", self._phase_c, self._power_toggle),
            ("c06_repair_primary_rally", self._phase_c, self._repair_primary_rally),
            ("c07_queue_and_cancel", self._phase_c, self._queue_and_cancel),
            ("c08_place_at", self._phase_c, self._place_at),
            ("c09_sell", self._phase_c, self._sell),
            ("c10_harvest_to", self._phase_c, self._harvest_to),
        ]

    def _phase_c(self, raw: dict) -> bool:
        obs = raw.get("observation") or {}
        types = [b.get("type") for b in obs.get("buildings") or []]
        inf = [u for u in obs.get("units") or [] if u.get("type") in INFANTRY and not u.get("wreck")]
        ready = ("fact" in types and ("powr" in types or "apwr" in types) and ("tent" in types or "barr" in types)
                 and len(inf) >= 3 and _tick(raw) >= 3200)
        return ready or _tick(raw) >= PHASE_C_LATEST_TICK  # try anyway; failures are logged

    # -- phase A: only the MCV ----------------------------------------------------------

    def _mcv(self, st: dict) -> int:
        ids = [u["id"] for u in (st.get("observation") or {}).get("units") or [] if u.get("type") == "mcv"]
        if not ids:
            raise ProbeError("no MCV")
        return ids[0]

    def _tools_list(self, raw):
        answer = self.rpc("tools/list", {})
        tools = ((answer.get("payload") or {}).get("tools")) or []
        return {"summary": {"tools": [t.get("name") for t in tools]}, "answer": answer}

    def _legal_and_result(self, raw):
        return {"get_legal_actions": self.tool("get_legal_actions"), "get_result": self.tool("get_result"),
                "summary": "recorded"}

    def _wait_for_update(self, raw):
        st = self.state()
        out = {}
        a = self.tool("wait_for_update", since_version=st["state_version"], since_is_current_actor=True,
                      since_phase=st.get("phase"))
        out["next"] = {"dt": a["dt"], "from": st["state_version"], "to": (a["payload"] or {}).get("state_version"),
                       "keys": sorted((a["payload"] or {}).keys()) if isinstance(a["payload"], dict) else None}
        b = self.tool("wait_for_update", since_version=st["state_version"] + 100000, since_is_current_actor=True,
                      since_phase=st.get("phase"), timeout_seconds=3)
        out["future_since"] = {"dt": b["dt"], "to": (b["payload"] or {}).get("state_version")
                               if isinstance(b["payload"], dict) else None,
                               "error": (b["payload"] or {}).get("error") if isinstance(b["payload"], dict) else None}
        out["summary"] = out.copy()
        out["raw_future"] = b
        return out

    def _latency_series(self, raw):
        rows = []
        for delay in (0.0, 0.5, 2.0):
            first = self.state()
            mcv = self._mcv(first)
            time.sleep(delay)
            now = self.state()
            sent = self.play([{"cmd": "stop", "units": [mcv]}], first["state_version"])
            verdict = self.settle(sent)
            final = verdict.get("final") or {}
            rows.append({"delay": delay, "sv": first["state_version"], "tick_at_send": now["state_version"],
                         "applied_at": final.get("applied_at_tick"), "seen_final_at": verdict.get("seen_at"),
                         "carried": final.get("carried"), "answer_dt": sent["dt"],
                         "answer": sent["payload"]})
            self.gap()
        summary = [(r["delay"], (r["applied_at"] or 0) - r["sv"], (r["applied_at"] or 0) - r["tick_at_send"])
                   for r in rows]
        return {"summary": {"delay, applied-sv, applied-tick_at_send": summary}, "rows": rows}

    def _pacing_hold(self, raw):
        st = self.state()
        mcv = self._mcv(st)
        a = self.play([{"cmd": "stop", "units": [mcv]}], st["state_version"])
        b = self.play([{"cmd": "stop", "units": [mcv]}], st["state_version"])
        self.gap()
        return {"summary": {"first_dt": a["dt"], "second_dt": b["dt"],
                            "second": _short(b["payload"])}, "first": a, "second": b}

    def _concurrent(self, raw):
        st = self.state()
        mcv = self._mcv(st)
        results: list[dict] = [{}, {}]
        clients = [httpx.Client(timeout=40.0), httpx.Client(timeout=40.0)]
        barrier = threading.Barrier(2)

        def send(i):
            barrier.wait()
            results[i] = self.play([{"cmd": "stop", "units": [mcv]}], st["state_version"], http=clients[i])

        threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for c in clients:
            c.close()
        self.gap()
        return {"summary": [_short(r.get("payload")) for r in results], "results": results}

    def _future_version(self, raw):
        st = self.state()
        mcv = self._mcv(st)
        a = self.play([{"cmd": "stop", "units": [mcv]}], st["state_version"] + 1000)
        b = self.play([{"cmd": "stop", "units": [mcv]}], -1)
        self.gap()
        return {"summary": {"future": _short(a["payload"]), "negative": _short(b["payload"])}, "future": a, "negative": b}

    def _order_matrix(self, raw):
        st = self.state()
        mcv = self._mcv(st)
        bounds = (st.get("map") or {}).get("bounds") or [1, 1, 90, 90]
        good = {"cmd": "stop", "units": [mcv]}
        cases = {
            "unknown_field": {"cmd": "stop", "units": [mcv], "foo": 1},
            "unknown_cmd": {"cmd": "dance", "units": [mcv]},
            "to_outside_bounds_inside_map": {"cmd": "move", "units": [mcv], "to": [bounds[0] - 1, bounds[1] - 1]},
            "to_outside_map": {"cmd": "move", "units": [mcv], "to": [999, 999]},
            "float_cell": {"cmd": "move", "units": [mcv], "to": [10.5, 10]},
            "duplicate_ids": {"cmd": "stop", "units": [mcv, mcv]},
            "fifty_one_units": {"cmd": "stop", "units": [mcv] + list(range(900000, 900050))},
            "unknown_unit": {"cmd": "stop", "units": [987654]},
            "string_id": {"cmd": "stop", "units": ["abc"]},
            "empty_units": {"cmd": "stop", "units": []},
            "train_count_11": {"cmd": "train", "item": "e1", "count": 11},
            "train_unavailable": {"cmd": "train", "item": "3tnk"},
            "build_unavailable": {"cmd": "build", "item": "weap"},
            "build_unknown_item": {"cmd": "build", "item": "deathstar"},
            "place_not_ready": {"cmd": "place", "item": "powr"},
            "cancel_nothing": {"cmd": "cancel", "item": "powr"},
            "attack_unknown": {"cmd": "attack", "units": [mcv], "target": 987654},
            "attack_self": {"cmd": "attack", "units": [mcv], "target": mcv},
            "attack_ground": {"cmd": "attack", "units": [mcv], "to": [bounds[0] + 5, bounds[1] + 5]},
            "attack_both": {"cmd": "attack", "units": [mcv], "target": mcv, "to": [5, 5]},
            "guard_self": {"cmd": "guard", "units": [mcv], "target": mcv},
            "stance_bad": {"cmd": "set_stance", "units": [mcv], "stance": "berserk"},
            "repair_unit": {"cmd": "repair", "building": mcv},
            "sell_unit": {"cmd": "sell", "building": mcv},
            "rally_unit": {"cmd": "rally", "building": mcv, "to": [10, 10]},
            "unload_nothing": {"cmd": "unload", "units": [mcv]},
            "enter_self": {"cmd": "enter_transport", "units": [mcv], "target": mcv},
            "harvest_mcv": {"cmd": "harvest", "units": [mcv]},
            "queued_flag": {"cmd": "move", "units": [mcv], "to": list(_center(st)), "queued": True},
        }
        rows = {}
        for name, bad in cases.items():
            cur = self.state()
            sent = self.play([bad, good], cur["state_version"])
            verdict = self.settle(sent) if isinstance(sent["payload"], dict) and sent["payload"].get("accepted") else {}
            final = (verdict.get("final") or {}).get("results") or []
            rows[name] = {"order": bad, "immediate": _rows(sent["payload"]), "final": _rows({"results": final}),
                          "error": (sent["payload"] or {}).get("error") if isinstance(sent["payload"], dict) else None,
                          "detail": (sent["payload"] or {}).get("detail") if isinstance(sent["payload"], dict) else None,
                          "problems": verdict.get("problems")}
            self.gap()
        summary = {k: (v["error"] or (v["immediate"][:1], v["final"][:1])) for k, v in rows.items()}
        return {"summary": summary, "rows": rows}

    def _batch_errors(self, raw):
        st = self.state()
        mcv = self._mcv(st)
        sv = st["state_version"]
        cases = {
            "empty_orders": [],
            "21_orders": [{"cmd": "stop", "units": [mcv]}] * 21,
            "250_fake_unit_commands": [{"cmd": "move", "units": list(range(800000 + 50 * i, 800050 + 50 * i)),
                                        "to": [10, 10]} for i in range(5)],
            "200_train_commands": [{"cmd": "train", "item": "e1", "count": 10}] * 20,
            "wrong_type": {"type": "move", "orders": [{"cmd": "stop", "units": [mcv]}]},
            "not_a_list": {"type": "orders", "orders": {"cmd": "stop", "units": [mcv]}},
        }
        rows = {}
        for name, orders in cases.items():
            rows[name] = self.play(orders, self.state()["state_version"])
            self.gap()
        rows["action_id"] = self.tool("play_action", state_version=self.state()["state_version"], action_id=0)
        self.gap()
        long = "x" * 700
        rows["long_reasoning"] = self.play([{"cmd": "stop", "units": [mcv]}], self.state()["state_version"],
                                           reasoning_summary=long)
        self.gap()
        rows["no_state_version"] = self.tool("play_action", action={"type": "orders",
                                                                    "orders": [{"cmd": "stop", "units": [mcv]}]})
        self.gap()
        rows["wrong_session"] = self.rpc("tools/call", {"name": "get_game_state",
                                                        "arguments": {"session_id": "00000000-0000-0000-0000-000000000000"}})
        rows["unknown_tool"] = self.rpc("tools/call", {"name": "teleport", "arguments": {"session_id": self.sid}})
        summary = {k: _short(v.get("payload")) or v.get("rpc_error") for k, v in rows.items()}
        return {"summary": summary, "rows": rows, "sv": sv}

    # -- phase C: a small base ------------------------------------------------------------

    def _infantry(self, st: dict, need: int = 1) -> list[dict]:
        units = [u for u in (st.get("observation") or {}).get("units") or []
                 if u.get("type") in INFANTRY and not u.get("wreck")]
        if len(units) < need:
            raise ProbeError(f"needs {need} infantry, has {len(units)}")
        return units

    def _building(self, st: dict, *types: str) -> dict | None:
        return next((b for b in (st.get("observation") or {}).get("buildings") or [] if b.get("type") in types), None)

    def _track(self, ids: list[int], seconds: float) -> list[dict]:
        """Watch for ``seconds`` of game time (ticks, so the window is the same on the local
        platform's virtual clock, where a round trip costs no wall time)."""
        rows = []
        end = None
        while True:
            st = self.state()
            tick = st.get("state_version") or 0
            if end is None:
                end = tick + seconds * 25
            elif tick >= end:
                break
            obs = st.get("observation") or {}
            rows.append({"tick": st.get("state_version"), "t": round(time.time(), 3),
                         "units": {u["id"]: [u.get("x"), u.get("y"), u.get("act"), u.get("idle"), u.get("stance")]
                                   for u in obs.get("units") or [] if u.get("id") in ids},
                         "events": [e for e in obs.get("events") or []][-5:],
                         "economy": obs.get("economy"), "production": obs.get("production")})
            time.sleep(0.2)
        return rows

    def _stale(self, raw):
        st = self.state()
        unit = self._infantry(st)[0]["id"]
        sv = st["state_version"]
        a = self.play([{"cmd": "stop", "units": [unit]}], sv - 3001)
        self.gap()
        # The window is measured at arrival (current tick - 3000), so leave room for the round trip.
        b = self.play([{"cmd": "stop", "units": [unit]}], sv - 2950)
        verdict = self.settle(b)
        self.gap()
        return {"summary": {"3001_old": _short(a["payload"]), "2950_old": _short(b["payload"]),
                            "final": _rows((verdict.get("final") or {}))},
                "a": a, "b": b, "verdict": verdict}

    def _stance_guard_queue(self, raw):
        st = self.state()
        inf = self._infantry(st, 3)
        fact = self._building(st, "fact")
        a, b, c = (u["id"] for u in inf[:3])
        fx, fy = fact["x"], fact["y"]
        p1, p2 = self._clamp(st, fx + 4, fy), self._clamp(st, fx + 4, fy + 4)
        orders = [{"cmd": "set_stance", "units": [a], "stance": "hold_fire"},
                  {"cmd": "guard", "units": [b], "target": fact["id"]},
                  {"cmd": "move", "units": [c], "to": p1},
                  {"cmd": "move", "units": [c], "to": p2, "queued": True}]
        sent = self.play(orders, st["state_version"])
        verdict = self.settle(sent)
        track = self._track([a, b, c], 8.0)
        self.gap()
        return {"summary": {"immediate": _rows(sent["payload"]), "final": _rows(verdict.get("final") or {}),
                            "c_path": [r["units"].get(c) for r in track[::5]]},
                "track": track, "verdict": verdict, "ids": [a, b, c], "p1": p1, "p2": p2}

    def _buildings_as_units(self, raw):
        st = self.state()
        fact = self._building(st, "fact")
        unit = self._infantry(st)[0]["id"]
        cases = {"move_building": {"cmd": "move", "units": [fact["id"]], "to": [fact["x"] + 2, fact["y"]]},
                 "stop_building": {"cmd": "stop", "units": [fact["id"]]},
                 "stance_building": {"cmd": "set_stance", "units": [fact["id"]], "stance": "hold_fire"},
                 "attack_ground_infantry": {"cmd": "attack", "units": [unit], "to": [fact["x"] + 6, fact["y"] + 6]}}
        rows = {}
        for name, order in cases.items():
            cur = self.state()
            sent = self.play([order], cur["state_version"])
            verdict = self.settle(sent)
            rows[name] = {"immediate": _rows(sent["payload"]), "final": _rows(verdict.get("final") or {}),
                          "error": (sent["payload"] or {}).get("error"), "detail": (sent["payload"] or {}).get("detail")}
            self.gap()
        return {"summary": rows}

    def _speed(self, raw):
        st = self.state()
        inf = self._infantry(st)
        unit = inf[1] if len(inf) > 1 else inf[0]
        cx, cy = _center(st)
        dx, dy = cx - unit["x"], cy - unit["y"]
        norm = max(1.0, (dx * dx + dy * dy) ** 0.5)
        goal = self._clamp(st, round(unit["x"] + dx / norm * 12), round(unit["y"] + dy / norm * 12))
        sent = self.play([{"cmd": "move", "units": [unit["id"]], "to": goal}], st["state_version"])
        track = self._track([unit["id"]], 12.0)
        path = [(r["tick"], *(r["units"].get(unit["id"]) or [None, None])[:2]) for r in track]
        moving = [(t, x, y) for t, x, y in path if x is not None]
        dist = sum(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 for (_, x1, y1), (_, x2, y2) in zip(moving, moving[1:]))
        ticks = (moving[-1][0] - moving[0][0]) if len(moving) > 1 else 0
        self.gap()
        return {"summary": {"type": unit["type"], "cells": round(dist, 1), "ticks": ticks,
                            "cells_per_s": round(dist / ticks * 25, 2) if ticks else None},
                "goal": goal, "start": [unit["x"], unit["y"]], "path": path, "answer": sent}

    def _power_toggle(self, raw):
        st = self.state()
        # Only these can power down in the RA rules (^DisableOnLowPowerOrPowerDown); others get engine `invalid`.
        own = (st.get("observation") or {}).get("buildings") or []
        plants = ([b for b in own if b.get("type") in POWER_DOWN_TYPES]
                  or [b for b in own if b.get("type") in ("powr", "apwr")])
        if not plants:
            raise ProbeError("no building to power down")
        rows = {}
        for name, orders in (
            ("off", [{"cmd": "power_toggle", "building": p["id"], "on": False} for p in plants]),
            ("off_again", [{"cmd": "power_toggle", "building": plants[0]["id"], "on": False}]),
        ):
            cur = self.state()
            sent = self.play(orders, cur["state_version"])
            rows[name] = {"immediate": _rows(sent["payload"]), "verdict": self.settle(sent)}
            self.gap()
        rows["while_off"] = self._track([], 6.0)
        cur = self.state()
        sent = self.play([{"cmd": "power_toggle", "building": p["id"], "on": True} for p in plants], cur["state_version"])
        rows["on"] = {"immediate": _rows(sent["payload"]), "verdict": self.settle(sent)}
        self.gap()
        cur = self.state()
        sent = self.play([{"cmd": "power_toggle", "building": plants[0]["id"]}], cur["state_version"])
        rows["toggle_no_on"] = {"immediate": _rows(sent["payload"]), "verdict": self.settle(sent)}
        self.gap()
        cur = self.state()
        sent = self.play([{"cmd": "power_toggle", "building": plants[0]["id"], "on": True}], cur["state_version"])
        rows["on_again"] = {"immediate": _rows(sent["payload"]), "verdict": self.settle(sent)}
        rows["after"] = self._track([], 3.0)
        events = sorted({e.get("kind") for r in rows["while_off"] + rows["after"] for e in r["events"]})
        econ = [r["economy"] for r in rows["while_off"][:1] + rows["after"][-1:]]
        self.gap()
        return {"summary": {"events": events, "economy_off_then_on": econ,
                            "off_again": _rows((rows["off_again"]["verdict"].get("final") or {}))},
                "rows": rows}

    def _repair_primary_rally(self, raw):
        st = self.state()
        fact = self._building(st, "fact")
        barracks = self._building(st, "tent", "barr")
        cases = {"repair_undamaged": [{"cmd": "repair", "building": fact["id"]}],
                 "repair_off_undamaged": [{"cmd": "repair", "building": fact["id"], "on": False}],
                 "set_primary": [{"cmd": "set_primary", "building": barracks["id"]}],
                 "set_primary_fact": [{"cmd": "set_primary", "building": fact["id"]}],
                 "rally_far": [{"cmd": "rally", "building": barracks["id"], "to": list(_center(st))}]}
        rows = {}
        for name, orders in cases.items():
            cur = self.state()
            sent = self.play(orders, cur["state_version"])
            verdict = self.settle(sent)
            rows[name] = {"immediate": _rows(sent["payload"]), "final": _rows(verdict.get("final") or {}),
                          "error": (sent["payload"] or {}).get("error"), "detail": (sent["payload"] or {}).get("detail")}
            self.gap()
        rows["barracks_after"] = self._building(self.state(), "tent", "barr")
        return {"summary": rows}

    def _queue_and_cancel(self, raw):
        steps = {}
        infantry_item = next((i for i in ("e1", "e3") if i in ((self.state().get("observation") or {})
                                                            .get("available_production") or [])), "e1")
        for name, orders in (("train_5", [{"cmd": "train", "item": infantry_item, "count": 5}]),
                             ("cancel_1", [{"cmd": "cancel", "item": infantry_item}]),
                             ("build_powr", [{"cmd": "build", "item": "powr"}]),
                             ("build_powr_again", [{"cmd": "build", "item": "powr"}]),
                             ("cancel_powr", [{"cmd": "cancel", "item": "powr"}])):
            cur = self.state()
            before = (cur.get("observation") or {}).get("production")
            sent = self.play(orders, cur["state_version"])
            verdict = self.settle(sent)
            after = (self.state().get("observation") or {}).get("production")
            steps[name] = {"immediate": _rows(sent["payload"]), "final": _rows(verdict.get("final") or {}),
                           "error": (sent["payload"] or {}).get("error"), "before": before, "after": after}
            self.gap()
        return {"summary": {k: (v["final"] or v["immediate"] or v["error"]) for k, v in steps.items()}, "steps": steps}

    def _wait_ready(self, item: str, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            prod = (self.state().get("observation") or {}).get("production") or []
            if any(p.get("item") == item and p.get("ready") for p in prod):
                return True
            time.sleep(0.5)
        return False

    def _place_at(self, raw):
        st = self.state()
        fact = self._building(st, "fact")
        targets = {"near": [fact["x"] - 4, fact["y"] + 3], "on_fact": [fact["x"], fact["y"]],
                   "far": list(_center(st))}
        rows = {}
        for name, at in targets.items():
            cur = self.state()
            prod = (cur.get("observation") or {}).get("production") or []
            if any(p.get("queue") == "Building" for p in prod):
                self._wait_queue_free(40.0)
                cur = self.state()
            self.play([{"cmd": "build", "item": "powr"}], cur["state_version"])
            self.gap()
            if not self._wait_ready("powr", 30.0):
                rows[name] = {"error": "powr never ready"}
                continue
            before = {b["id"] for b in (self.state().get("observation") or {}).get("buildings") or []}
            cur = self.state()
            sent = self.play([{"cmd": "place", "item": "powr", "at": at}], cur["state_version"])
            verdict = self.settle(sent)
            time.sleep(0.5)
            after = [b for b in (self.state().get("observation") or {}).get("buildings") or [] if b["id"] not in before]
            rows[name] = {"at": at, "landed": [[b["type"], b["x"], b["y"]] for b in after],
                          "final": _rows(verdict.get("final") or {}), "immediate": _rows(sent["payload"])}
            self.gap()
        return {"summary": {k: (v.get("at"), v.get("landed"), v.get("final")) for k, v in rows.items()}, "rows": rows}

    def _wait_queue_free(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            prod = (self.state().get("observation") or {}).get("production") or []
            building = [p for p in prod if p.get("queue") == "Building"]
            if not building:
                return
            ready = [p for p in building if p.get("ready")]
            if ready:  # someone else's finished building: place it so the queue frees up
                cur = self.state()
                self.play([{"cmd": "place", "item": ready[0]["item"]}], cur["state_version"])
                self.gap()
            time.sleep(0.5)

    def _sell(self, raw):
        st = self.state()
        plants = [b for b in (st.get("observation") or {}).get("buildings") or [] if b.get("type") == "powr"]
        target = plants[-1]
        cash_before = (st.get("observation") or {}).get("economy")
        sent = self.play([{"cmd": "sell", "building": target["id"]}], st["state_version"])
        verdict = self.settle(sent)
        track = self._track([], 4.0)
        events = [e for r in track for e in r["events"] if e.get("id") == target["id"]]
        self.gap()
        return {"summary": {"final": _rows(verdict.get("final") or {}), "events": events[:3],
                            "economy_before": cash_before, "economy_after": track[-1]["economy"] if track else None},
                "track": track}

    def _harvest_to(self, raw):
        st = self.state()
        harv = [u for u in (st.get("observation") or {}).get("units") or [] if u.get("type") == "harv"]
        if not harv:
            return {"summary": "no harvester"}
        h = harv[0]
        to = self._clamp(st, h["x"] + 3, h["y"] + 3)
        sent = self.play([{"cmd": "harvest", "units": [h["id"]], "to": to}], st["state_version"])
        verdict = self.settle(sent)
        track = self._track([h["id"]], 4.0)
        self.gap()
        cur = self.state()
        back = self.play([{"cmd": "harvest", "units": [h["id"]]}], cur["state_version"])
        return {"summary": {"final": _rows(verdict.get("final") or {}),
                            "path": [r["units"].get(h["id"]) for r in track[::4]], "back": _rows(back["payload"])},
                "track": track}

    # -- the end ----------------------------------------------------------------------------

    def _end(self, raw):
        out = {"get_result_before": self.tool("get_result")}
        if not self.resign_at_end:
            out["summary"] = "probe finished; bot plays on"
            return out
        out["resign"] = self.tool("resign")
        time.sleep(2.0)
        out["state_after"] = self.tool("get_game_state")
        out["result_after"] = self.tool("get_result")
        out["play_after"] = self.play([{"cmd": "stop", "units": [1]}], _tick(raw))
        out["resign_again"] = self.tool("resign")
        out["summary"] = {k: _short(v.get("payload")) for k, v in out.items() if isinstance(v, dict) and "payload" in v}
        return out

    def _clamp(self, st: dict, x: int, y: int) -> list[int]:
        bx, by, bw, bh = (st.get("map") or {}).get("bounds") or [1, 1, 90, 90]
        return [min(max(int(x), bx + 1), bx + bw - 2), min(max(int(y), by + 1), by + bh - 2)]


def _tick(raw: dict) -> int:
    try:
        return int(raw.get("state_version") or 0)
    except (TypeError, ValueError):
        return 0


def _center(st: dict) -> tuple[int, int]:
    bx, by, bw, bh = (st.get("map") or {}).get("bounds") or [1, 1, 90, 90]
    return bx + bw // 2, by + bh // 2


def _rows(payload: Any) -> list:
    if not isinstance(payload, dict):
        return []
    return [[r.get("cmd"), r.get("status"), r.get("reason"), r.get("detail"), r.get("failed") or None]
            for r in payload.get("results") or [] if isinstance(r, dict)]


def _short(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if "error" in payload:
        return {"error": payload.get("error"), "detail": str(payload.get("detail"))[:200]}
    keep = {k: payload.get(k) for k in ("accepted", "pending", "state_version", "status", "is_terminal",
                                        "termination_reason", "your_return", "next_batch_allowed_in_ms") if k in payload}
    if "results" in payload:
        keep["results"] = _rows(payload)
    return keep
