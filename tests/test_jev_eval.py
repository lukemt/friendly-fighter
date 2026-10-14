"""lab/jev_eval.py without the network."""

from __future__ import annotations

from lab import jev_eval


class Fake:
    def __init__(self, picks):
        self.picks, self.seen = picks, []

    def ask(self, state, questions):
        self.seen.append(questions)
        return {q: {"choice": self.picks[q], "probabilities": {self.picks[q]: 0.9}} for q in questions}


CASES = [
    {"id": "a", "state": {"clock": "8:00"}, "expect": {"posture": ["defend", "gather"]}, "why": "ahead"},
    {"id": "b", "state": {"clock": "8:30"}, "expect": {"posture": ["commit"], "opening": ["guard"]}, "why": "behind"},
]


def test_cases_are_scored_and_misses_listed():
    result = jev_eval.evaluate(CASES, jev=Fake({"posture": "defend", "opening": "guard"}), workers=1)
    assert result["score"] == {"posture": [1, 2], "opening": [1, 1]}
    assert result["total"] == (2, 3)
    assert [(m["case"], m["got"]) for m in result["misses"]] == [("b", "defend")]
    assert "miss b posture: wanted commit, got defend" in jev_eval.report(result)


def test_only_the_expected_questions_are_asked_and_a_variant_replaces_text():
    fake = Fake({"posture": "commit", "opening": "guard"})
    variant = {"playbook": ["only this"], "criteria": {"posture": {"commit": "x", "defend": "y"}}}
    jev_eval.evaluate(CASES, variant, jev=fake, workers=1)
    asked = {k for qs in fake.seen for k in qs}
    assert asked == {"posture", "opening"}
    posture = next(qs["posture"] for qs in fake.seen if "posture" in qs)
    assert posture["criteria"] == {"commit": "x", "defend": "y"}
    assert posture["instructions"]["playbook"] == ["only this"]


def test_the_shipped_cases_are_well_formed():
    from bots.jev_bot import OPENING_QUESTION, QUESTIONS

    options = {q: set(v["criteria"]) for q, v in {**QUESTIONS, "opening": OPENING_QUESTION}.items()}
    cases = jev_eval.load_cases()
    assert cases and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        for q, ok in c["expect"].items():
            assert ok and set(ok) <= options[q], (c["id"], q, ok)
