"""Honor of Kings 5v5 for the example LLM agent: the model commands five heroes.

Honor of Kings (``honor_of_kings_5v5``) is played by teams: each agent commands
a whole team of five heroes (seats 0-4 or 5-9) and destroys the enemy crystal
to win. Every hero takes one action per step (0.2 s of game time). Moves are
**macros** offered per hero in ``state.legal_actions`` (action ids
``s<seat>:macro_advance``, ``macro_retreat``, ``macro_attack``,
``macro_skill1``-``3``, each offered only while it is possible), sent one
hero at a time as ``{"macro": "<id>", "seat": n}``. A macro becomes that
hero's **standing order**: the server keeps re-playing it for the new
situation on every step until the hero gets another order, so the team keeps
fighting while the model thinks. See GAMES.md.

``examples/llm_agent.py`` hands every HoK decision to a ``HokPlayer``, one per
match:

1. Every ``DECIDE_SECONDS`` the model gets a compact view (each hero's HP,
   level, position and the macros it has now; enemy heroes in sight; towers
   and crystals) and answers with one macro per hero through a strict JSON
   schema. A hero the model leaves out, an order that isn't on offer, a
   provider error or the call cap: the built-in rules decide (retreat on low
   HP, fight what is in range, else push), so the team never stands still.
2. Only changes are sent: a hero whose standing order is already the plan
   gets nothing. In real time (``pacing.mode == "realtime"``) the player
   returns ``altruagent.WAIT`` when there is nothing to change. In lockstep
   (each step waits for every hero) it gives every hero still waiting an
   action, as the server requires.
3. One order goes out per ``choose_action`` call (one ``play_action`` per
   hero); the runner calls again at once with the next view.
"""

from __future__ import annotations

import inspect
import json
import re
import time
from typing import Any, Callable

from altruagent import WAIT, DecisionContext, GameState, LegalAction, WithReasoning

from .providers import LLMProvider, ProviderError

GAME_TYPE = "honor_of_kings_5v5"
MACROS = ("macro_advance", "macro_retreat", "macro_attack", "macro_skill1", "macro_skill2", "macro_skill3")

DECIDE_SECONDS = 3.0  # how often the model re-plans the team (the orders keep playing meanwhile)
REQUEST_SECONDS = 15.0  # one model request at most; real time does not wait for it
MAX_CALLS = 900  # a cost guard: a 30-minute match at one plan every 2 s
ERROR_BACKOFF_SECONDS = 10.0  # after a provider error, the rules play for this long
LOW_HP = 0.25  # the rules retreat a hero below this share of its HP
IN_RANGE = 9000  # the rules fight a target closer than this (game units)
REASONING_MAX_CHARS = 280
MAX_ENEMIES_SHOWN = 5
MAX_STRUCTURES_SHOWN = 24

_SEAT_ID = re.compile(r"^s(\d+):(.+)$")
_DISTANCE = re.compile(r"(\d+) away\)")

_SYSTEM_PROMPT = (
    "You command a team of five heroes in Honor of Kings 5v5 (a MOBA) on the AltruAgent "
    "platform. Win by destroying the enemy crystal; at the step or time limit the team that "
    "did more damage to enemy towers and crystal wins. The game runs on while you think. For "
    "each of your heroes choose one macro from that hero's own options: macro_advance (walk "
    "toward the nearest enemy tower or crystal), macro_retreat (walk home to heal), "
    "macro_attack (basic attack on the nearest target), macro_skill1/2/3 (cast a skill at "
    "it). A macro keeps playing until you change it. Heroes heal at home; a dead hero "
    "revives after revive_time. Push together, retreat heroes with low HP, and use skills "
    "when a target is in range. Answer only in the required JSON format; reasoning_summary "
    "is one or two short sentences spectators will see."
)


class HokPlayer:
    """One Honor of Kings match's decisions. ``choose_action`` returns one
    hero's order (``{"macro": id, "seat": n}``, or a complete listed action
    for a hero with no macro in lockstep) or ``WAIT``."""

    def __init__(
        self,
        provider: LLMProvider | None,
        *,
        log: Callable[[str], None] = print,
        decide_seconds: float = DECIDE_SECONDS,
        max_calls: int = MAX_CALLS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._log = log
        self._decide_seconds = decide_seconds
        self.max_calls = max_calls
        self._clock = clock
        self._takes_timeout = _accepts_timeout(provider)
        self.calls = 0
        self.ok = 0
        self.fallbacks: dict[str, int] = {}
        self._plan: dict[int, str] = {}
        self._reasoning: str | None = None
        self._next_plan_at: float | None = None
        self._quiet_until = 0.0

    # -- the runner's hooks ------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        realtime = _is_realtime(state)
        offers = _offers_by_seat(state.legal_actions)
        if not offers:
            if realtime:
                return WAIT
            raise ValueError("Honor of Kings asked for a move but listed no legal actions")
        now = self._clock()
        if self._next_plan_at is None or now >= self._next_plan_at:
            self._replan(state, offers)
            self._next_plan_at = now + self._decide_seconds

        standing = _standing_orders(state)
        pending = _pending_seats(state, offers)
        for seat in pending:
            macros = offers[seat]["macros"]
            want = self._plan.get(seat)
            if want not in macros:
                want = rule_macro(_summary(state, seat), macros)
            if want is None:
                if realtime:
                    continue  # a dead hero: nothing to send
                return _complete_raw(offers[seat]["raw"], seat)
            if realtime and standing.get(seat) == want:
                continue
            order = {"macro": want, "seat": seat}
            return WithReasoning(order, self._reasoning) if self._reasoning else order
        if realtime:
            return WAIT
        # Lockstep with every pending hero already on its order cannot happen
        # (a pending hero always gets one above); keep the server moving anyway.
        seat = pending[0]
        return _complete_raw(offers[seat]["raw"], seat)

    def on_action_result(self, result: dict, context: DecisionContext) -> None:
        if isinstance(result, dict) and result.get("error"):
            # A race (the macro went away between the read and the send): plan
            # again on the next view rather than repeating it.
            self.fallbacks["refused"] = self.fallbacks.get("refused", 0) + 1
            self._next_plan_at = None

    # -- planning -------------------------------------------------------------------------

    def _replan(self, state: GameState, offers: dict[int, dict[str, Any]]) -> None:
        seats = sorted(offers)
        rules = {s: rule_macro(_summary(state, s), offers[s]["macros"]) for s in seats}
        plan = self._ask_model(state, offers) if self._model_available() else None
        if plan is None:
            self._plan = {s: m for s, m in rules.items() if m}
            self._reasoning = None
            return
        merged = {}
        for s in seats:
            pick = plan.get(s)
            if pick in offers[s]["macros"]:
                merged[s] = pick
            elif rules[s]:
                merged[s] = rules[s]
                self.fallbacks["unlisted"] = self.fallbacks.get("unlisted", 0) + 1
        self._plan = merged

    def _model_available(self) -> bool:
        if self._provider is None:
            return False
        if self.calls >= self.max_calls:
            self.fallbacks["call_cap"] = self.fallbacks.get("call_cap", 0) + 1
            return False
        return self._clock() >= self._quiet_until

    def _ask_model(self, state: GameState, offers: dict[int, dict[str, Any]]) -> dict[int, str] | None:
        seats = sorted(offers)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(build_view(state, offers), ensure_ascii=False, separators=(",", ":"))},
        ]
        kwargs = {"timeout": REQUEST_SECONDS} if self._takes_timeout else {}
        self.calls += 1
        try:
            answer = self._provider.complete_structured(messages, "hok_orders", answer_schema(seats), **kwargs)
        except ProviderError as exc:
            self._log(f"[hok] model unavailable, the rules play for {ERROR_BACKOFF_SECONDS:.0f} s: {exc}")
            self.fallbacks["provider_error"] = self.fallbacks.get("provider_error", 0) + 1
            self._quiet_until = self._clock() + ERROR_BACKOFF_SECONDS
            return None
        plan = parse_answer(answer, seats)
        if not plan:
            self.fallbacks["unusable"] = self.fallbacks.get("unusable", 0) + 1
            return None
        self.ok += 1
        summary = str(answer.get("reasoning_summary") or "").strip()[:REASONING_MAX_CHARS]
        self._reasoning = summary or None
        self._log(f"[hok] plan {', '.join(f'{s}:{m[6:]}' for s, m in sorted(plan.items()))}"
                  + (f" | {summary}" if summary else ""))
        return plan


# -- state helpers (pure; tested directly) ----------------------------------------------


def _is_realtime(state: GameState) -> bool:
    pacing = state.raw.get("pacing")
    return isinstance(pacing, dict) and pacing.get("mode") == "realtime"


def _offers_by_seat(legal_actions: list[LegalAction]) -> dict[int, dict[str, Any]]:
    """{seat: {"macros": {macro_id: label}, "raw": [LegalAction, ...]}} from the
    seat-tagged legal actions (``s3:macro_attack``, ``s3:button_0``, ...)."""
    out: dict[int, dict[str, Any]] = {}
    for action in legal_actions:
        m = _SEAT_ID.match(action.action_id)
        if not m:
            continue
        seat, base = int(m.group(1)), m.group(2)
        entry = out.setdefault(seat, {"macros": {}, "raw": []})
        if base in MACROS:
            entry["macros"][base] = action.label or base
        else:
            entry["raw"].append(action)
    return out


def _standing_orders(state: GameState) -> dict[int, str]:
    simultaneous = state.raw.get("simultaneous") or {}
    orders = simultaneous.get("standing_orders") or {}
    return {int(k): v for k, v in orders.items() if str(k).isdigit() and isinstance(v, str)}


def _pending_seats(state: GameState, offers: dict[int, Any]) -> list[int]:
    simultaneous = state.raw.get("simultaneous") or {}
    pending = simultaneous.get("your_pending_seats")
    if isinstance(pending, list):
        return [s for s in pending if s in offers]
    return sorted(offers)


def _summary(state: GameState, seat: int) -> dict[str, Any]:
    observation = state.raw.get("observation") or {}
    seats = observation.get("seats") if isinstance(observation, dict) else None
    entry = (seats or {}).get(str(seat)) or {}
    summary = entry.get("summary") if isinstance(entry, dict) else None
    return summary if isinstance(summary, dict) else {}


def _distance(label: str) -> float:
    """Macro labels end "(... 4123 away)"; inf without one."""
    m = _DISTANCE.search(label or "")
    return float(m.group(1)) if m else float("inf")


def rule_macro(summary: dict[str, Any], macros: dict[str, str]) -> str | None:
    """The built-in rules: retreat on low HP, fight what is in range, else
    push; None when the hero has no macro (dead)."""
    if not macros:
        return None
    me = summary.get("my_hero") or {}
    hp, max_hp = me.get("hp"), me.get("max_hp")
    if isinstance(hp, (int, float)) and isinstance(max_hp, (int, float)) and max_hp > 0 \
            and hp < LOW_HP * max_hp and "macro_retreat" in macros:
        return "macro_retreat"
    for macro in ("macro_skill3", "macro_skill2", "macro_skill1", "macro_attack"):
        if macro in macros and _distance(macros[macro]) < IN_RANGE:
            return macro
    if "macro_advance" in macros:
        return "macro_advance"
    return next(iter(macros))


def _complete_raw(raw: list[LegalAction], seat: int) -> dict[str, Any]:
    """A playable action for a hero without macros (lockstep only): the
    do-nothing button when listed, else the first button with the first
    legal value of each field."""
    for action in raw:
        payload = dict(action.input.get("action") or {})
        if payload.get("which_button") == 0:
            return {"which_button": 0, "seat": seat}
    if not raw:
        return {"which_button": 0, "seat": seat}
    payload = dict(raw[0].input.get("action") or {})
    out: dict[str, Any] = {"seat": seat}
    for key, value in payload.items():
        if key.startswith("legal_"):
            if isinstance(value, list) and value:
                out[key[len("legal_"):]] = value[0]
        elif key != "seat":
            out[key] = value
    return out


def build_view(state: GameState, offers: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """The compact view the model sees: each hero's state and options, enemies
    in sight, the structures, and the clock."""
    heroes, enemies, structures = [], {}, None
    for seat in sorted(offers):
        summary = _summary(state, seat)
        me = summary.get("my_hero") or {}
        heroes.append({
            "seat": seat,
            **{k: me.get(k) for k in ("hero_id", "hp", "max_hp", "level", "x", "z", "kills", "deaths", "revive_time")},
            "options": offers[seat]["macros"],
        })
        camp = me.get("camp")
        for other in summary.get("other_heroes") or []:
            if other.get("visible") and other.get("camp") not in (None, camp):
                enemies[other.get("runtime_id")] = {k: other.get(k) for k in ("hero_id", "hp", "max_hp", "x", "z")}
        if structures is None and summary.get("structures"):
            structures = [
                {k: s.get(k) for k in ("camp", "type", "hp", "max_hp", "x", "z")}
                for s in summary["structures"][:MAX_STRUCTURES_SHOWN]
            ]
    return {
        "your_camp": state.raw.get("camp"),
        "step": state.state_version,
        "max_steps": state.raw.get("max_steps"),
        "seconds_left": state.raw.get("seconds_left"),
        "heroes": heroes,
        "enemies_in_sight": list(enemies.values())[:MAX_ENEMIES_SHOWN],
        "structures": structures or [],
        "instructions": "Give each hero one macro from its own options.",
    }


def answer_schema(seats: list[int]) -> dict[str, Any]:
    order = {
        "type": "object",
        "properties": {"seat": {"type": "integer", "enum": seats}, "macro": {"type": "string", "enum": list(MACROS)}},
        "required": ["seat", "macro"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"orders": {"type": "array", "items": order}, "reasoning_summary": {"type": "string"}},
        "required": ["orders", "reasoning_summary"],
        "additionalProperties": False,
    }


def parse_answer(answer: Any, seats: list[int]) -> dict[int, str]:
    """{seat: macro} from the model's answer: known seats and macros only, the
    first order per seat."""
    plan: dict[int, str] = {}
    orders = answer.get("orders") if isinstance(answer, dict) else None
    for order in orders if isinstance(orders, list) else []:
        if not isinstance(order, dict):
            continue
        seat, macro = order.get("seat"), order.get("macro")
        if isinstance(seat, int) and not isinstance(seat, bool) and seat in seats \
                and macro in MACROS and seat not in plan:
            plan[seat] = macro
    return plan


def _accepts_timeout(provider: Any) -> bool:
    try:
        parameters = inspect.signature(provider.complete_structured).parameters.values()
    except (AttributeError, TypeError, ValueError):
        return False
    return any(p.name == "timeout" or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)
