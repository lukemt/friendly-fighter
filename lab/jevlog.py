"""What a Jev-steered bot (bots/jev_bot.py) was thinking: its call log, one line of JSON per call, in
``<lab>/jev/<session>-<seat>.jsonl`` (the session of a lab game is ``lab-<game id>``)."""

from __future__ import annotations

import json
from pathlib import Path

from lab import LAB_DIR

PRICE_PER_MTOK = 0.042  # input tokens; output is free (docs.typesafe.ai/models)


def log_files(game_id: str | None = None) -> list[Path]:
    root = Path(LAB_DIR) / "jev"
    if not root.is_dir():
        return []
    pattern = f"{game_id}-*.jsonl" if game_id else "*.jsonl"
    return sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime)


def read(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _clock(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def timeline(rows: list[dict], *, every: bool = False) -> str:
    """One line per change of decision (every call with ``every``), with the facts that drove it."""
    out, prev = [], None
    for r in rows:
        decision = r.get("decision") or ""
        key = decision.split(" open=")[0].split(" rails:")[0]
        s = r.get("state") or {}
        if not every and key == prev and "our_attack" not in s:
            continue
        prev = key
        bits = [s.get("score", "").replace("kills minus losses: ", ""), s.get("our_army", "")[:40]]
        if s.get("our_attack"):
            bits.append(s["our_attack"][:90])
        if s.get("now_at_our_base", "").startswith(tuple("0123456789")):
            bits.append(s["now_at_our_base"])
        out.append(f"{_clock(r['t']):>5}  {decision[:70]:<70}  " + " | ".join(b for b in bits if b))
    return "\n".join(out)


def moment(rows: list[dict], seconds: float) -> str:
    """The call nearest to ``seconds``: everything Jev was shown and what it answered."""
    r = min(rows, key=lambda r: abs(r["t"] - seconds))
    lines = [f"call at {_clock(r['t'])} ({r.get('latency', 0) * 1000:.0f} ms, {r.get('tokens', 0)} tokens)",
             f"decision: {r.get('decision')}", "", "state:"]
    lines += [f"  {k}: {v}" for k, v in (r.get("state") or {}).items()]
    lines += ["", "answers:"]
    for q, a in (r.get("answers") or {}).items():
        if isinstance(a, dict):
            a = ", ".join(f"{k} {v:.2f}" for k, v in sorted(a.items(), key=lambda kv: -kv[1]))
        lines.append(f"  {q}: {a}")
    return "\n".join(lines)


def usage(paths: list[Path]) -> str:
    calls = tokens = 0
    latency = []
    for p in paths:
        for r in read(p):
            calls += 1
            tokens += r.get("tokens") or 0
            if r.get("latency"):
                latency.append(r["latency"])
    if not calls:
        return "no Jev calls logged"
    latency.sort()
    pct = lambda q: latency[min(len(latency) - 1, int(q * len(latency)))] * 1000  # noqa: E731
    return (f"{len(paths)} game seats, {calls} calls ({calls / max(1, len(paths)):.0f} a game), "
            f"{tokens:,} input tokens (${tokens / 1e6 * PRICE_PER_MTOK:.4f}; "
            f"${tokens / max(1, len(paths)) / 1e6 * PRICE_PER_MTOK:.5f} a game); "
            f"latency median {pct(0.5):.0f} ms, p95 {pct(0.95):.0f} ms")
