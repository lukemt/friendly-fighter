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


def read(path: Path | str) -> list[dict]:
    rows = []
    for line in Path(path).read_text().splitlines():
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
    calls = tokens = unanswered = 0
    latency = []
    for p in paths:
        for r in read(p):
            calls += 1
            unanswered += not r.get("answers")
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
            f"latency median {pct(0.5):.0f} ms, p95 {pct(0.95):.0f} ms"
            + (f"\nWARNING: {unanswered} calls got no answer (no TYPESAFE_API_KEY, or Jev failing): "
               f"those games played without Jev" if unanswered else ""))


def _net(call: dict) -> int:
    score = (call.get("state") or {}).get("score", "")
    if "behind by" in score:
        return -int(score.split("behind by ")[1].split()[0])
    if "ahead by" in score:
        return int(score.split("ahead by ")[1].split()[0])
    return 0


def attacks(rows: list[dict]) -> list[dict]:
    """Each attack (a run of ``commit`` decisions): when, the score before, the change over the
    attack and the 20 s after it, and what Jev decided next."""
    out, start = [], None
    for i, c in enumerate(rows + [None]):
        committed = c is not None and str(c.get("decision", "")).startswith("commit")
        if committed and start is None:
            start = i
        if not committed and start is not None:
            end = c or rows[-1]
            after = next((r for r in rows[i:] if r["t"] >= end["t"] + 20), rows[-1])
            out.append({"t": rows[start]["t"], "before": _net(rows[start]), "change": _net(after) - _net(rows[start]),
                        "ended": str(end.get("decision", "")).split("(")[0] if c else "game over",
                        "army": (rows[start].get("state") or {}).get("our_army", "").split(" combat")[0]})
            start = None
    return out


def attack_table(db, paths: list[Path]) -> str:
    """Every logged attack, grouped by opponent: how often it gained, and by how much."""
    import re

    groups: dict[str, list[dict]] = {}
    for p in paths:
        m = re.match(r"(lab-.+)-(\d)\.jsonl$", p.name)
        if not m:
            continue
        opp = db.q("SELECT bot FROM seats WHERE game_id = ? AND position != ?", m.group(1), int(m.group(2)))
        if not opp:
            continue
        for a in attacks(read(p)):
            groups.setdefault(opp[0]["bot"], []).append(a)
    if not groups:
        return "no attacks logged"
    lines = [f"{'opponent':<18}{'attacks':>8}{'gained':>8}{'mean':>8}{'median':>8}   when behind <=500 / more / not behind"]
    for opp, rows in sorted(groups.items()):
        ch = sorted(r["change"] for r in rows)
        def part(f):
            sel = [r["change"] for r in rows if f(r["before"])]
            return f"{len(sel)}: {sum(sel) / len(sel):+.0f}" if sel else "-"
        lines.append(f"{opp:<18}{len(rows):>8}{sum(c > 0 for c in ch):>8}{sum(ch) / len(ch):>+8.0f}{ch[len(ch) // 2]:>+8}   "
                     f"{part(lambda b: -500 <= b < 0)} / {part(lambda b: b < -500)} / {part(lambda b: b >= 0)}")
    return "\n".join(lines)
