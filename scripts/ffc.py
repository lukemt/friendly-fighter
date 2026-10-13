"""Client for the console seat (agent/console.py): drive one seat live.

CLI:
    python scripts/ffc.py on --seat 0 [--bot off]   # for the NEXT match (read at its first view)
    python scripts/ffc.py off
    python scripts/ffc.py state [--seat 0]          # compact summary of the seat's view
    python scripts/ffc.py call TOOL '{"arg": 1}'    # any tool, raw answer (session_id is added)
    python scripts/ffc.py list                       # tools/list
    python scripts/ffc.py play '[{"cmd": "stop", "units": [101]}]'   # with the newest state_version
    python scripts/ffc.py bot on|off                 # let the scripted bot play this seat, or not

Library (for timing experiments; calls go straight to GameAPI, not via the proxy):
    import sys; sys.path.insert(0, "/workspace/scripts"); from ffc import Seat
    s = Seat(0); st = s.state(); s.play([...], st["state_version"]); s.settle(sv)

The seat token is fetched from the agent process and never printed.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REMOTE = Path(os.environ.get("FF_REMOTE_DIR") or ROOT / "remote")
sys.path.insert(0, str(ROOT))


class Seat:
    def __init__(self, seat: int = 0) -> None:
        info = json.loads((REMOTE / f"console-seat{seat}.json").read_text())
        self.base = info["url"]
        self.seat = seat
        self.local = httpx.Client(timeout=60.0)
        self.http = httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0))
        self._ids = itertools.count(1)
        self._creds: dict = {}
        self._creds_at = 0.0

    # -- via the agent process ------------------------------------------------------

    def creds(self, fresh: bool = False) -> dict:
        if fresh or time.time() - self._creds_at > 60:
            self._creds = self.local.get(self.base + "/creds").json()
            self._creds_at = time.time()
        return self._creds

    def bot(self, on: bool | None = None) -> bool:
        if on is None:
            return self.local.get(self.base + "/bot").json()["on"]
        return self.local.post(self.base + "/bot", json={"on": on}).json()["on"]

    def proxy(self, tool: str | None = None, method: str | None = None, **arguments) -> dict:
        body = {"tool": tool, "arguments": arguments} if tool else {"method": method, "params": arguments}
        return self.local.post(self.base + "/call", json=body).json()

    # -- straight to GameAPI ----------------------------------------------------------

    def rpc(self, method: str, params: dict, http: httpx.Client | None = None) -> dict:
        c = self.creds()
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        sent = time.time()
        r = (http or self.http).post(c["mcp_url"], json=body, headers={
            "Authorization": f"Bearer {c['token']}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        if r.status_code == 401:
            self.creds(fresh=True)
            return self.rpc(method, params, http)
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            text = data[-1] if data else ""
        envelope = json.loads(text) if text else {}
        result = envelope.get("result") or {}
        payload = result.get("structuredContent") if method == "tools/call" else result
        if payload is None and result.get("content"):
            try:
                payload = json.loads(result["content"][0].get("text") or "null")
            except ValueError:
                payload = result["content"][0].get("text")
        return {"payload": payload, "dt": round(time.time() - sent, 3), "http": r.status_code,
                "rpc_error": envelope.get("error"), "t": round(sent, 3)}

    def call(self, tool: str, http: httpx.Client | None = None, **arguments):
        args = {"session_id": self.creds()["session_id"], **arguments}
        return self.rpc("tools/call", {"name": tool, "arguments": args}, http)["payload"]

    def state(self) -> dict:
        return self.call("get_game_state")

    def play(self, orders, sv: int | None = None, **extra):
        if sv is None:
            sv = self.state()["state_version"]
        action = orders if isinstance(orders, dict) and "type" in orders else {"type": "orders", "orders": orders}
        return self.call("play_action", state_version=sv, action=action, **extra)

    def settle(self, sv: int, timeout: float = 6.0) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            st = self.state()
            lo = st.get("last_orders") or {}
            if lo.get("state_version") == sv and not lo.get("pending"):
                return {"final": lo, "seen_at": st.get("state_version"), "problems": st.get("recent_order_problems")}
            time.sleep(0.05)
        return None


def _summary(st: dict) -> dict:
    from agent.remote import summarize

    out = summarize(st)
    out["state_version"] = st.get("state_version")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command")
    p.add_argument("args", nargs="*")
    p.add_argument("--seat", type=int, default=0)
    p.add_argument("--bot", default="on")
    p.add_argument("--full", action="store_true")
    a = p.parse_args()
    if a.command in ("on", "off"):
        REMOTE.mkdir(exist_ok=True)
        data = {"enabled": a.command == "on", "seat": a.seat, "bot": a.bot != "off"}
        (REMOTE / "console.json").write_text(json.dumps(data))
        print("console:", data)
        return 0
    s = Seat(a.seat)
    if a.command == "bot":
        print("bot on:", s.bot(a.args[0] == "on") if a.args else s.bot())
    elif a.command == "state":
        st = s.state()
        print(json.dumps(st if a.full else _summary(st), indent=None if not a.full else 1, default=str))
    elif a.command == "list":
        print(json.dumps(s.rpc("tools/list", {})["payload"], indent=1))
    elif a.command == "call":
        print(json.dumps(s.call(a.args[0], **(json.loads(a.args[1]) if len(a.args) > 1 else {})), default=str))
    elif a.command == "play":
        print(json.dumps(s.play(json.loads(a.args[0])), default=str))
    else:
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
