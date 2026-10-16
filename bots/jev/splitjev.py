"""jev-sj: the council's quartermaster loop, answered by Jev next to the rules (experiment jev-comp).

Every other loop (the strategist, the field commander) is answered by code, as in the champion. Only the
production question ("what should our factories train now?") goes to Jev as well, with a briefing that
says what the matchup model says (riflemen against infantry, rocket soldiers against tanks and
aircraft) but not the thresholds, and with the enemy mix counted in words by code (``enemy_mix``).

``supply_mode``:
- ``"shadow"``: the rules decide, Jev's answer is only logged next to them, call by call. The game is the
  rules bot's game (the same seeds play the same), and ``agreement()`` says how often Jev chose alike.
- ``"jev"``: Jev decides when it answers with at least ``supply_gate`` probability; otherwise the rules.

The rules side is ``jev-rifr`` (``rifle_rule``): riflemen at 60%+ infantry among the enemy units seen in the
last 90 s, rocket soldiers at 35%-, else mixed. Paid calls: about 60 a game (a call every 20 s).
"""

from __future__ import annotations

import time
from typing import Any

from bots.jev_bot import Jev
from .v10.bot import CouncilBot

MODEL = "jev-1.13.0"  # pinned, as the council's

BRIEF = [
    "Rifle infantry (100 credits) are by far the best value against enemy infantry, and in numbers they also beat light vehicles; they cannot hit aircraft and do little against tanks.",
    "Rocket infantry (300 credits) are the best value against tanks and aircraft, and a poor value against infantry: a crowd of riflemen beats them at equal cost.",
    "Tanks and artillery spend money fastest and support the infantry; artillery is strong against crowds of infantry.",
    "Choose by what the enemy army is made of, as seen in the last minute and a half (`enemy_mix`): mostly infantry: rifles; mostly tanks or aircraft: rockets; a real mix of both: mixed. When too little has been seen, keep what we are training.",
    "Before 4:00 a rush of infantry may come: infantry are ready fastest.",
]


class SplitJevBot(CouncilBot):
    def __init__(self, *, supply_mode: str = "shadow", supply_gate: float = 0.55, model: str = MODEL, **kwargs) -> None:
        kwargs.setdefault("oracle", "rules")  # (the other loops stay with code)
        kwargs.setdefault("rifle_rule", True)
        kwargs.setdefault("blind_production", "rifles")
        super().__init__(**kwargs)
        self.supply_brief = BRIEF
        self.supply_mode, self.supply_gate = supply_mode, supply_gate
        self.supply_jev = Jev(model=model)
        self.compare: list[dict[str, Any]] = []

    def _decide(self, raw, facts, now, context):
        self._ctx = context  # (the supply calls are made in threads: they log under this game)
        return super()._decide(raw, facts, now, context)

    def _ask(self, state: dict, questions: dict) -> tuple[dict | None, int]:
        if "production" not in questions:
            return super()._ask(state, questions)
        rules, _ = super()._ask(state, questions)
        start = time.monotonic()
        jev, tokens = self.supply_jev.ask_counted(state, questions)
        latency = time.monotonic() - start
        rules_pick = ((rules or {}).get("production") or {}).get("choice")
        jev_pick = ((jev or {}).get("production") or {}).get("choice")
        p = float((((jev or {}).get("production") or {}).get("probabilities") or {}).get(jev_pick, 0.0)) if jev_pick else 0.0
        self.compare.append({"t": state.get("clock"), "rules": rules_pick, "jev": jev_pick, "p": round(p, 2),
                             "mix": state.get("enemy_mix"), "latency": round(latency, 2)})
        del self.compare[:-400]
        self._log_supply(state, rules_pick, jev_pick, p, latency, tokens, jev)
        if self.supply_mode == "jev" and jev_pick and p >= self.supply_gate:
            return jev, tokens
        return rules, tokens

    def _log_supply(self, state: dict, rules_pick, jev_pick, p: float, latency: float, tokens: int, jev: dict | None) -> None:
        """One line a call in the Jev log (``lab jev GAME``): the state, both answers."""
        try:
            self.jev.last_tokens = tokens
            now = getattr(self, "_now", (None, 0.0, None))[1] or 0.0
            self._log(getattr(self, "_ctx", None), now, state, jev, latency,
                      {"loop": "supply", "rules": rules_pick, "jev": jev_pick, "agree": rules_pick == jev_pick})
        except Exception:  # noqa: BLE001  (logging never costs a game)
            pass

    def agreement(self) -> tuple[int, int]:
        """(calls where Jev answered, of them the calls where it chose what the rules chose)."""
        answered = [c for c in self.compare if c["jev"]]
        return len(answered), sum(1 for c in answered if c["jev"] == c["rules"])


def create_agent(**kwargs) -> SplitJevBot:
    return SplitJevBot(**kwargs)
