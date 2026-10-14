"""Jev's decisions against a labelled set, without playing: the prompt loop in seconds.

``lab/jev_cases.json`` holds moments from real games (the state a Jev bot sent, from its call log)
with the answer Claude would give there (``expect``: question -> acceptable choices) and why.
``evaluate`` asks Jev the bot's questions on every case (in parallel) and scores the answers, so a
change to the playbook or the criteria can be tried before any game is played:

    lab jev eval                       # the bot's questions as they are
    lab jev eval --questions FILE      # a variant: {"playbook": [...], "criteria": {"posture": {...}}}
    lab jev case GAME --at 7:40 --expect posture=defend,gather --why "..."   # add a case
"""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lab import ROOT

CASES = ROOT / "lab" / "jev_cases.json"


def load_cases(path: Path = CASES) -> list[dict]:
    return json.loads(path.read_text())["cases"] if path.exists() else []


def save_cases(cases: list[dict], path: Path = CASES) -> None:
    path.write_text(json.dumps({"cases": cases}, indent=1) + "\n")


def questions(variant: dict | None = None, *, opening: bool = False) -> dict:
    """The bot's questions, with a variant's playbook and criteria swapped in."""
    import tempfile

    from bots import jev_bot

    path = None
    if variant:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(variant, f)
            path = f.name
    qs = jev_bot.load_questions(path)
    qs["economy"] = jev_bot.ECONOMY_QUESTION  # (asked only of the cases that expect it)
    if not opening:
        qs.pop("opening", None)
    return qs


def evaluate(cases: list[dict], variant: dict | None = None, *, jev=None, workers: int = 8) -> dict:
    """Ask every case; per question: right/asked, and each miss with what Jev said."""
    from bots.jev_bot import Jev

    jev = jev or Jev()

    def one(case: dict):
        qs = questions(variant, opening="opening" in case["expect"])
        qs = {k: v for k, v in qs.items() if k in case["expect"]}
        return case, jev.ask(case["state"], qs)

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, cases))
    score: dict[str, list[int]] = {}
    misses = []
    for case, answers in results:
        for qid, ok in case["expect"].items():
            got = ((answers or {}).get(qid) or {}).get("choice")
            right = got in ok
            s = score.setdefault(qid, [0, 0])
            s[0] += right
            s[1] += 1
            if not right:
                probs = ((answers or {}).get(qid) or {}).get("probabilities") or {}
                misses.append({"case": case["id"], "question": qid, "expected": ok, "got": got,
                               "p": {k: round(v, 2) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:3]},
                               "why": case.get("why", "")})
    total = sum(s[0] for s in score.values()), sum(s[1] for s in score.values())
    return {"score": score, "total": total, "misses": misses, "unanswered": sum(1 for _, a in results if not a)}


def report(result: dict) -> str:
    lines = [f"{q:<12}{r}/{n} right" for q, (r, n) in sorted(result["score"].items())]
    r, n = result["total"]
    lines.append(f"{'all':<12}{r}/{n} ({100 * r / max(1, n):.0f}%)")
    if result["unanswered"]:
        lines.append(f"WARNING: {result['unanswered']} cases got no answer (no key, or Jev failing)")
    for m in result["misses"]:
        lines.append(f"  miss {m['case']} {m['question']}: wanted {'/'.join(m['expected'])}, got {m['got']} {m['p']}"
                     + (f"  ({m['why']})" if m["why"] else ""))
    return "\n".join(lines)


def case_from_log(rows: list[dict], seconds: float, expect: dict[str, list[str]], *, game: str, why: str = "") -> dict:
    call = min(rows, key=lambda r: abs(r["t"] - seconds))
    return {"id": f"{game[4:12] if game.startswith('lab-') else game[:8]}@{int(call['t'])}", "game": game,
            "t": call["t"], "state": call["state"], "expect": expect, "why": why,
            "bot_said": call.get("decision", "")}
