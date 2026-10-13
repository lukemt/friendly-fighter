"""Play one Red Alert seat by hand through the running agent (agent/remote.py).

    python scripts/ff_play.py on --seat 1 [--manual]   # take over seat 1 (autopilot stays on unless --manual)
    python scripts/ff_play.py off                      # hand the seat back to the bot
    python scripts/ff_play.py autopilot on|off
    python scripts/ff_play.py status [--seat 1] [--full]
    python scripts/ff_play.py build powr | train e1 5 | place powr [x y]
    python scripts/ff_play.py attack X Y [all|ID ...]   # attack-move
    python scripts/ff_play.py move X Y ID ...
    python scripts/ff_play.py target ENEMY_ID ID ...    # attack a visible enemy
    python scripts/ff_play.py raw '<order json or list>'
    python scripts/ff_play.py knobs posture=attack attack_threshold=6
    python scripts/ff_play.py log [N]                   # answers to my last orders

Orders go to remote/seat<N>/inbox/ and are sent on the agent's next view
(about every 0.5 s). The seat is the runtime's 0-based seat_position; in a
self-play match, status shows which faction each seat is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "remote"


def control() -> dict:
    try:
        return json.loads((ROOT / "control.json").read_text())
    except (OSError, ValueError):
        return {}


def save_control(data: dict) -> None:
    ROOT.mkdir(exist_ok=True)
    tmp = ROOT / "control.json.tmp"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, ROOT / "control.json")


def seat_dir(seat) -> Path:
    if seat is None:
        seat = control().get("seat", 0)
    return ROOT / f"seat{seat}"


def send(seat, message: dict) -> None:
    inbox = seat_dir(seat) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}.json"
    tmp = inbox / (name + ".tmp")
    tmp.write_text(json.dumps(message))
    os.replace(tmp, inbox / name)
    print(f"queued: {json.dumps(message)}")


def unit_ids(summary: dict, wanted: list[str]) -> list[int]:
    if not wanted or wanted == ["all"]:
        return summary.get("combat_unit_ids") or []
    return [int(x) for x in wanted]


def load_state(seat) -> dict:
    path = seat_dir(seat) / "state.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        sys.exit(f"No state yet at {path}: is remote control on and a match running?")
    state["_age_s"] = round(time.time() - path.stat().st_mtime, 1)
    return state


def show(state: dict, full: bool) -> None:
    if full:
        print(json.dumps(state, indent=1))
        return
    t = state.get("time") or {}
    e = state.get("economy") or {}
    m = state.get("military") or {}
    print(f"[{state.get('faction')} vs {state.get('enemy_faction')}] t={t.get('game_seconds')}s left={t.get('remaining_seconds')}s "
          f"(view {state['_age_s']}s old)  base={state.get('base_center')} enemy_base={state.get('enemy_base')} map={state.get('map')}")
    print(f"cash={e.get('cash')} ore={e.get('ore')} power={e.get('power_provided')}/{e.get('power_drained')} "
          f"harvesters={e.get('harvesters')}  kills/losses=${m.get('kills_cost')}/${m.get('deaths_cost')} assets=${m.get('assets_value')}")
    print("buildings:", " ".join(f"{b[1]}#{b[0]}@{b[2]},{b[3]}({b[4]}%)" for b in state.get("buildings") or []))
    print("production:", state.get("production"), " can build:", state.get("available_production"))
    for kind, rows in (state.get("units_by_type") or {}).items():
        print(f"  {kind} x{len(rows)}: " + " ".join(f"#{r[0]}@{r[1]},{r[2]}({r[3]}%,{r[4]})" for r in rows[:15]))
    print("enemy units:", state.get("enemy_units"))
    print("enemy buildings:", state.get("enemy_buildings"))
    print("events:", [f"{ev.get('kind')}:{ev.get('type', '')}" for ev in state.get("events") or []])
    if state.get("last_orders"):
        print("last batch verdicts:", state["last_orders"])
    if state.get("result"):
        print("RESULT:", state["result"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command")
    parser.add_argument("args", nargs="*")
    parser.add_argument("--seat", default=None)
    parser.add_argument("--manual", action="store_true", help="with 'on': autopilot off")
    parser.add_argument("--full", action="store_true", help="with 'status': the whole summary as JSON")
    a = parser.parse_args()
    cmd, args, seat = a.command, a.args, a.seat

    if cmd == "on":
        save_control({"enabled": True, "seat": int(seat if seat is not None else 0), "autopilot": not a.manual})
        print("control:", control())
    elif cmd == "off":
        save_control({**control(), "enabled": False})
        print("control:", control())
    elif cmd == "autopilot":
        save_control({**control(), "autopilot": args[:1] == ["on"]})
        print("control:", control())
    elif cmd == "status":
        show(load_state(seat), a.full)
    elif cmd == "build":
        send(seat, {"orders": [{"cmd": "build", "item": args[0]}]})
    elif cmd == "train":
        send(seat, {"orders": [{"cmd": "train", "item": args[0], "count": int(args[1]) if len(args) > 1 else 1}]})
    elif cmd == "place":
        order = {"cmd": "place", "item": args[0]}
        if len(args) >= 3:
            order["at"] = [int(args[1]), int(args[2])]
        send(seat, {"orders": [order]})
    elif cmd in ("attack", "move"):
        ids = unit_ids(load_state(seat), args[2:])
        to = [int(args[0]), int(args[1])]
        verb = "attack_move" if cmd == "attack" else "move"
        send(seat, {"orders": [{"cmd": verb, "units": ids[i:i + 50], "to": to} for i in range(0, len(ids), 50)]})
    elif cmd == "target":
        send(seat, {"orders": [{"cmd": "attack", "units": [int(x) for x in args[1:]], "target": int(args[0])}]})
    elif cmd == "raw":
        payload = json.loads(args[0])
        send(seat, {"orders": payload if isinstance(payload, list) else [payload]})
    elif cmd == "knobs":
        knobs = {}
        for pair in args:
            key, _, value = pair.partition("=")
            knobs[key] = int(value) if value.isdigit() else value
        send(seat, {"knobs": knobs})
    elif cmd == "log":
        path = seat_dir(seat) / "log.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        for line in lines[-int(args[0] if args else 5):]:
            print(line)
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
