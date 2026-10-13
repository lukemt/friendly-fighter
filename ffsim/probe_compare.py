"""Compare two probe logs (agent/probe.py): the hosted platform's and a local game's.

    python -m ffsim.probe_compare recordings/<real>/probe-seat0.jsonl recordings/<sim>/probe-seat0.jsonl

For every step both ran, the results are walked side by side. Strings are compared
with digits masked (ids, ticks and cells differ between games), verdict rows
``[cmd, status, reason, detail, failed]`` by their cmd/status/reason, numbers only by
type. A step that failed on one side only is reported too.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Keys whose values legitimately differ between games (measurements, positions, ids).
IGNORE = {"dt", "t", "took_s", "tick", "first_dt", "second_dt", "path", "c_path", "start", "goal", "cells", "ticks",
          "cells_per_s", "economy_off_then_on", "events", "while_off", "after", "near", "far", "on_fact",
          "barracks_after", "keys", "seen_at", "views", "traceback", "problems", "rev", "applied_at_tick",
          "state_version", "from", "to", "next", "batch", "type", "session_id", "name", "agent_id", "faction",
          "enemy_faction", "track", "first", "units", "available_production", "before"}


def load(path: Path) -> dict:
    steps = {}
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("step"):
            steps[r["step"]] = r
    return steps


def _mask(s: str) -> str:
    s = re.sub(r"(sim-)?[0-9a-f]{8}-[0-9a-f-]{27}", "<session>", s)
    s = re.sub(r"input_value=.*?(, input_type|$)", r"input_value=<value>\1", s, flags=re.S)
    return re.sub(r"\d+", "#", s)


def _is_row(v) -> bool:
    return isinstance(v, list) and len(v) == 5 and (v[0] is None or isinstance(v[0], str)) and isinstance(v[1], str)


def diff(a, b, path: str, out: list) -> None:
    if _is_row(a) and _is_row(b):
        if a[:3] != b[:3]:
            out.append(f"{path}: real {a[:3]} vs sim {b[:3]}")
        elif (a[3] is None) != (b[3] is None) or (isinstance(a[3], str) and isinstance(b[3], str) and _mask(a[3]) != _mask(b[3])):
            out.append(f"{path}: detail real {a[3]!r} vs sim {b[3]!r}")
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in IGNORE:
                continue
            if k not in b:
                out.append(f"{path}.{k}: only real")
            elif k not in a:
                out.append(f"{path}.{k}: only sim")
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} items real vs {len(b)} sim")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
        return
    if isinstance(a, str) and isinstance(b, str):
        if _mask(a)[:160] != _mask(b)[:160]:
            out.append(f"{path}: real {a[:160]!r} vs sim {b[:160]!r}")
        return
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        if a != b:
            out.append(f"{path}: real {a!r} vs sim {b!r}")
        return
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        out.append(f"{path}: type real {type(a).__name__} vs sim {type(b).__name__}")


def compare(real: dict, sim: dict) -> dict[str, list[str]]:
    report = {}
    for step in sorted(set(real) | set(sim)):
        if step not in sim:
            report[step] = ["not run in sim"]
            continue
        if step not in real:
            report[step] = ["not run on the platform"]
            continue
        a, b = real[step], sim[step]
        out = []
        if a.get("ok") != b.get("ok"):
            out.append(f"ok: real {a.get('ok')} vs sim {b.get('ok')}")
        if a.get("ok") and b.get("ok"):
            ra, rb = a.get("result"), b.get("result")
            if step.startswith("a06"):
                # Two calls sent at the same moment: which thread wins is random; compare the outcomes as a set.
                ra, rb = (sorted(r.get("summary") or [], key=lambda x: json.dumps(x, sort_keys=True)[:1] + str("error" in x))
                          for r in (ra, rb))
            diff(ra, rb, "", out)
        report[step] = out
    return report


def main(argv: list[str]) -> int:
    report = compare(load(Path(argv[0])), load(Path(argv[1])))
    total = 0
    for step, issues in report.items():
        print(f"{step}: {'same' if not issues else f'{len(issues)} difference(s)'}")
        for i in issues[:40]:
            print("   ", i)
        total += len(issues)
    print(f"{total} difference(s) in {sum(1 for v in report.values() if v)} of {len(report)} steps")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
