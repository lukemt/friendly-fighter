"""ff-jev: our scripted bot as the hands, the strategy Claude used to beat OpenRA's AIs by hand as a
few Jev decision loops (TypeSafe's System One model: typed answers with probabilities, ~0.4 s).

The split follows Jev's own advice ("keep code in control, give the model narrow decisions; numbers
stay in code"):

- ``RedAlertBot`` (agent/redalert_bot.py) does everything that needs no judgment: building,
  harvesting, training, placing, defending the base, running a wave.
- Every ``DECIDE_SECONDS`` of game time, and at once when an enemy reaches the base or the army
  meets one, this bot writes the game as a short text (``describe``: counts and sums done here and
  put into words) and asks Jev, in one call, the questions Claude asked itself while playing
  (``QUESTIONS``): who is this opponent, what should the army do, what should we build.
- Code turns the answers into the bot's knobs, with rails from docs/STRATEGY.md (no attack with a
  small army, hold the lead in the last minute) and some hysteresis.

Without ``TYPESAFE_API_KEY`` (or when Jev fails) it plays on with its last decisions; it starts as
``ff-guard-turtle``. Every call is logged as a line of JSON (state, answers, latency) under
``$FF_JEV_LOG`` (default ``sim/lab/jev/``), and its decision goes into the batch's reasoning.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from agent.redalert_bot import SURPLUS_CASH, Knobs, RedAlertBot, is_combat
from altruagent import WAIT, WithReasoning

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT_SECONDS = 6.0
DECIDE_SECONDS = 10.0           # a Jev call this often (game time)...
URGENT_GAP_SECONDS = 3.0        # ...and at once on a new threat or fight (then this often while it lasts)
SCOUT_SECONDS = (100, 280)      # one infantry looks at the enemy base at these times
TICKS = 25
HOME_RADIUS = 16                # enemies this close to home are at the base
FIGHT_RADIUS = 12               # enemies this close to the army's center are in the fight
COMMIT_MIN_ARMY = 15            # rails: no attack with fewer combat units than this...
COMMIT_MIN_SECONDS = 300        # ...or before 5:00 (unless the opponent is passive)
COMMIT_HOLD_SECONDS = 25        # once committed, only a retreat ends it before this
COMMIT_DEFICIT = 500            # "behind" enough to attack: by this much, or by anything near the end
COMMIT_LATE_SECONDS = 150
RECOMMIT_SECONDS = 60           # after a failed attack, no new one for this long
GREED = 2                       # the "greedy" economy: up to 4 refineries and 2 more harvesters
RETREAT_HOLD_SECONDS = 20
FINAL_SECONDS = 60
MAX_ORDERS = 20

NAMES = {
    "e1": "rifle infantry", "e2": "grenadier", "e3": "rocket infantry", "e4": "flamethrower",
    "e6": "engineer", "dog": "attack dog", "spy": "spy", "medi": "medic", "shok": "shock trooper",
    "1tnk": "light tank", "2tnk": "medium tank", "3tnk": "heavy tank", "4tnk": "mammoth tank",
    "v2rl": "V2 rocket launcher", "arty": "artillery", "jeep": "ranger jeep", "apc": "APC",
    "ftrk": "flak truck", "mnly": "minelayer", "ttnk": "tesla tank", "harv": "harvester", "mcv": "MCV",
    "heli": "Longbow helicopter", "hind": "Hind helicopter", "mh60": "Black Hawk helicopter",
    "yak": "Yak plane", "mig": "MiG plane", "tran": "transport helicopter", "badr": "bomber",
    "tsla": "tesla coil", "ftur": "flame tower", "gun": "turret", "pbox": "pillbox",
    "hbox": "camo pillbox", "sam": "SAM site", "agun": "AA gun", "gap": "gap generator",
    "minv": "mine", "minp": "mine",
}
AIR = {"heli", "hind", "mh60", "yak", "mig", "tran", "badr", "u2"}
DEFENSES = {"tsla", "ftur", "gun", "pbox", "hbox", "sam", "agun"}
NONCOMBAT = {"harv", "mcv", "e6", "spy", "medi", "mnly", "truk"}

# What Claude knew while playing (docs/STRATEGY.md), given to Jev with the questions.
PLAYBOOK = [
    "The game ends at 10:00 and is decided by kills minus losses in credits; a 0-0 draw is replayed, not lost.",
    "Attacking into defenses (tesla coils, flame towers, pillboxes, turrets) loses: every unit that dies costs its full price.",
    "Against a passive opponent: lose nothing and kill what comes; one kill with no losses wins.",
    "A rusher attacks our base at 3:00-4:10 with about 20 infantry, then sends more waves; beat it with a defense and 10 infantry at home.",
    "A big army committed all at once breaks an army; units sent in small groups die one by one.",
    "When the score is even, keep defending: most opponents attack sooner or later and lose units to our defense, and a 0-0 draw is not a loss.",
    "Attack only when behind on score with a big army (25 or more units), late in the game, at a part of the enemy base where no defenses stand.",
    "When ahead on score, never attack: defend the lead.",
    "An attack that is losing more than it kills, or that runs into defenses, must fall back at once.",
    "Against a turtle (a base full of defenses, mines or aircraft) attacking almost always loses, even when slightly behind: keep defending.",
    "Rocket infantry are the answer to aircraft and tanks; rifle infantry cannot hit aircraft.",
]

QUESTIONS: dict[str, dict[str, Any]] = {
    "opponent": {
        "type": "choice",
        "instructions": {"question": "Which kind of opponent are we playing, judging by everything seen so far?",
                         "playbook": PLAYBOOK},
        "criteria": {
            "rusher": "Attacks our base early (before about 4:30) with a big wave of infantry, and keeps sending waves",
            "turtle": "Stays home behind many defenses (pillboxes, turrets, tesla coils, mines); may have aircraft; rarely attacks",
            "fortified": "Fortifies its ore field, keeps a big reaction army of infantry at home, attacks rarely and late",
            "aggressive": "Sends attack waves at our base again and again through the middle of the game",
            "passive": "Has no army or never fights; nothing to fear",
            "unknown": "Too early or too little seen to tell",
        },
    },
    "posture": {
        "type": "choice",
        "instructions": {"question": "What should our army do for the next 10 seconds?", "playbook": PLAYBOOK},
        "criteria": {
            "defend": "Stay at home and kill whatever comes. Right when ahead or even on score, early in the game, against a rusher or an aggressive opponent, or when the army is small",
            "gather": "Build one big group at the staging point outside our base without attacking. Right when behind on score but the army is not big enough yet to attack",
            "commit": "Attack with everything at once where no defenses stand. Right when behind on score late in the game with a big army, or against a passive opponent",
            "retreat": "Pull the army back home now. Right when our army out of the base is losing more than it kills, or is under fire from enemy defenses",
        },
    },
    "production": {
        "type": "choice",
        "instructions": {"question": "What should our factories train now?", "playbook": PLAYBOOK},
        "criteria": {
            "infantry": "Rifle and rocket infantry only: right in the first 4 minutes against a rush",
            "rockets": "Infantry are all rocket infantry, plus tanks: right when enemy aircraft or many enemy tanks have been seen",
            "vehicles": "Tanks first: right when money is piling up unspent and the enemy army is mostly infantry",
            "mixed": "Both infantry and tanks: the default once the opening is over",
        },
    },
}
ECONOMY_QUESTION = {
    "type": "choice",
    "instructions": {"question": "Should we spend on extra refineries and harvesters now?", "playbook": [
        "At the time limit, when kills minus losses are equal, the side with more assets (the cost of its units and buildings) wins; unspent money counts for nothing.",
        "A refinery comes with a free harvester: it adds more to our assets than it costs, and it earns money.",
        "Money spent on the economy is not spent on the army: against an opponent that attacks, it can cost the base.",
    ]},
    "criteria": {
        "greedy": "Extra refineries and harvesters: right when the opponent has not attacked our base for a long time, or is a turtle, fortified or passive, or when the game looks like a standoff with nobody fighting",
        "safe": "Normal economy, money into the army and defenses: right against a rusher or an aggressive opponent, while enemies are attacking, or early in the game before we know the opponent",
    },
}
OPENING_QUESTION = {
    "type": "choice",
    "instructions": {"question": "Which opening should we play in the first 6 minutes?", "playbook": PLAYBOOK},
    "criteria": {
        "guard": "A defense and about 10 infantry at home first: safe against an early rush, slower economy",
        "economy": "Refineries and harvesters first: stronger later, but wiped out by an early rush",
    },
}


def _root() -> Path:
    """The repository (frozen copies live deeper: walk up to it)."""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "lab").is_dir() and (p / "bots").is_dir()), here.parents[1])


def _lab_dir() -> Path:
    """The lab's directory, shared by every session (lab.LAB_DIR); the repository's sim/lab if the
    lab package can't be imported."""
    try:
        from lab import LAB_DIR

        return LAB_DIR
    except ImportError:
        return _root() / "sim" / "lab"


def load_questions(variant: str | None) -> dict[str, dict[str, Any]]:
    """The questions, with a variant's playbook / criteria / question text swapped in (a JSON file,
    relative to the repository: see bots/jev_variants/ and ``lab jev eval --questions``)."""
    import copy

    qs = copy.deepcopy(QUESTIONS)
    qs["opening"] = copy.deepcopy(OPENING_QUESTION)
    if not variant:
        return qs
    path = Path(variant) if Path(variant).is_absolute() else _root() / variant
    v = json.loads(path.read_text())
    for qid, q in qs.items():
        if "playbook" in v and isinstance(q["instructions"], dict):
            q["instructions"]["playbook"] = v["playbook"]
        if qid in (v.get("criteria") or {}):
            q["criteria"] = v["criteria"][qid]
        if qid in (v.get("instructions") or {}):
            q["instructions"]["question"] = v["instructions"][qid]
    return qs


def _setting(name: str) -> str | None:
    """An environment variable, else a line ``NAME=...`` in this checkout's ``.env``, else in the main
    checkout's (a family session can point its own worktree elsewhere: AGENTS.md)."""
    if os.environ.get(name):
        return os.environ[name]
    roots = [_root()]
    try:
        from lab import MAIN_ROOT

        roots.append(MAIN_ROOT)
    except ImportError:
        pass
    for root in roots:
        try:
            for line in (root / ".env").read_text().splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip() or None
        except OSError:
            pass
    return None


def _key() -> str | None:
    return _setting("TYPESAFE_API_KEY")


def _url() -> str:
    """``JEV_URL``: another System One server (e.g. a local one, no key needed); default TypeSafe's."""
    return _setting("JEV_URL") or API_URL


class Jev:
    """A minimal client for TypeSafe's ``/v1/systemone`` (no SDK needed)."""

    def __init__(self, key: str | None = None, *, model: str = MODEL) -> None:
        import httpx

        self.url = _url()
        # A server of our own needs no key ("local" stands in for one, and isn't sent).
        self.key = key or _key() or ("local" if self.url != API_URL else None)
        self.model = model
        self.http = httpx.Client(timeout=TIMEOUT_SECONDS)
        self.calls = self.errors = self.tokens = self.last_tokens = 0
        self.seconds = 0.0

    def _headers(self) -> dict:
        return {} if self.key == "local" else {"Authorization": f"Bearer {self.key}"}

    def check(self) -> str:
        """"" when Jev answers; otherwise why not (no key, no credits, the service down). One tiny call."""
        if not self.key:
            return "no TYPESAFE_API_KEY (environment or .env)"
        try:
            reply = self.http.post(self.url, headers=self._headers(),
                                   json={"model": self.model, "state": "ping",
                                         "questions": {"ok": {"type": "noul", "instructions": "Is `state` a word?"}}})
        except Exception as e:  # noqa: BLE001
            return f"Jev unreachable: {type(e).__name__}"
        if reply.status_code == 200:
            return ""
        try:
            detail = (reply.json().get("detail") or {})
            detail = detail.get("message") if isinstance(detail, dict) else str(detail)
        except ValueError:
            detail = reply.text[:200]
        return f"Jev answered {reply.status_code}: {detail}"

    def ask(self, state: Any, questions: dict[str, Any]) -> dict[str, Any] | None:
        """The answers by question id, or None (no key, or the call failed)."""
        answers, tokens = self.ask_counted(state, questions)
        self.last_tokens = tokens
        return answers

    def ask_counted(self, state: Any, questions: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
        """``ask`` and the request's input tokens (safe to call from several threads at once)."""
        if not self.key:
            return None, 0
        start = time.monotonic()
        self.calls += 1
        try:
            reply = self.http.post(self.url, headers=self._headers(),
                                   json={"model": self.model, "state": state, "questions": questions})
            reply.raise_for_status()
            body = reply.json()
        except Exception:  # noqa: BLE001  (the game goes on with the last decision)
            self.errors += 1
            return None, 0
        finally:
            self.seconds += time.monotonic() - start
        tokens = int((body.get("usage") or {}).get("input_tokens") or 0)
        self.tokens += tokens
        return body.get("answers") or None, tokens


def _cell(obj: Any) -> tuple[int, int] | None:
    if isinstance(obj, dict) and isinstance(obj.get("x"), (int, float)) and isinstance(obj.get("y"), (int, float)):
        return int(obj["x"]), int(obj["y"])
    if isinstance(obj, (list, tuple)) and len(obj) == 2:
        return int(obj[0]), int(obj[1])
    return None


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _size(n: int) -> str:
    return "none" if n == 0 else "small" if n < 8 else "medium" if n < 16 else "big" if n < 25 else "huge"


def _mix(types: dict[str, int]) -> str:
    parts = [f"{n} {NAMES.get(t, t)}" for t, n in sorted(types.items(), key=lambda kv: -kv[1])]
    return ", ".join(parts) if parts else "nothing"


def _ratio(ours: float, theirs: float) -> str:
    if theirs <= 0:
        return "no enemy force near"
    r = ours / theirs
    if r >= 2:
        return "we clearly outnumber them (more than 2 to 1)"
    if r >= 1.3:
        return "we are stronger"
    if r >= 0.77:
        return "about even"
    if r >= 0.5:
        return "they are stronger"
    return "they clearly outnumber us"


class JevBot:
    """``push``: an attack falls back only when Jev says so *and* the odds counted here are against us
    (by default Jev's word is enough)."""

    def __init__(self, *, log_dir: str | None = None, model: str = MODEL, push: bool = False,
                 commit_deficit: int = COMMIT_DEFICIT, use_jev: bool = True, questions: str | None = None, scout: bool = True,
                 force: str | None = None, economy: str | int = "off", guard_end: float | None = None,
                 greed_floor: int = 0, defenses: int = 2, early_refineries: int = 0,
                 commit_min_army: int = COMMIT_MIN_ARMY, surplus: int = SURPLUS_CASH, extra_barracks: int = 0,
                 fallback: float = 0.0) -> None:
        """``force``: a posture held whatever Jev says and past every rail (for the coach: what would
        each posture have scored from this moment?)."""
        self.push = push
        self.force = force
        # "off": the normal economy; a number: that much greed from the start; "jev": Jev decides
        # (greedy = GREED) whenever it asks.
        self.economy = economy
        self.greed = economy if isinstance(economy, int) else GREED if economy == "read" else 0
        self.guard_end = guard_end      # the guard opening ends here when no raid has come (else at 6:00)
        self.greed_floor = greed_floor  # Jev's "greedy" only with at least this many combat units
        self.defenses = defenses        # the scripted bot's cap on defenses
        self.early_refineries = early_refineries  # refineries before the war factory beyond the first
        self.commit_min_army = commit_min_army    # the rail: no attack with fewer combat units than this
        self.surplus, self.extra_barracks = surplus, extra_barracks  # (the scripted bot's factory knobs)
        self.fallback = fallback  # (outnumbered at home: meet them at our defenses)
        self.scout = scout
        self.questions = load_questions(questions)
        if economy == "jev":
            self.questions["economy"] = ECONOMY_QUESTION
        self.commit_deficit = commit_deficit
        self.bot = RedAlertBot(knobs=Knobs(opening="guard", posture="defend", attack_threshold=60,
                                           greed=economy if isinstance(economy, int) else 2 if economy == "read" else 0,
                                           defenses=defenses, early_refineries=early_refineries,
                                           surplus=surplus, extra_barracks=extra_barracks, fallback=fallback),
                               log=lambda line: None)
        self.jev = Jev(model=model)
        if not use_jev:  # (the control: the same bot with the decisions frozen at the start)
            self.jev.key = None
        self.log_dir = Path(log_dir or os.environ.get("FF_JEV_LOG") or _lab_dir() / "jev")
        self.log_path: Path | None = None
        self.posture = "defend"
        self.posture_since = 0.0
        self.production = "mixed"
        self.opening = "guard"
        self.opponent = "unknown"
        self.belief: dict[str, float] = {}
        self.last_call = -1e9
        self.last_threat = False
        self.last_fight = False
        self.note = "starting as ff-guard-turtle"
        # What has been seen (for the description).
        self.enemy_seen: dict[int, tuple[str, float]] = {}   # unit id -> (type, last seen)
        self.building_types: dict[int, str] = {}             # every enemy building ever seen
        self.last_raid_end = -1e9
        self.enemy_max_army = 0
        self.enemy_defenses: set[str] = set()
        self.defense_cells: dict[int, tuple[str, tuple[int, int]]] = {}  # building id -> (type, cell), while it stands
        self.enemy_buildings: dict[int, tuple[str, tuple[int, int]]] = {}
        self.scout_id: int | None = None
        self.scouts_sent = 0
        self.scout_home = False
        self.attacks: list[dict[str, Any]] = []  # each commit: start, kills/losses at start, result
        self.odds = 0.0                          # our value / theirs in the current fight (0: no fight)
        self.enemy_air: set[str] = set()
        self.first_raid: tuple[float, int] | None = None     # (when, how many) enemies first came to the base
        self.raids = 0
        self.score_marks: list[tuple[float, int, int]] = []  # (seconds, kills, losses)
        self.calls: list[dict[str, Any]] = []
        self.last_state: dict | None = None
        self.last_qids: set = set()
        self.last_answers: dict | None = None
        self.last_asked = -1e9
        self.skipped = 0

    # -- the runner's hooks ---------------------------------------------------------------------

    def choose_action(self, state, context):
        raw = state.raw
        obs = raw.get("observation")
        if not isinstance(obs, dict):
            return WAIT
        now = float((raw.get("time") or {}).get("game_seconds") or (obs.get("tick") or 0) / TICKS)
        facts = self._facts(raw, now)
        urgent = (facts["threat"] and not self.last_threat) or (facts["fight"] and not self.last_fight)
        self.last_threat, self.last_fight = facts["threat"], facts["fight"]
        extra: list[dict[str, Any]] = []
        busy = urgent or facts["fight"] or self.posture == "commit"
        if now - self.last_call >= DECIDE_SECONDS or (busy and now - self.last_call >= URGENT_GAP_SECONDS):
            self.last_call = now
            extra = self._decide(raw, facts, now, context)
        extra += self._scout(raw, facts, now)
        move = self.bot.choose_action(state, context)
        orders = list(move.action.get("orders") or []) if isinstance(move, WithReasoning) else []
        orders = (extra + orders)[:MAX_ORDERS]
        if not orders:
            return WAIT
        why = move.reasoning_summary if isinstance(move, WithReasoning) else ""
        self._sent_extra = len(extra)
        return WithReasoning({"type": "orders", "orders": orders}, f"[jev {self.note}] {why or ''}"[:280])

    def on_action_result(self, result, context):
        shift = getattr(self, "_sent_extra", 0)
        self._sent_extra = 0
        if shift and isinstance(result, dict) and isinstance(result.get("results"), list):
            rows = [dict(r, index=r["index"] - shift) for r in result["results"]
                    if isinstance(r.get("index"), int) and r["index"] >= shift]
            result = {**result, "results": rows}
        self.bot.on_action_result(result, context)

    # -- what is going on (counted in code) -----------------------------------------------------

    def _facts(self, raw: dict, now: float) -> dict[str, Any]:
        obs = raw.get("observation") or {}
        units = [u for u in obs.get("units") or [] if not u.get("wreck")]
        buildings = obs.get("buildings") or []
        home = _cell(obs.get("base_center")) or _cell(buildings[0] if buildings else None) or (0, 0)
        combat = [u for u in units if is_combat(u)]
        enemies = [e for e in obs.get("visible_enemies") or []
                   if not e.get("wreck") and not str(e.get("type") or "").endswith("husk")]
        for e in enemies:
            t = str(e.get("type") or "").lower()
            if isinstance(e.get("id"), int):
                self.enemy_seen[e["id"]] = (t, now)
            if t in AIR:
                self.enemy_air.add(t)
        for b in obs.get("visible_enemy_buildings") or []:
            t = str(b.get("type") or "").lower()
            cell = _cell(b)
            if not isinstance(b.get("id"), int) or cell is None:
                continue
            if b.get("wreck"):
                self.defense_cells.pop(b["id"], None)
                self.enemy_buildings.pop(b["id"], None)
                continue
            self.enemy_buildings[b["id"]] = (t, cell)
            self.building_types[b["id"]] = t
            if t in DEFENSES:
                self.enemy_defenses.add(t)
                self.defense_cells[b["id"]] = (t, cell)
        for gone in set(self.enemy_buildings) - {b.get("id") for b in obs.get("visible_enemy_buildings") or []}:
            # (a remembered building we should see now but don't: destroyed)
            t, cell = self.enemy_buildings[gone]
            if any((c := _cell(u)) and _dist(c, cell) <= 4 for u in units):
                self.enemy_buildings.pop(gone, None)
                self.defense_cells.pop(gone, None)
        army_enemies = [e for e in enemies if str(e.get("type") or "").lower() not in NONCOMBAT]
        self.enemy_max_army = max(self.enemy_max_army, len(army_enemies))
        at_base = [e for e in army_enemies if (c := _cell(e)) and _dist(c, home) <= HOME_RADIUS]
        threat = bool(at_base)
        if not threat and self.last_threat:
            self.last_raid_end = now
        if threat and not self.last_threat and now - self.last_raid_end >= 20:
            self.raids += 1
            if self.first_raid is None:
                self.first_raid = (now, len(at_base))
            elif len(at_base) > self.first_raid[1] and now - self.first_raid[0] < 30:
                self.first_raid = (self.first_raid[0], len(at_base))
        away = [u for u in combat if (c := _cell(u)) and _dist(c, home) > HOME_RADIUS + 4 and u.get("id") != self.scout_id]
        center = None
        if away:
            cells = [_cell(u) for u in away]
            center = (sum(c[0] for c in cells) // len(cells), sum(c[1] for c in cells) // len(cells))
        near_fight = [e for e in army_enemies if center and (c := _cell(e)) and _dist(c, center) <= FIGHT_RADIUS]
        near_defenses = [t for t, c in self.defense_cells.values() if center and _dist(c, center) <= FIGHT_RADIUS]
        fight = bool(near_fight or near_defenses)
        military = obs.get("military") or {}
        kills, losses = int(military.get("kills_cost") or 0), int(military.get("deaths_cost") or 0)
        if not self.score_marks or now - self.score_marks[-1][0] >= 2:
            self.score_marks.append((now, kills, losses))
        return {"home": home, "combat": combat, "enemies": army_enemies, "at_base": at_base, "threat": threat,
                "away": away, "center": center, "near_fight": near_fight, "near_defenses": near_defenses,
                "fight": fight, "kills": kills, "losses": losses, "units": units}

    def _scout(self, raw: dict, facts: dict, now: float) -> list[dict[str, Any]]:
        """One rifleman walks to the enemy base (twice a game): what it sees feeds the opponent read;
        it turns back once it has seen enemy buildings."""
        scout = next((u for u in facts["units"] if u.get("id") == self.scout_id), None)
        if scout is not None and not self.scout_home and (c := _cell(scout)):
            seen = [cell for _, cell in self.enemy_buildings.values() if _dist(cell, c) <= 10]
            seen += [e for e in facts["enemies"] if (ec := _cell(e)) and _dist(ec, c) <= 8]
            if seen:
                self.scout_home = True
                self.bot._unit_ordered[self.scout_id] = now + 30
                return [{"cmd": "move", "units": [self.scout_id], "to": list(facts["home"])}]
        if not self.scout or self.scouts_sent >= len(SCOUT_SECONDS) or now < SCOUT_SECONDS[self.scouts_sent] or facts["threat"]:
            return []
        legal = raw.get("legal_actions") or {}
        target = _cell(legal.get("enemy_base")) or self.bot._enemy_spawn(facts["home"])
        alive = {u.get("id") for u in facts["combat"]}
        if self.scout_id in alive or not target:
            return []
        picks = [u for u in facts["combat"] if str(u.get("type")).lower() in ("e1", "dog", "jeep")]
        if not picks:
            return []
        self.scout_id = min(picks, key=lambda u: u["id"])["id"]
        self.scouts_sent += 1
        self.scout_home = False
        self.bot._unit_ordered[self.scout_id] = now + 120  # (the bot leaves it alone for a while)
        return [{"cmd": "move", "units": [self.scout_id], "to": list(target)}]

    def _aim(self, facts: dict) -> tuple[int, int] | None:
        """Where to attack: the known enemy building farthest from every known defense."""
        targets = [c for t, c in self.enemy_buildings.values() if t not in DEFENSES]
        if not targets:
            return None
        towers = [c for _, c in self.defense_cells.values()]
        if not towers:
            return min(targets, key=lambda c: _dist(c, facts["home"]))
        return max(targets, key=lambda c: (min(_dist(c, d) for d in towers), -_dist(c, facts["home"])))

    def _intel_text(self) -> str:
        """Everything seen of the enemy all game, so 'never seen' reads as unknown, not as nothing."""
        units: dict[str, int] = {}
        for t, _ in self.enemy_seen.values():
            if t not in NONCOMBAT:
                units[t] = units.get(t, 0) + 1
        kinds: dict[str, int] = {}
        for t in self.building_types.values():
            kinds[t] = kinds.get(t, 0) + 1
        parts = []
        parts.append(f"buildings seen all game: {_mix(kinds)}" if kinds else
                     "we have never seen their base: its defenses are unknown")
        parts.append(f"different combat units seen all game: {sum(units.values())} ({_mix(units)})" if units else
                     "we have never seen their army: its size is unknown, not zero")
        return "; ".join(parts)

    def _defenses_text(self) -> str:
        standing: dict[str, int] = {}
        for t, _ in self.defense_cells.values():
            standing[t] = standing.get(t, 0) + 1
        n = sum(standing.values())
        if not n:
            return "none standing that we know of" + (" (some destroyed)" if self.enemy_defenses else "")
        return f"{n} known ({'many: a fortified base' if n >= 4 else 'a few'}): {_mix(standing)}"

    def _cost(self, unit_type: str) -> int:
        return int(self.bot._costs.get(unit_type, 400))

    def describe(self, raw: dict, facts: dict, now: float) -> dict[str, str]:
        """The game in words: every number counted and compared here, not by the model."""
        obs = raw.get("observation") or {}
        time_info = raw.get("time") or {}
        remaining = time_info.get("remaining_seconds")
        total = now + remaining if isinstance(remaining, (int, float)) else 600.0
        left = total - now
        phase = ("opening: the rush window" if now < 270 else "middle game" if now < 450
                 else "late game" if left > FINAL_SECONDS else "final minute")
        net = facts["kills"] - facts["losses"]
        score = ("even" if net == 0 else f"we are ahead by {net} credits" if net > 0 else f"we are behind by {-net} credits")
        window = 12 if facts["fight"] else 30
        recent = [m for m in self.score_marks if now - m[0] <= window]
        trade = f"no fighting in the last {window} seconds"
        if recent:
            k, l = facts["kills"] - recent[0][1], facts["losses"] - recent[0][2]
            if k or l:
                trade = f"in the last {window} seconds we killed {k} and lost {l} credits worth" + (
                    " (we are winning the trades)" if k > l else " (we are losing the trades)" if l > k else "")
        ours: dict[str, int] = {}
        for u in facts["combat"]:
            t = str(u.get("type") or "").lower()
            ours[t] = ours.get(t, 0) + 1
        n = len(facts["combat"])
        where = ("at home" if not facts["away"] else
                 "out of the base" if len(facts["away"]) == n else f"{len(facts['away'])} of them out of the base")
        recent_enemy: dict[str, int] = {}
        for t, seen in self.enemy_seen.values():
            if now - seen <= 60 and t not in NONCOMBAT:
                recent_enemy[t] = recent_enemy.get(t, 0) + 1
        raid = ("no enemy army has come near our base yet" if self.first_raid is None else
                f"enemies first came to our base at {_clock(self.first_raid[0])} with {self.first_raid[1]} units; "
                f"{self.raids} raid(s) so far")
        state = {
            "clock": f"{_clock(now)} played, {_clock(left)} left ({phase})",
            "score": f"kills minus losses: {score}",
            "recent_trades": trade,
            "our_army": f"{n} combat units ({_size(n)}), {where}: {_mix(ours)}",
            "enemy_army": (f"in the last minute we saw {sum(recent_enemy.values())} enemy combat units: {_mix(recent_enemy)}; "
                           f"the biggest enemy group seen at once was {self.enemy_max_army} units ({_size(self.enemy_max_army)})"),
            "enemy_defenses": self._defenses_text(),
            "what_we_know_of_the_enemy": self._intel_text(),
            "enemy_aircraft": ", ".join(sorted(NAMES.get(t, t) for t in self.enemy_air)) or "none seen",
            "raids_on_our_base": raid,
            "now_at_our_base": (f"{len(facts['at_base'])} enemy units are attacking our base" if facts["threat"]
                                else "our base is quiet"),
            "our_current_orders": {"defend": "defending at home", "gather": "gathering outside the base",
                                   "commit": "attacking", "retreat": "falling back home"}[self.posture],
        }
        if facts["fight"]:
            ours_v = sum(self._cost(str(u.get("type") or "").lower()) for u in facts["away"])
            theirs_v = sum(self._cost(str(e.get("type") or "").lower()) for e in facts["near_fight"])
            theirs_v += 1500 * len(facts["near_defenses"])  # (a defense is worth several units in a fight)
            self.odds = ours_v / theirs_v if theirs_v else 9.9
            towers = {}
            for t in facts["near_defenses"]:
                towers[t] = towers.get(t, 0) + 1
            state["our_attack"] = (f"our {len(facts['away'])} units out of the base are fighting "
                                   f"{len(facts['near_fight'])} enemy units"
                                   + (f" and {_mix(towers)}" if towers else "") + f": {_ratio(ours_v, theirs_v)}")
        economy = obs.get("economy") or {}
        cash = int(economy.get("cash") or 0) + int(economy.get("ore") or 0)
        harvesters = sum(1 for u in obs.get("units") or [] if str(u.get("type")).lower() == "harv")
        state["economy"] = f"{harvesters} harvesters, " + ("money piling up unspent" if cash > 2000 and now >= 180 else
                                                           "some money" if cash > 600 else "money all spent")
        if not facts["fight"]:
            self.odds = 0.0
        done = [a for a in self.attacks if a.get("result")]
        if done:
            state["our_past_attacks"] = "; ".join(
                f"at {_clock(a['start'])} our attack killed {a['killed']} and lost {a['lost']} credits worth"
                + (f" against {a['towers']}" if a.get("towers") else "") for a in done[-3:])
        if self.belief:
            best = max(self.belief, key=self.belief.get)
            state["our_read_so_far"] = f"the opponent looked like '{best}' (about {round(self.belief[best] * 10) * 10}% sure)"
        return state

    # -- deciding ------------------------------------------------------------------------------------

    def _decide(self, raw: dict, facts: dict, now: float, context) -> list[dict[str, Any]]:
        state = self.describe(raw, facts, now)
        if self.force:  # (no question to ask: the posture is fixed)
            extra = self._apply({}, facts, now, raw)
            self._log(context, now, state, None, 0.0)
            return extra
        questions = {k: v for k, v in self.questions.items() if k != "opening" or now < 240}
        same = {k: v for k, v in state.items() if k != "clock"} == self.last_state and set(questions) == self.last_qids
        self.last_state, self.last_qids = {k: v for k, v in state.items() if k != "clock"}, set(questions)
        if same and self.last_answers and now - self.last_asked < 30:
            # Nothing changed since the last call: its answers stand (no call, no cost).
            self.skipped += 1
            return self._apply(self.last_answers, facts, now, raw)
        start = time.monotonic()
        answers = self.jev.ask(state, questions)
        if answers:
            self.last_answers, self.last_asked = answers, now
        latency = time.monotonic() - start
        extra: list[dict[str, Any]] = []
        if answers:
            extra = self._apply(answers, facts, now, raw)
        self._log(context, now, state, answers, latency)
        return extra

    def _apply(self, answers: dict, facts: dict, now: float, raw: dict) -> list[dict[str, Any]]:
        def choice(qid: str) -> tuple[str | None, float]:
            a = answers.get(qid) or {}
            pick = a.get("choice")
            return pick, float((a.get("probabilities") or {}).get(pick, 0.0)) if pick else 0.0

        opp, _ = choice("opponent")
        probs = (answers.get("opponent") or {}).get("probabilities") or {}
        if probs:  # a running belief: the read sharpens as the game shows more
            for k, p in probs.items():
                self.belief[k] = 0.6 * self.belief.get(k, p) + 0.4 * float(p)
            self.opponent = max(self.belief, key=self.belief.get)
        posture, p_posture = choice("posture")
        production, _ = choice("production")
        opening, _ = choice("opening")
        n = len(facts["combat"])
        time_info = raw.get("time") or {}
        remaining = time_info.get("remaining_seconds")
        net = facts["kills"] - facts["losses"]
        rails = []

        # Rails from what we learned (docs/STRATEGY.md).
        passive = self.opponent == "passive" and self.enemy_max_army < 5  # (a read the facts must back)
        if posture == "commit" and self.posture != "commit":
            if n < (8 if passive else self.commit_min_army) or (now < COMMIT_MIN_SECONDS and not passive):
                posture = "gather"
                rails.append("too small/early to attack")
            elif not passive and not (net <= -self.commit_deficit or
                                      (net < 0 and isinstance(remaining, (int, float)) and remaining < COMMIT_LATE_SECONDS
                                       and self.opponent != "turtle")):
                posture = "defend"
                rails.append("not behind enough: defend")
            elif self.attacks and self.attacks[-1].get("failed") and now - self.attacks[-1]["end"] < RECOMMIT_SECONDS:
                posture = "gather"
                rails.append("last attack failed: wait")
        if production == "infantry" and now >= 240:
            production = "mixed"
        if isinstance(remaining, (int, float)) and remaining < FINAL_SECONDS and net > 0 and posture in ("commit", "gather"):
            posture = "defend"
            rails.append("hold the lead")
        if facts["threat"] and posture == "gather":
            posture = "defend"
        # Hysteresis: an attack runs its course unless the model wants out; never turn around in the
        # middle of a fight we are winning (turning back under fire loses the army).
        if self.posture == "commit" and posture in ("gather", "defend"):
            if now - self.posture_since < COMMIT_HOLD_SECONDS:
                posture = "commit"
            elif facts["fight"] and self.odds >= 1.0 and not facts["threat"]:
                posture = "commit"
                rails.append("finish the fight")
        if self.posture == "retreat" and posture == "commit" and now - self.posture_since < RETREAT_HOLD_SECONDS:
            posture = "defend"
        if posture == "retreat" and not facts["away"]:
            posture = "defend"
        if posture == "retreat" and self.push and self.posture == "commit" and self.odds >= 1.0:
            posture = "commit"
            rails.append(f"push on (odds {self.odds:.1f})")

        if self.force:
            posture = self.force if (self.force != "retreat" or facts["away"]) else "defend"
            rails = [f"forced {self.force}"]
        extra: list[dict[str, Any]] = []
        if posture and posture != self.posture:
            if self.posture == "commit":  # the wave ends: everyone out there comes back
                self.bot._wave_active, self.bot._wave_ids = False, set()
                if posture != "retreat":
                    ids = [u["id"] for u in facts["away"]][:50]
                    if ids:
                        extra.append({"cmd": "move", "units": ids, "to": list(facts["home"])})
            if posture == "commit":
                self.attacks.append({"start": now, "k0": facts["kills"], "l0": facts["losses"], "towers": ""})
            elif self.posture == "commit" and self.attacks:
                a = self.attacks[-1]
                a.update(end=now, result=True, killed=facts["kills"] - a["k0"], lost=facts["losses"] - a["l0"])
                a["failed"] = a["lost"] > a["killed"]
            self.posture, self.posture_since = posture, now
        if self.posture == "commit" and self.attacks and facts["near_defenses"]:
            towers: dict[str, int] = {}
            for t in facts["near_defenses"]:
                towers[t] = towers.get(t, 0) + 1
            self.attacks[-1]["towers"] = _mix(towers)
        if self.posture == "retreat":
            ids = [u["id"] for u in facts["away"]][:50]
            if ids:
                extra.append({"cmd": "move", "units": ids, "to": list(facts["home"])})
        if production in ("infantry", "rockets", "vehicles", "mixed"):
            self.production = production
        economy, _ = choice("economy")
        if self.economy == "read":
            # Greedy until the opponent shows itself as one that attacks (Jev's read), or attacks now.
            dangerous = self.opponent in ("rusher", "aggressive") or facts["threat"] or self.raids >= 2
            self.greed = 0 if dangerous else GREED
        if self.economy == "jev" and economy in ("greedy", "safe"):
            self.greed = GREED if (economy == "greedy" and not facts["threat"]
                                   and len(facts["combat"]) >= self.greed_floor) else 0
        if self.guard_end is not None and now >= self.guard_end and self.raids == 0 and self.opening == "guard":
            self.opening = "standard"
            rails.append("no rush: guard over")
        if opening in ("guard", "economy") and now < 240:
            self.opening = "guard" if opening == "guard" else "standard"
        knobs = Knobs(
            posture={"defend": "defend", "retreat": "defend", "gather": "attack", "commit": "attack"}[self.posture],
            # (commit: launch now; the bot asks later waves for 2 more units each)
            attack_threshold=60 if self.posture == "gather" else max(3, n - 2 * min(self.bot._waves, 3)),
            unit_focus=self.production,
            target="nearest",
            opening=self.opening,
            aim=self._aim(facts) if self.posture == "commit" else None,
            greed=self.greed,
            defenses=self.defenses,
            early_refineries=self.early_refineries,
            surplus=self.surplus,
            extra_barracks=self.extra_barracks,
            fallback=self.fallback,
            note=self.posture,
        )
        self.bot.set_knobs(knobs)
        self.note = (f"{self.posture}" + (f"({p_posture:.2f})" if p_posture else "") + f" opp={self.opponent} build={self.production}"
                     + (f" greed={self.greed}" if self.economy != "off" else "")
                     + (f" open={self.opening}" if now < 240 else "") + (f" rails: {', '.join(rails)}" if rails else ""))
        return extra

    def _log(self, context, now, state, answers, latency, more: dict | None = None) -> None:
        entry = {"t": round(now, 1), "latency": round(latency, 3), "tokens": self.jev.last_tokens if answers else 0,
                 "state": state, "decision": self.note,
                 "answers": {k: (v.get("probabilities") if v.get("type") != "noul" else v.get("noul"))
                             for k, v in (answers or {}).items()} if answers else None, **(more or {})}
        self.calls.append(entry)
        try:
            if self.log_path is None:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                sid = getattr(context, "session_id", None) or f"game-{int(time.time())}"
                self.log_path = self.log_dir / f"{sid}-{getattr(context, 'seat_position', 0)}.jsonl"
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def create_agent(**kwargs) -> JevBot:
    return JevBot(**kwargs)
