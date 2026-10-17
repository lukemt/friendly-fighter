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


def _ask(jev, state, questions) -> tuple[dict | None, int]:
    counted = getattr(jev, "ask_counted", None)
    return counted(state, questions) if counted else (jev.ask(state, questions), 0)


def evaluate(cases: list[dict], variant: dict | None = None, *, jev=None, workers: int = 8) -> dict:
    """Ask every case; per question: right/asked, and each miss with what Jev said."""
    from bots.jev_bot import Jev

    jev = jev or Jev()

    def one(case: dict):
        qs = questions(variant, opening="opening" in case["expect"])
        qs = {k: v for k, v in qs.items() if k in case["expect"]}
        answers, tokens = _ask(jev, case["state"], qs)
        spent.append(tokens)
        return case, answers

    spent: list[int] = []
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
    return {"score": score, "total": total, "misses": misses, "unanswered": sum(1 for _, a in results if not a),
            "tokens": sum(spent) / max(1, len(spent))}


def _seconds(case: dict) -> float:
    clock = str((case.get("state") or {}).get("clock", "0:00")).split()[0]
    m, _, s = clock.partition(":")
    return int(m) * 60 + int(s or 0) if m.isdigit() else float(case.get("t") or 0)


# A council answer that stands for an old question: the economy is the strategist's plan.
COUNCIL_ECONOMY = {"greedy": {"boom"}, "safe": {"hold", "fortify", "attack"}}


def evaluate_council(cases: list[dict], *, jev=None, workers: int = 8) -> dict:
    """The same cases asked the way bots/jev_council.py asks: the strategist first, then the
    quartermaster and the field commander with the strategist's plan in their state (the real
    dependency between the loops). Scored like ``evaluate``, plus the tokens each case cost."""
    from bots.jev_bot import Jev
    from bots.jev_council import loop_request

    jev = jev or Jev()
    loop_of = {"opponent": "strategy", "opening": "strategy", "economy": "strategy", "production": "supply",
               "posture": "field"}

    def ask(loop, words, seconds, plan="hold"):
        state, qs = loop_request(loop, words, seconds=seconds, plan=plan)
        answers, tokens = _ask(jev, state, qs)
        return answers or {}, tokens

    def one(case: dict):
        seconds = _seconds(case)
        got, tokens = ask("strategy", case["state"], seconds)
        plan = (got.get("plan") or {}).get("choice") or "hold"
        answers = {k: v for k, v in got.items()}
        for loop in sorted({loop_of[q] for q in case["expect"]} - {"strategy"}):
            more, n = ask(loop, case["state"], seconds, plan)
            answers.update(more)
            tokens += n
        return case, answers, tokens, plan

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, cases))
    score: dict[str, list[int]] = {}
    misses = []
    for case, answers, _, plan in results:
        for qid, ok in case["expect"].items():
            if qid == "economy":
                got = plan
                right = bool(COUNCIL_ECONOMY.get(ok[0], set()) & {plan}) if len(ok) == 1 else plan in ok
                probs = (answers.get("plan") or {}).get("probabilities") or {}
            else:
                got = (answers.get(qid) or {}).get("choice")
                right = got in ok
                probs = (answers.get(qid) or {}).get("probabilities") or {}
            s = score.setdefault(qid, [0, 0])
            s[0] += right
            s[1] += 1
            if not right:
                misses.append({"case": case["id"], "question": qid, "expected": ok, "got": f"{got} (plan {plan})",
                               "p": {k: round(v, 2) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:3]},
                               "why": case.get("why", "")})
    total = sum(s[0] for s in score.values()), sum(s[1] for s in score.values())
    tokens = [t for _, _, t, _ in results]
    return {"score": score, "total": total, "misses": misses, "unanswered": sum(1 for _, a, _, _ in results if not a),
            "tokens": sum(tokens) / max(1, len(tokens))}


def report(result: dict) -> str:
    lines = [f"{q:<12}{r}/{n} right" for q, (r, n) in sorted(result["score"].items())]
    r, n = result["total"]
    lines.append(f"{'all':<12}{r}/{n} ({100 * r / max(1, n):.0f}%)"
                 + (f", {result['tokens']:.0f} tokens a case" if result.get("tokens") else ""))
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
