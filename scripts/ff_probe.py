"""Turn the probe seat (agent/probe.py) on or off for the NEXT match.

    python scripts/ff_probe.py on --seat 0 [--no-resign]   # seat 0 runs the experiments
    python scripts/ff_probe.py off
    python scripts/ff_probe.py status                      # the latest probe log, one line per step

The agent reads remote/probe.json once, at a match's first view, so turn it
on before the match starts. With --no-resign the bot plays on after the
experiments instead of resigning.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTROL = ROOT / "remote" / "probe.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["on", "off", "status"])
    parser.add_argument("--seat", type=int, default=0)
    parser.add_argument("--no-resign", action="store_true")
    a = parser.parse_args()
    if a.command in ("on", "off"):
        CONTROL.parent.mkdir(exist_ok=True)
        data = {"enabled": a.command == "on", "seat": a.seat, "resign_at_end": not a.no_resign}
        CONTROL.write_text(json.dumps(data))
        print("probe:", data)
        return 0
    logs = sorted(glob.glob(str(ROOT / "recordings" / "*" / "probe-seat*.jsonl")), key=os.path.getmtime)
    if not logs:
        print("no probe log yet")
        return 0
    print(logs[-1])
    for line in Path(logs[-1]).read_text().splitlines():
        rec = json.loads(line)
        summary = (rec.get("result") or {}).get("summary", "")
        print(f"{rec['step']:<28} {'ok ' if rec['ok'] else 'ERR'} tick={rec['tick']:<6} {rec['took_s']:>6}s  "
              f"{json.dumps(summary, default=str)[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
