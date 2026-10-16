"""jev-lsj: the last stand where Jev decides *when* to bank the lead (bots/jev/laststand.py does the rest).

The rules version banks at a set time when the score is ahead. Here the strategist's question is
asked in words every ``ask_every`` seconds from ``ask_from``: "sell the whole base now to keep the
lead, or wait?" Jev reads what code counts (the clock, the score, the base's health, strikes seen,
aircraft seen, the army, the spare MCV); code applies the answer behind the same gates (a spare MCV
alive, the score at least ``cashout_net``). Without an answer the base is kept: no Jev, no banking.

This is the Jev side of the experiment (docs/JEV_LOOPS.md: "Jev adds nothing over rules so far").
The control is ``jev-ls4*``: the same bot with a timer. Paid calls: about 25 a game.
"""

from __future__ import annotations

import time
from typing import Any

from bots.jev_bot import Jev
from .laststand import SOVIET, LastStandBot, _cell, _dist
from .v10.jev_bot import AIR, NAMES, NONCOMBAT, _clock, _mix

MODEL = "jev-1.13.0"  # pinned, as the council's

BRIEFING = [
    "The game ends at 20:00 and is decided by kills minus losses in credits (the score). A building that is sold is not a loss; a building that is destroyed is.",
    "From about 14:00 the computer opponents fire superweapons at our base: a nuclear missile (any group of buildings worth 3,000 or more) and bombers. One hit destroys most of the base and the army near it within seconds, which turns a lead into a loss. It most often comes between 16:30 and 19:30. It is not announced.",
    "If the whole base is sold first, nothing can be destroyed and the lead is kept. A spare MCV hiding on the map keeps us in the game, but we can no longer build, repair or train, and the opponent may hunt down the spare MCV in two to three minutes, which would lose the game.",
    "So selling is right when we lead clearly and the time left is short enough that the opponent cannot hunt the MCV down (about 3 minutes or less), or when a strike has already shown the opponent has superweapons and the lead is large. Selling is wrong when the lead is small or lost, when the base is calm and much time is left, or when no spare MCV is alive.",
]
CRITERIA = {
    "bank_now": "Sell the whole base now to keep the lead. Right when we lead clearly, a spare MCV is alive, and the strike could come any moment, or the time left is about three minutes or less",
    "wait": "Keep the base and keep playing. Right when the lead is small or lost, no spare MCV is alive, or it is still early and nothing has struck yet",
}


class LastStandJevBot(LastStandBot):
    def __init__(self, *, ask_from: float = 840.0, ask_every: float = 15.0, bank_gate: float = 0.6,
                 use_jev: bool = True, model: str = MODEL, **kwargs) -> None:
        """``bank_gate``: the probability "bank_now" needs. ``use_jev=False``: never asks, never banks."""
        kwargs.setdefault("oracle", "rules")  # (the council's own questions stay with code: only the bank decision is Jev's)
        kwargs.pop("cashout_at", None)
        super().__init__(**kwargs)
        self.ask_from, self.ask_every, self.bank_gate = ask_from, ask_every, bank_gate
        self.bank_jev = Jev(model=model)
        if not use_jev:
            self.bank_jev.key = None
        self.last_ask = -1e9
        self.bank_calls = 0
        self.seen_air: dict[str, int] = {}
        self.strikes: list[str] = []
        self.first_seen_air: dict[str, float] = {}

    def _last_stand(self, raw: dict, obs: dict, now: float, context, inner: list[dict]):
        self._watch(obs, now)
        mcvs = [u for u in obs.get("units") or [] if str(u.get("type") or "").lower() == "mcv" and not u.get("wreck")] if now > 30 else []
        if (not self.stand and self.cashout_at is None and now >= self.ask_from and now - self.last_ask >= self.ask_every
                and mcvs and obs.get("buildings")):
            self.last_ask = now
            if self._ask_bank(raw, obs, now, context):
                self.cashout_at = now
        return super()._last_stand(raw, obs, now, context, inner)

    # -- what Jev is told ---------------------------------------------------------------------

    def _watch(self, obs: dict, now: float) -> None:
        for e in obs.get("visible_enemies") or []:
            t = str(e.get("type") or "").lower()
            if t in AIR or t in ("u2", "badr", "badr.bomber"):
                self.seen_air[t] = self.seen_air.get(t, 0) + 1
                self.first_seen_air.setdefault(t, now)
        if len(self.destroyed_at) >= 4 and (not self.strikes or now - self._strike_time > 60):
            self._strike_time = now
            self.strikes.append(f"at {_clock(now)} {len(self.destroyed_at)} buildings were destroyed within seconds")
            del self.strikes[:-3]

    _strike_time = -1e9

    def describe_bank(self, raw: dict, obs: dict, now: float) -> dict[str, Any]:
        mil = obs.get("military") or {}
        net = int(mil.get("kills_cost") or 0) - int(mil.get("deaths_cost") or 0)
        remaining = (raw.get("time") or {}).get("remaining_seconds")
        left = float(remaining) if isinstance(remaining, (int, float)) else 1200.0 - now
        buildings = obs.get("buildings") or []
        costs = self.bot._costs
        worth = sum(int(costs.get(str(b.get("type") or "").lower(), 500)) for b in buildings)
        hurt = sum(1 for b in buildings if int(b.get("hp") or 100) < 50)
        army: dict[str, int] = {}
        for u in obs.get("units") or []:
            t = str(u.get("type") or "").lower()
            if not u.get("wreck") and t not in NONCOMBAT | {"mcv", "harv", "tecn", "tecn2", "c10"}:
                army[t] = army.get(t, 0) + 1
        n = sum(army.values())
        faction = str(obs.get("enemy_faction") or "").lower()
        air = ", ".join(f"{NAMES.get(t, t)} seen {c} times" for t, c in sorted(self.seen_air.items())) or "no aircraft seen"
        mcvs = [u for u in obs.get("units") or [] if str(u.get("type") or "").lower() == "mcv"]
        return {
            "clock": f"{_clock(now)} played, {_clock(left)} left",
            "score": ("kills minus losses: even" if net == 0 else
                      f"kills minus losses: we are ahead by {net} credits" if net > 0 else
                      f"kills minus losses: we are behind by {-net} credits"),
            "opponent": f"a computer opponent of the {'Soviet' if faction in SOVIET else 'Allied'} side ({faction or 'unknown'}); {air}",
            "strikes_so_far": "; ".join(self.strikes) or "none: no sudden mass loss has happened yet",
            "our_base": f"{len(buildings)} buildings worth about {worth} credits, {hurt} of them below half health",
            "our_army": f"{n} combat units ({_mix(army)})" if n else "no combat units",
            "spare_mcv": f"{len(mcvs)} alive, hiding on the map" if mcvs else "none alive",
        }

    def _ask_bank(self, raw: dict, obs: dict, now: float, context) -> bool:
        state = {"briefing": BRIEFING, **self.describe_bank(raw, obs, now)}
        questions = {"bank": {"type": "choice", "instructions": "Using `briefing`, should we sell the whole base now to keep the lead, or wait?",
                              "criteria": CRITERIA}}
        start = time.monotonic()
        answers, tokens = self.bank_jev.ask_counted(state, questions)
        latency = time.monotonic() - start
        self.bank_calls += 1
        self.jev.last_tokens = tokens
        pick = (answers or {}).get("bank") or {}
        p = float((pick.get("probabilities") or {}).get("bank_now", 0.0)) if pick.get("choice") else 0.0
        bank = pick.get("choice") == "bank_now" and p >= self.bank_gate
        self.note = f"bank? {pick.get('choice')} ({p:.2f})" + (" -> BANK" if bank else "")
        self._log(context, now, state, answers, latency, {"bank": bank})
        return bank


def create_agent(**kwargs) -> LastStandJevBot:
    return LastStandJevBot(**kwargs)
