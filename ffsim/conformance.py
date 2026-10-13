"""Compare a local (ffsim) game's recordings with a real platform game's.

    python -m ffsim.conformance recordings/<real session> recordings/<sim session>

Checks, over every recorded state and wire answer of both:

- the schema: each key path and the JSON types seen there (``observation.units[].act``:
  ``str|null``), and which paths only one side has;
- the clock: views every 6 ticks, ``state_version == observation.tick``;
- the verdict timing: ``applied_at_tick - state_version == 12`` in settled ``last_orders``;
- the vocabularies: phases, statuses, verdict reasons, event kinds, unit activities.
"""

from __future__ import annotations

import collections
import glob
import gzip
import json
import sys
from pathlib import Path

# Paths whose value set is open-ended (ids, names...): only types are compared there.
VOCAB_PATHS = {
    "phase": "phase", "status": "status",
    "last_orders.results[].status": "verdict status", "last_orders.results[].reason": "verdict reason",
    "observation.events[].kind": "event kind", "observation.units[].stance": "stance",
}


def _type(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    return "obj"


DYNAMIC_KEYS = {"returns"}  # objects keyed by agent id


def walk(obj, path: str, out: dict, vocab: dict) -> None:
    out[path].add(_type(obj))
    if path.rsplit(".", 1)[-1] in DYNAMIC_KEYS:
        return
    if path in VOCAB_PATHS and isinstance(obj, str):
        vocab[path].add(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(v, f"{path}.{k}" if path else k, out, vocab)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, f"{path}[]", out, vocab)


def _records(folder: Path):
    for p in sorted(glob.glob(str(folder / "seat*.jsonl.gz"))) + sorted(glob.glob(str(folder / "wire-*.jsonl.gz"))):
        try:
            with gzip.open(p, "rt") as f:
                for line in f:
                    try:
                        yield p, json.loads(line)
                    except ValueError:
                        return
        except (EOFError, OSError):
            continue


def profile(folder: Path) -> dict:
    states = collections.defaultdict(set)
    answers = collections.defaultdict(lambda: collections.defaultdict(set))
    vocab = collections.defaultdict(set)
    steps = collections.Counter()
    latency = collections.Counter()
    mismatched_sv = 0
    last_tick = {}
    for path, r in _records(folder):
        if r.get("kind") == "state" and isinstance(r.get("raw"), dict):
            raw = r["raw"]
            walk(raw, "", states, vocab)
            obs = raw.get("observation") or {}
            if raw.get("phase") == "playing" and obs:
                if raw.get("state_version") != obs.get("tick"):
                    mismatched_sv += 1
                prev = last_tick.get(path)
                if prev is not None and obs["tick"] > prev:
                    steps[obs["tick"] - prev] += 1
                last_tick[path] = obs["tick"]
            lo = raw.get("last_orders") or {}
            if lo.get("applied_at_tick") is not None:
                latency[lo["applied_at_tick"] - lo["state_version"]] += 1
        elif "tool" in r and isinstance(r.get("answer"), dict):
            walk(r["answer"], "", answers[r["tool"]], vocab)
    return {"states": states, "answers": answers, "vocab": vocab, "steps": steps, "latency": latency,
            "mismatched_sv": mismatched_sv}


def _norm(types: set) -> set:
    return types - {"null"} if len(types) > 1 else types


def compare(real: dict, sim: dict) -> list[str]:
    issues = []

    def cmp_schema(a: dict, b: dict, where: str) -> None:
        for path in sorted(set(a) | set(b)):
            if path not in b:
                issues.append(f"{where}: only real has {path} ({'|'.join(sorted(a[path]))})")
            elif path not in a:
                issues.append(f"{where}: only sim has {path} ({'|'.join(sorted(b[path]))})")
            elif _norm(a[path]) != _norm(b[path]) and not ({"num"} <= a[path] and {"num"} <= b[path]):
                issues.append(f"{where}: {path} types real={sorted(a[path])} sim={sorted(b[path])}")

    cmp_schema(real["states"], sim["states"], "state")
    for tool in sorted(set(real["answers"]) & set(sim["answers"])):
        cmp_schema(real["answers"][tool], sim["answers"][tool], f"answer {tool}")
    for path, label in VOCAB_PATHS.items():
        extra = sim["vocab"][path] - real["vocab"][path]
        if extra and real["vocab"][path]:
            issues.append(f"vocab {label}: sim has {sorted(extra)} never seen on the platform")
    if sim["mismatched_sv"]:
        issues.append(f"clock: {sim['mismatched_sv']} sim states where state_version != observation.tick")
    odd = {k: v for k, v in sim["steps"].items() if k % 6}
    if odd:
        issues.append(f"clock: sim view steps not a multiple of 6: {dict(odd)}")
    bad = {k: v for k, v in sim["latency"].items() if k != 12}
    if bad:
        issues.append(f"latency: sim applied_at_tick - state_version != 12: {dict(bad)}")
    return issues


def main(argv: list[str]) -> int:
    real, sim = profile(Path(argv[0])), profile(Path(argv[1]))
    issues = compare(real, sim)
    print(f"real view steps {dict(real['steps'].most_common(4))}, sim view steps {dict(sim['steps'].most_common(4))}")
    print(f"real latency {dict(real['latency'])}, sim latency {dict(sim['latency'])}")
    for label, path in ((v, k) for k, v in VOCAB_PATHS.items()):
        print(f"{label}: real {sorted(real['vocab'][path])} | sim {sorted(sim['vocab'][path])}")
    print(f"{len(issues)} issue(s)")
    for i in issues:
        print(" -", i)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
