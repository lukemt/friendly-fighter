# Frozen from bots.jev.v10.bot (jev-rifr jev-rifr@1a62cf87) at 679dc78+changes.
# Frozen from bots.jev_council (ff-council-rules-eco ff-council-rules-eco@4023a564) at 7036a80+changes.
"""ff-jev-council: the Jev bot as three decision loops, each with its own briefing, its own view of the
game and its own pace, instead of one call that asks everything with the whole playbook.

TypeSafe's docs (docs.typesafe.ai) say how loops combine: questions that share a state go in one
request (the state is read and billed once, each question costs only its own tokens, and every
question is answered independently); a second request is right only when one answer is needed to
build the next request. A strategist's plan is such an answer: the field commander has to know the
plan before it can judge a fight. So:

- **strategist** (every 30 s, and at once when something new happens: a raid starts or ends, a new
  enemy unit type or building shows up, an attack ends): the whole game so far (score, everything
  seen, a memory of events) and the strategic rules. Asks: who is the opponent, which plan
  (hold / boom / fortify / attack), will an attack come soon, which opening.
- **quartermaster** (every 20 s, and at once on a new enemy unit type): our army and theirs, the
  counters. Asks: what to train. Options that cannot be right now (infantry-only after 4:00) are
  not offered at all (code builds the options).
- **field commander** (only when there is something to command: a fight away from home, an army
  out, or a plan to attack; every 3 s while fighting): the fight in words, the plan, the fighting
  rules. Asks: defend / gather / commit / retreat.

The rules live once in each request's state (``briefing``) and the questions point at it, so no
rule is sent twice. Answers are gated on confidence (the docs' confidence-gated routing): a risky
change (to "attack" or "boom") needs a clear answer; the rails of bots/jev_bot.py still stand
behind every posture. The plan becomes knobs here: ``boom`` = extra refineries (greed), ``fortify``
= no greed and up to 4 defenses.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .jev_bot import AIR, GREED, NONCOMBAT, JevBot, _clock

MODEL = "jev-1.13.0"  # pinned: an alias (jev-latest) moves with new releases and changes answers
STRATEGY_SECONDS = 30.0
SUPPLY_SECONDS = 20.0
FIELD_SECONDS = 10.0
MEMORY = 10  # events the strategist remembers

STRATEGY_BRIEF = [
    "The game ends at 10:00. It is decided by kills minus losses in credits; when that is even, the side with more assets (the cost of its units and buildings; unspent money counts for nothing) wins.",
    "A rusher attacks our base at 3:00-4:10 with about 20 infantry, then sends more waves; a defense and 10 infantry at home beat it.",
    "Most opponents attack sooner or later and lose units to our defense; defending is how we win the score.",
    "An opponent whose army we have never seen after 5:00 is not harmless: it is massing an army, often tanks, and usually comes before 7:00. One that has not come by 7:00 is staying home: the game will end in a standoff.",
    "A refinery comes with a free harvester: it adds more to our assets than it costs. But money spent on the economy is not spent on the army, and against an opponent that attacks that can cost the base.",
    "Against a turtle (a base full of defenses) or a passive opponent that never attacks, nobody fights: the bigger economy wins on assets.",
    "Attacking is right only when clearly behind on score (by 500 credits or more) late in the game with a big army, or against a passive opponent with no army and no defenses.",
    "When ahead on score, never attack: defend the lead.",
]
SUPPLY_BRIEF = [
    "Rocket infantry beat tanks and aircraft; rifle infantry cannot hit aircraft and do little to tanks.",
    "Rifle infantry are cheap and quick against infantry; tanks beat infantry and spend money fastest.",
    "An enemy army never seen after 5:00 is usually built around tanks: rocket infantry are the safe answer.",
    "Before 4:00 a rush of infantry may come: infantry are ready fastest.",
]
FIELD_BRIEF = [
    "Every unit that dies costs its full price on the score: losses decide games.",
    "Attacking into defenses (tesla coils, flame towers, pillboxes, turrets) loses; never attack a fortified base (4 or more known defenses).",
    "A big army committed all at once breaks an army; units sent in small groups die one by one.",
    "An attack that is losing more than it kills, or that runs into defenses, must fall back at once; one that is winning its fight should finish it.",
    "Attack only when clearly behind on score (by 500 credits or more) with a big army (25 or more units) late in the game, or against a passive opponent; when ahead, defend the lead.",
]

PLAN = {
    "hold": "Defend at home with a normal economy, money into the army and a couple of defenses. Right early in the game before we know the opponent, and against a rusher or an aggressive opponent",
    "boom": "Defend at home and build extra refineries and harvesters. Right when the opponent is a turtle, fortified or passive, or when it is past 7:00 and no enemy army has come to our base all game (a standoff: the assets decide)",
    "fortify": "No extra economy: every credit into the army and up to 4 defenses. Right when an attack is expected soon: an enemy army was seen coming or growing, a raid was just beaten off and more will come, or it is between 5:00 and 7:00 and the enemy army has never been seen",
    "attack": "Gather a big army and attack where no defenses stand. Right only when clearly behind on score (by 500 credits or more) late in the game with a big army, or against a passive opponent with no army and no defenses",
}
OPPONENT = {
    "rusher": "Attacks our base early (before about 4:30) with a big wave of infantry, and keeps sending waves",
    "turtle": "Builds many defenses (pillboxes, turrets, tesla coils, flame towers, mines), maybe aircraft; rarely attacks; a fortified base",
    "fortified": "Fortifies its ore field, keeps a big reaction army of infantry at home, attacks rarely and late",
    "aggressive": "Sends attack waves at our base again and again through the middle of the game",
    "passive": "Has no army and no defenses at all: we have seen its base and nothing there fights",
    "unknown": "Too early or too little seen to tell",
}
POSTURE = {
    "defend": "Stay at home and kill whatever comes. Right when ahead on score, when even against an opponent that has an army or defenses, against a rusher or an aggressive opponent, or when the army is small",
    "gather": "Build one big group at the staging point outside our base without attacking. Right when the plan is to attack but the army is not big enough yet",
    "commit": "Attack with everything at once where no defenses stand. Right against a passive opponent, or when clearly behind on score (by 500 credits or more) late in the game with a big army; never into a fortified base",
    "retreat": "Pull the army back home now. Right when our army out of the base is losing more than it kills, is under fire from enemy defenses, or is heading into a fortified base",
}
PRODUCTION = {
    "infantry": "Rifle and rocket infantry only: right against an early rush",
    "rockets": "Infantry are all rocket infantry, plus tanks: right when enemy tanks or aircraft have been seen, or when it is past 5:00 and the enemy army has never been seen",
    "vehicles": "Tanks first: right when money is piling up unspent and the enemy army is mostly infantry",
    "mixed": "Rifle and rocket infantry and tanks: the default; right when the enemy army has not been seen yet and it is before 5:00, or when it is a mix of infantry and tanks",
    "rifles": "Rifle infantry only, plus tanks and artillery: right when most of the enemy army is infantry, because rocket infantry are poor against infantry",
}
OPENING = {
    "guard": "A defense and about 10 infantry at home first: safe against an early rush, slower economy",
    "economy": "Refineries and harvesters first: stronger later, but wiped out by an early rush",
}

# Which parts of the shared description (JevBot.describe) each loop sees.
VIEWS = {
    "strategy": ("clock", "score", "our_army", "enemy_army", "enemy_defenses", "what_we_know_of_the_enemy",
                 "enemy_aircraft", "raids_on_our_base", "now_at_our_base", "economy", "our_past_attacks", "scouting",
                 "our_read_so_far"),
    "supply": ("clock", "our_army", "enemy_army", "what_we_know_of_the_enemy", "enemy_aircraft", "economy", "enemy_mix"),
    "field": ("clock", "score", "recent_trades", "our_army", "enemy_army", "enemy_defenses", "now_at_our_base",
              "our_current_orders", "our_attack", "our_past_attacks", "scouting", "our_read_so_far"),
}


def loop_request(loop: str, words: dict[str, str], *, seconds: float, plan: str = "hold", events: list[str] | None = None,
                 read: str | None = None, rifles: bool = False, supply_brief: list[str] | None = None) -> tuple[dict, dict]:
    """One loop's (state, questions), from the shared description: also used by ``lab jev eval``."""
    state: dict[str, Any] = {k: words[k] for k in VIEWS[loop] if k in words}
    plan_text = f"{plan}: {PLAN[plan].split('. Right')[0].lower()}"
    if loop == "strategy":
        state = {"briefing": STRATEGY_BRIEF, **state, "what_happened": events or ["nothing yet"],
                 "current_plan": plan_text}
        if read:
            state["our_read_so_far"] = read
        questions: dict[str, Any] = {
            "opponent": {"type": "choice", "instructions": "Using `briefing`, which kind of opponent are we playing, judging by everything seen so far?",
                         "criteria": OPPONENT},
            "plan": {"type": "choice", "instructions": "Using `briefing`, which plan should we follow for the next minute?",
                     "criteria": PLAN},
            "attack_soon": {"type": "noul",
                            "instructions": "Will an enemy army attack our base within the next two minutes, judging by `what_happened`, `enemy_army` and `clock`?"},
        }
        if seconds < 240:
            questions["opening"] = {"type": "choice", "instructions": "Using `briefing`, which opening should we play in the first 6 minutes?",
                                    "criteria": OPENING}
        return state, questions
    if loop == "supply":
        state = {"briefing": supply_brief or SUPPLY_BRIEF, **state, "current_plan": plan_text}
        options = ("infantry", "rockets", "mixed") if seconds < 240 else ("rockets", "vehicles", "mixed") + (("rifles",) if rifles else ())
        return state, {"production": {"type": "choice", "instructions": "Using `briefing`, what should our factories train now?",
                                      "criteria": {k: PRODUCTION[k] for k in options}}}
    if loop == "field":
        state = {"briefing": FIELD_BRIEF, **state, "current_plan": plan_text}
        if read:
            state["our_read_so_far"] = read
        return state, {"posture": {"type": "choice", "instructions": "Using `briefing` and `current_plan`, what should our army do for the next 10 seconds?",
                                   "criteria": POSTURE}}
    raise KeyError(loop)


class CouncilBot(JevBot):
    def __init__(self, *, log_dir: str | None = None, model: str = MODEL, push: bool = True, scout: bool = False,
                 plan: str = "hold", gate: float = 0.55, attack_gate: float = 0.6, use_jev: bool = True,
                 boom_until: float = 600.0, keep_first: float = 0.0, oracle: str = "jev",
                 blind_production: str = "rockets", rifle_rule: bool = False, rifle_hi: float = 0.6, rifle_lo: float = 0.35,
                 tanya_rule: bool = False, greed_hold: int = 0, **kwargs) -> None:
        """``gate``: the probability a new plan needs; ``attack_gate``: the same for attack and boom.
        ``boom_until``: no boom from this time; ``keep_first``: the first plan stands until then unless
        the base is raided or an attack is expected (for experiments). ``oracle="rules"``: code answers
        the loops' questions (``Rules``), no Jev at all."""
        super().__init__(log_dir=log_dir, model=model, push=push, scout=scout, use_jev=use_jev, **kwargs)
        if oracle == "rules":
            self.jev = Rules(self)
        self.supply_brief: list[str] | None = None  # (jev family: another briefing for the quartermaster, see bots/jev/splitjev.py)
        self.blind_production = blind_production  # (jev family: what the rules train while the enemy army is unseen; v10: rockets)
        self.rifle_rule, self.rifle_hi, self.rifle_lo = rifle_rule, rifle_hi, rifle_lo
        self.tanya_rule = tanya_rule  # (jev family: Tanya seen -> tanks, which she cannot hurt)
        self.greed_hold = greed_hold  # (jev family: the greed of every plan but boom and fortify; v10: 0, so one raid ends the boom for good)
        self.plan = plan
        self.plan_since = 0.0
        self.gate, self.attack_gate = gate, attack_gate
        self.boom_until = boom_until
        self.keep_first = keep_first
        self.attack_soon = 0.0
        self.events: list[str] = []
        self.next_due = {"strategy": 0.0, "supply": 0.0, "field": 0.0}
        self.known_types: set[str] = set()
        self.known_buildings = False
        self.raid_marks: tuple[float, int, int] | None = None  # (start, kills, losses) of the raid under way
        self.attacks_seen = 0
        self.loop_calls = {"strategy": 0, "supply": 0, "field": 0}
        self.loop_tokens = {"strategy": 0, "supply": 0, "field": 0}
        self.pool = ThreadPoolExecutor(3)
        self._knobs_for_plan()

    # -- memory: what happened, written by code -------------------------------------------------

    def _remember(self, facts: dict, now: float) -> set[str]:
        """New events since the last call; the loops they wake up."""
        wake: set[str] = set()
        def note(text: str, *loops: str) -> None:
            self.events.append(f"{_clock(now)} {text}")
            del self.events[:-MEMORY]
            wake.update(loops)

        if facts["threat"] and self.raid_marks is None:
            self.raid_marks = (now, facts["kills"], facts["losses"])
            note(f"{len(facts['at_base'])} enemy units came to our base", "strategy", "supply")
        elif not facts["threat"] and self.raid_marks is not None:
            k, l = facts["kills"] - self.raid_marks[1], facts["losses"] - self.raid_marks[2]
            note(f"the raid ended: we killed {k} and lost {l} credits worth", "strategy")
            self.raid_marks = None
        new = {str(e.get("type") or "").lower() for e in facts["enemies"]} - self.known_types - NONCOMBAT
        if new:
            self.known_types |= new
            names = ", ".join(sorted(new))
            note(f"first saw enemy {names}" + (" (aircraft)" if new & AIR else ""), "strategy", "supply")
        if self.enemy_buildings and not self.known_buildings:
            self.known_buildings = True
            note("first saw enemy buildings", "strategy")
        done = [a for a in self.attacks if a.get("result")]
        if len(done) > self.attacks_seen:
            self.attacks_seen = len(done)
            a = done[-1]
            note(f"our attack ended: it killed {a['killed']} and lost {a['lost']} credits worth", "strategy")
        return wake

    # -- the loops ------------------------------------------------------------------------------

    def _field_needed(self, facts: dict) -> bool:
        return bool(facts["fight"] or facts["away"] or self.posture != "defend" or self.plan == "attack")

    def _read(self) -> str | None:
        if not self.belief:
            return None
        best = max(self.belief, key=self.belief.get)
        return f"'{best}' (about {round(self.belief[best] * 10) * 10}% sure)"

    def _decide(self, raw: dict, facts: dict, now: float, context) -> list[dict[str, Any]]:
        self._now = (facts, now, raw)
        words = self.describe(raw, facts, now)
        wake = self._remember(facts, now)
        busy = facts["fight"] or self.posture == "commit"
        due = []
        if now >= self.next_due["strategy"] or "strategy" in wake:
            due.append("strategy")
        if now >= self.next_due["supply"] or "supply" in wake:
            due.append("supply")
        if self._field_needed(facts) and (busy or now >= self.next_due["field"]):
            due.append("field")
        if self.force:
            extra = self._apply({}, facts, now, raw)
            self._log(context, now, words, None, 0.0)
            return extra
        if not due:
            return self._apply({}, facts, now, raw)
        answers, spent, latency = self._run(due, words, now)
        plan = self.plan
        self._take_plan(answers, facts, now)
        if "field" not in due and self._field_needed(facts) and self.plan != plan:
            # (the new plan needs the field commander now: the one real dependency between the loops,
            # so a second request, built from the first one's answer)
            more, n, wait = self._run(["field"], words, now)
            answers.update(more)
            spent.update(n)
            latency += wait
        extra = self._apply(answers, facts, now, raw)
        self._log_council(context, now, words, answers, latency, spent)
        return extra

    def _run(self, loops: list[str], words: dict, now: float) -> tuple[dict, dict[str, int], float]:
        """The loops' requests, all at once (one thread each): the answers together, each loop's
        tokens, the wall time."""
        requests = {loop: loop_request(loop, words, seconds=now, plan=self.plan, events=self.events, read=self._read(),
                                       rifles=self.rifle_rule or self.blind_production == "rifles", supply_brief=self.supply_brief)
                    for loop in loops}
        start = time.monotonic()
        futures = {loop: self.pool.submit(self._ask, state, questions) for loop, (state, questions) in requests.items()}
        results = {loop: f.result() for loop, f in futures.items()}
        answers: dict[str, Any] = {}
        spent: dict[str, int] = {}
        for loop, (got, n) in results.items():
            spent[loop] = n
            if got is None:
                continue
            self.next_due[loop] = now + {"strategy": STRATEGY_SECONDS, "supply": SUPPLY_SECONDS, "field": FIELD_SECONDS}[loop]
            self.loop_calls[loop] += 1
            self.loop_tokens[loop] += n
            answers.update(got)
        return answers, spent, time.monotonic() - start

    def _ask(self, state: dict, questions: dict) -> tuple[dict | None, int]:
        counted = getattr(self.jev, "ask_counted", None)
        if counted is not None:
            return counted(state, questions)
        return self.jev.ask(state, questions), 0

    def _take_plan(self, answers: dict, facts: dict, now: float) -> None:
        soon = answers.get("attack_soon")
        if isinstance(soon, dict) and isinstance(soon.get("noul"), (int, float)):
            self.attack_soon = float(soon["noul"])
        a = answers.get("plan") or {}
        pick = a.get("choice")
        if pick not in PLAN or pick == self.plan:
            return
        if now < self.keep_first and not (self.raids or self.attack_soon >= 0.5):
            return
        p = float((a.get("probabilities") or {}).get(pick, 0.0))
        if p < (self.attack_gate if pick in ("attack", "boom") else self.gate):
            return
        if pick == "boom" and (facts["threat"] or self.attack_soon >= 0.5 or now >= self.boom_until):
            return  # (no greed with an attack expected: the forecast vetoes the plan)
        self.plan, self.plan_since = pick, now
        self._knobs_for_plan()

    def _knobs_for_plan(self) -> None:
        self.greed = GREED if self.plan == "boom" else (self.greed_hold if self.plan != "fortify" else 0)
        self.defenses = 4 if self.plan == "fortify" else 2

    def _apply(self, answers: dict, facts: dict, now: float, raw: dict) -> list[dict[str, Any]]:
        extra = super()._apply(answers, facts, now, raw)
        self.note = f"plan={self.plan} " + self.note + (f" soon={self.attack_soon:.2f}" if self.attack_soon else "")
        return extra

    def _log_council(self, context, now, words, answers, latency, spent: dict[str, int]) -> None:
        self.jev.last_tokens = sum(spent.values())  # (the row's tokens: every loop's request together)
        self._log(context, now, words, answers or None, latency, {"loops": spent, "plan": self.plan})


VEHICLES = {"1tnk", "2tnk", "3tnk", "4tnk", "ttnk", "v2rl", "arty", "apc", "jeep", "ftrk", "mnly"}
INFANTRY = {"e1", "e2", "e3", "e4", "e6", "e7", "dog", "shok", "spy", "thf", "medi", "mech", "tecn", "tecn2"}


class Rules:
    """The council's questions answered by code from the counted facts, following the briefings'
    rules as written: the control that shows what Jev adds, and a council that needs no API."""

    key = "rules"
    last_tokens = 0
    calls = errors = tokens = 0

    def __init__(self, bot: CouncilBot) -> None:
        self.bot = bot

    def ask(self, state, questions):
        return self.ask_counted(state, questions)[0]

    def ask_counted(self, state, questions) -> tuple[dict, int]:
        facts, now, raw = self.bot._now
        out: dict[str, Any] = {}
        for qid, q in questions.items():
            pick = getattr(self, qid)(facts, now, raw)
            if q["type"] == "noul":
                out[qid] = {"type": "noul", "noul": pick}
                continue
            options = list(q["criteria"])
            pick = pick if pick in options else options[-1]
            rest = 0.1 / max(1, len(options) - 1)
            out[qid] = {"type": "choice", "choice": pick, "probabilities": {o: 0.9 if o == pick else rest for o in options}}
        return out, 0

    # -- what the briefings say, in code ----------------------------------------------------

    def _seen(self) -> set[str]:
        return {t for t, _ in self.bot.enemy_seen.values()} - NONCOMBAT

    def _raid_just_beaten(self, now: float) -> bool:
        b = self.bot
        return b.raids >= 1 and now - b.last_raid_end < 60

    def opponent(self, facts, now, raw) -> str:
        b = self.bot
        if b.first_raid and b.first_raid[0] < 270 and b.first_raid[1] >= 6:
            return "rusher"
        if b.raids >= 2:
            return "aggressive"
        if len(b.defense_cells) >= 4:
            return "turtle"
        if b.enemy_buildings and not self._seen() and not b.defense_cells:
            return "passive"
        if now >= 420 and b.raids == 0:
            return "turtle"  # (not come by 7:00: staying home)
        return "unknown"

    def plan(self, facts, now, raw) -> str:
        b = self.bot
        net = facts["kills"] - facts["losses"]
        left = (raw.get("time") or {}).get("remaining_seconds")
        left = left if isinstance(left, (int, float)) else 600 - now
        opp = self.opponent(facts, now, raw)
        if opp == "passive" or (net <= -500 and left < 180 and len(facts["combat"]) >= 25):
            return "attack"
        if facts["threat"] or (self._raid_just_beaten(now) and opp in ("rusher", "aggressive")):
            return "fortify"
        if 300 <= now < 420 and b.enemy_max_army == 0:
            return "fortify"
        if opp in ("turtle", "fortified") or (now >= 420 and b.raids == 0) or (now < 240 and b.raids == 0):
            return "boom"
        return "hold"

    def attack_soon(self, facts, now, raw) -> float:
        b = self.bot
        expected = (300 <= now < 420 and b.enemy_max_army == 0) or facts["threat"] or (
            self._raid_just_beaten(now) and self.opponent(facts, now, raw) in ("rusher", "aggressive"))
        return 0.8 if expected else 0.2

    def opening(self, facts, now, raw) -> str:
        return "guard"

    def production(self, facts, now, raw) -> str:
        b = self.bot
        seen = self._seen()
        if now < 240:
            return "infantry" if b.raids else "mixed"
        if b.tanya_rule and "e7" in seen:
            return "vehicles"
        if b.rifle_rule:
            share = self._infantry_share(now)
            if share is not None:
                if share >= b.rifle_hi:
                    return "rifles"
                if share <= b.rifle_lo and (seen & VEHICLES or b.enemy_air):
                    return "rockets"
                return "mixed"
        if seen & VEHICLES or b.enemy_air:
            return "rockets"
        if now >= 300 and b.enemy_max_army == 0:
            return b.blind_production
        cash = int(((raw.get("observation") or {}).get("economy") or {}).get("cash") or 0)
        if cash > 2000 and seen and not seen & VEHICLES:
            return "vehicles"
        return "mixed"

    def _infantry_share(self, now: float) -> float | None:
        """The infantry part of the enemy units seen in the last 90 s, or None when too few were seen."""
        recent = [t for t, seen in self.bot.enemy_seen.values() if now - seen <= 90 and t not in NONCOMBAT]
        if len(recent) < 8:
            return None
        return sum(1 for t in recent if t in INFANTRY) / len(recent)

    def posture(self, facts, now, raw) -> str:
        b = self.bot
        if facts["away"] and (facts["near_defenses"] or (facts["fight"] and 0 < b.odds < 0.8)):
            return "retreat"
        if b.plan == "attack":
            return "commit" if len(facts["combat"]) >= 15 else "gather"
        return "defend"


def create_agent(**kwargs) -> CouncilBot:
    return CouncilBot(**kwargs)
