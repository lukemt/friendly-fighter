"""friendly-fighter's Honor of Kings agent: the runner's hooks around the team policy.

The runner (``altruagent.runner.run_game``) calls ``choose_action`` with each
state and sends what it returns, one hero's action per call (/skill/hok
section 4). This agent:

- **Real time** (``pacing.mode == "realtime"``): plans the team on every
  state, and sends only *changes*: a macro becomes the hero's standing order
  and keeps playing, so a hero already on the wanted order gets nothing. The
  most urgent change goes first (a retreat before an advance). With nothing
  to change it returns ``WAIT``, after a short pause (``pace_s``) so idle reads
  stay a few a second, not one per 0.2 s step.
- **Lockstep**: answers every pending hero at once, each with its planned
  macro (sent even when it is the standing order: the step waits for it). A
  dead hero gets the do-nothing button.
- **Safety layer**: every answer is checked against the state's own legal
  actions before it leaves (a macro listed for that seat, a button with every
  field from its ``legal_*`` list, or ``WAIT``). An exception, an unlisted or
  malformed answer: a listed macro instead (attack, else advance, else the
  first entry), logged and counted. Never raises.
- **Refusals** (``on_action_result``): a refused order is not resent for a
  moment (real time) or for that step (lockstep); the hero's next choice is
  used. If ``{"macro", "seat"}`` actions keep being refused while nothing is
  accepted, it switches to sending the listed ``action_id`` (the guide's other
  documented form).
"""

from __future__ import annotations

import math
import time
import traceback
from collections import Counter
from typing import Any, Callable

from altruagent import WAIT, DecisionContext, GameState, LegalAction, WithReasoning
from altruagent.console import print_line

from .policy import Choice, Knobs, TeamPolicy
from .view import GAME_TYPE, HEAL, HOLD, MACROS, RAW_ORDERS, Offer, TeamView, offers_by_seat, read_view

PACE_S = 0.3                # real time: at most one idle look per this long
RESEND_S = 1.5              # an order sent this recently isn't resent while the server catches up
REFUSE_S = 1.0              # real time: a refused order rests this long
STATUS_GAME_SECONDS = 30.0  # a status line this often (game time)
FORMAT_SWITCH_REFUSALS = 3  # macro dicts refused in a row, none accepted: send action ids
LOG_FIRST = 5               # log the first few of each kind of trouble, then every LOG_EVERY-th
LOG_EVERY = 100
FALLBACK_ORDER = ("macro_attack", "macro_advance")


class HokAgent:
    #: The bot's name in the recordings.
    BOT = "ff-hok"

    def __init__(
        self,
        knobs: Knobs | None = None,
        *,
        log: Callable[[str], None] = print_line,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        pace_s: float = PACE_S,
        reasoning: bool = True,
    ) -> None:
        self.policy = TeamPolicy(knobs)
        self._log = log
        self._clock = clock
        self._sleep = sleep
        self._pace = pace_s
        self._reasoning = reasoning
        self._sent: dict[int, tuple[str, float]] = {}            # seat -> (order, when) last accepted
        self._inflight: tuple[int, str, str, int, float] | None = None  # seat, order, form, version, when
        self._refused: dict[tuple[int, str], tuple[float, int]] = {}  # (seat, order) -> (until, version)
        self._answered: dict[int, set[int]] = {}                  # lockstep: version -> seats answered
        self._use_action_id = False
        self._dict_refused_streak = 0
        self._dict_accepted = 0
        self._last_idle = -math.inf
        self._last_status = -math.inf
        self._started = False
        self._last_view: TeamView | None = None
        self._last_plan: dict[int, Choice] | None = None
        self._plan_version: int | None = None
        self.stats: Counter = Counter()
        self.reasons: Counter = Counter()
        self.decide_ms_total = 0.0
        self.decide_ms_max = 0.0

    # -- the runner's hooks ----------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        start = time.perf_counter()  # real time, whatever clock the agent plays by
        self.stats["decisions"] += 1
        try:
            decision = self._decide(state)
        except Exception as exc:  # noqa: BLE001 - nothing may stop the team
            self._trouble("exception", f"{type(exc).__name__}: {exc}", exc)
            decision = None
        try:
            decision = self._guard(decision, state)
        except Exception as exc:  # noqa: BLE001
            self._trouble("guard_exception", f"{type(exc).__name__}: {exc}", exc)
            decision = WAIT
        spent = (time.perf_counter() - start) * 1000.0
        self.decide_ms_total += spent
        self.decide_ms_max = max(self.decide_ms_max, spent)
        if decision is WAIT:
            self.stats["waits"] += 1
            self._idle(state)
        else:
            self.stats["sends"] += 1
        return decision

    def on_action_result(self, result: dict, context: DecisionContext) -> None:
        try:
            self._on_result(result)
        except Exception as exc:  # noqa: BLE001
            self._trouble("result_exception", f"{type(exc).__name__}: {exc}", exc)

    # -- deciding ----------------------------------------------------------------------------

    def _decide(self, state: GameState):
        view = read_view(state)
        self._last_view = view
        if not view.offers:
            return WAIT
        if not self._started:
            self._started = True
            self._log(f"[hok] {GAME_TYPE}: camp {view.camp} seats {view.seats} "
                      f"pacing {'realtime' if view.realtime else 'lockstep'} max_steps {view.max_steps} "
                      f"time_limit_s {view.time_limit_s}")
        if view.version == self._plan_version and self._last_plan is not None:
            plan = self._last_plan  # the same step: the plan stands (orders sent since don't change it)
        else:
            plan = self.policy.plan(view)
            self._plan_version = view.version
            for choice in plan.values():  # where the team's time goes (status lines, the sim)
                self.reasons[choice.reason.split(" (")[0].split(":")[0]] += 1
        self._last_plan = plan
        self._status(view, plan)
        now = self._clock()
        if view.realtime:
            return self._realtime(view, plan, now)
        return self._lockstep(view, plan, now)

    def _realtime(self, view: TeamView, plan: dict[int, Choice], now: float):
        seats = [s for s in (view.pending or sorted(view.offers)) if s in view.offers]
        best: tuple[int, int, str, Choice] | None = None
        for seat in seats:
            choice = plan.get(seat)
            if choice is None:
                continue  # dead, or nothing on offer: its standing order (if any) plays
            order = self._resolve(seat, choice, view, now)
            if order is None or self._already(seat, order, view, now):
                continue
            key = (choice.priority if order == choice.base else choice.priority + 1, seat)
            if best is None or key < (best[0], best[1]):
                best = (key[0], seat, order, choice)
        if best is None:
            return WAIT
        _, seat, order, choice = best
        return self._emit(seat, order, view, choice.reason, now)

    def _lockstep(self, view: TeamView, plan: dict[int, Choice], now: float):
        answered = self._answered.setdefault(view.version, set())
        if len(self._answered) > 8:
            for v in sorted(self._answered)[:-4]:
                self._answered.pop(v, None)
        seats = [s for s in (view.pending or sorted(view.offers)) if s in view.offers and s not in answered]
        seats.sort(key=lambda s: (plan[s].priority if s in plan else 99, s))
        for seat in seats:
            choice = plan.get(seat)
            order = self._resolve(seat, choice, view, now) if choice is not None else None
            if order is not None:
                return self._emit(seat, order, view, choice.reason if choice else "", now)
            idle = self._idle_action(seat, view, now)  # a dead hero: the do-nothing button
            if idle is not None:
                return idle
        return WAIT

    def _resolve(self, seat: int, choice: Choice, view: TeamView, now: float) -> str | None:
        """The first of the choice, its alternatives and the generic chain that is on offer
        for this hero and not resting after a refusal."""
        offers = view.offers.get(seat)
        if offers is None:
            return None
        chain = [choice.base, *choice.alternatives, "macro_attack", "macro_advance", "macro_retreat",
                 *offers.macros]
        for order in dict.fromkeys(chain):
            if self._is_refused(seat, order, view, now):
                continue
            if order in offers.macros:
                return order
            if order in RAW_ORDERS and offers.raw_for(order) is not None:
                return order
        return None

    def _already(self, seat: int, order: str, view: TeamView, now: float) -> bool:
        """Real time: whether the hero already plays this order (no need to send it)."""
        standing = view.standing.get(seat)
        last = self._sent.get(seat)
        if order == HOLD:
            # A raw action clears the standing order: holding = no standing order after our hold.
            return standing is None and last is not None and last[0] == HOLD
        if order == HEAL:
            # Once: the skill leaves the offer while it cools down; don't fire it twice in a row.
            return last is not None and last[0] == HEAL and now - last[1] < RESEND_S
        if standing == order:
            return True
        if last is not None and last[0] == order and now - last[1] < RESEND_S:
            return True  # just sent: the server's report may lag a step
        if not view.standing_known and last is not None and last[0] == order:
            return True  # no standing orders reported: trust what was accepted
        if self._inflight is not None and self._inflight[0] == seat and self._inflight[1] == order \
                and now - self._inflight[4] < RESEND_S:
            return True
        return False

    def _emit(self, seat: int, order: str, view: TeamView, reason: str, now: float):
        offers = view.offers[seat]
        if order in RAW_ORDERS:
            button = offers.raw_for(order)
            assert button is not None  # _resolve checked it
            if button.needs_fields:
                move: Any = button.fill_fields(centred=True)
                form = "raw"
            else:
                move = button.action
                form = "action_id"
        elif self._use_action_id:
            move = offers.macros[order].action
            form = "action_id"
        else:
            move = {"macro": order, "seat": seat}
            form = "dict"
        self._inflight = (seat, order, form, view.version, now)
        if not view.realtime:
            self._answered.setdefault(view.version, set()).add(seat)
        if self._reasoning and reason:
            return WithReasoning(move, f"hero {seat}: {reason}"[:200])
        return move

    def _idle_action(self, seat: int, view: TeamView, now: float):
        """Lockstep, a hero with no macro (dead): the do-nothing button, else any listed
        button with its first legal values."""
        offers = view.offers.get(seat)
        if offers is None:
            return None
        hold = offers.hold()
        buttons = sorted(offers.buttons.values(), key=lambda o: (o is not hold, o.needs_fields, o.base))
        for o in buttons:
            if self._is_refused(seat, o.base, view, now):
                continue
            self._inflight = (seat, o.base, "action_id", view.version, now)
            self._answered.setdefault(view.version, set()).add(seat)
            if not o.needs_fields:
                return o.action
            filled = o.fill_fields()
            if filled is not None:
                self._inflight = (seat, o.base, "raw", view.version, now)
                return filled
        return None

    def _is_refused(self, seat: int, order: str, view: TeamView, now: float) -> bool:
        rest = self._refused.get((seat, order))
        if rest is None:
            return False
        until, version = rest
        if view.realtime:
            return now < until
        return version == view.version

    # -- the safety layer ---------------------------------------------------------------------

    def _guard(self, decision: Any, state: GameState):
        """``decision`` if it is a legal answer for this state, else a listed fallback."""
        if decision is not None and _legal(decision, state):
            return decision
        if decision is not None:
            self._trouble("illegal", f"unlisted answer {decision!r}; using a listed fallback")
        else:
            self.stats["fallbacks"] += 1
        return self._fallback(state)

    def _fallback(self, state: GameState):
        """A listed action for the first pending hero: attack if listed, else advance, else
        its first entry (``WAIT`` when nothing is listed, or in real time when that hero
        already plays it)."""
        offers = offers_by_seat(state.legal_actions or [])
        if not offers:
            return WAIT
        raw = state.raw if isinstance(state.raw, dict) else {}
        realtime = isinstance(raw.get("pacing"), dict) and raw["pacing"].get("mode") == "realtime"
        sim = raw.get("simultaneous") if isinstance(raw.get("simultaneous"), dict) else {}
        pending = [p for p in (sim.get("your_pending_seats") or []) if isinstance(p, int) and p in offers] \
            if isinstance(sim.get("your_pending_seats"), list) else []
        standing = sim.get("standing_orders") if isinstance(sim.get("standing_orders"), dict) else {}
        for seat in pending or sorted(offers):
            entry = offers[seat]
            pick: Offer | None = next((entry.macros[m] for m in FALLBACK_ORDER if m in entry.macros), None)
            if pick is None:
                listed = entry.all()
                pick = next((o for o in listed if not o.needs_fields), None)
                if pick is None and listed:
                    filled = listed[0].fill_fields()
                    if filled is not None:
                        return filled
            if pick is None:
                continue
            if realtime and pick.base in MACROS and standing.get(str(seat)) == pick.base:
                continue
            self._inflight = (seat, pick.base, "action_id", int(getattr(state, "state_version", 0) or 0),
                              self._clock())
            return pick.action
        return WAIT

    # -- results -----------------------------------------------------------------------------

    def _on_result(self, result: Any) -> None:
        inflight, self._inflight = self._inflight, None
        error = None
        if not isinstance(result, dict):
            return
        if result.get("error"):
            error = str(result.get("error"))
        elif result.get("accepted") is False:
            error = "NOT_ACCEPTED"
        now = self._clock()
        if error is None:
            self.stats["accepted"] += 1
            if inflight is not None:
                seat, order, form, _version, _ = inflight
                self._sent[seat] = (order, now)
                if form == "dict":
                    self._dict_accepted += 1
                    self._dict_refused_streak = 0
            return
        self.stats[f"refused:{error}"] += 1
        detail = str(result.get("detail") or "")[:240]
        if inflight is None:
            self._trouble("refused", f"{error}: {detail}")
            return
        seat, order, form, version, _ = inflight
        self._refused[(seat, order)] = (now + REFUSE_S, version)
        if len(self._refused) > 200:
            self._refused = {k: v for k, v in self._refused.items() if v[0] > now}
        self._trouble("refused", f"hero {seat} {order} ({form}) refused: {error}: {detail}")
        if error == "INVALID_ACTION" and form == "dict":
            self._dict_refused_streak += 1
            if self._dict_refused_streak >= FORMAT_SWITCH_REFUSALS and not self._dict_accepted \
                    and not self._use_action_id:
                self._use_action_id = True
                self._log("[hok] macro actions keep being refused: sending listed action ids instead")

    # -- logging and pacing -------------------------------------------------------------------

    def _idle(self, state: GameState) -> None:
        raw = state.raw if isinstance(state.raw, dict) else {}
        pacing = raw.get("pacing") if isinstance(raw.get("pacing"), dict) else {}
        if pacing.get("mode") != "realtime" or self._pace <= 0:
            return
        delay = self._pace - (self._clock() - self._last_idle)
        if delay > 0:
            self._sleep(min(delay, self._pace))
        self._last_idle = self._clock()

    def _trouble(self, kind: str, message: str, exc: BaseException | None = None) -> None:
        self.stats[kind] += 1
        n = self.stats[kind]
        if n <= LOG_FIRST or n % LOG_EVERY == 0:
            self._log(f"[hok] {kind} #{n}: {message}")
            if exc is not None and n <= 2:
                self._log("[hok] " + "".join(traceback.format_exception(exc)).strip().replace("\n", "\n[hok] "))

    def _status(self, view: TeamView, plan: dict[int, Choice]) -> None:
        t = view.game_seconds
        if t - self._last_status < STATUS_GAME_SECONDS and self._last_status > -math.inf:
            return
        self._last_status = t
        dealt, taken = self.policy.score(view)
        kills = sum(h.kills or 0 for h in view.heroes.values())
        deaths = sum(h.deaths or 0 for h in view.heroes.values())
        heroes = []
        for seat in view.seats:
            h = view.heroes.get(seat)
            if h is None:
                heroes.append(f"{seat}:?")
            elif h.dead:
                heroes.append(f"{seat}:dead")
            else:
                hp = f"{h.hp_frac:.0%}" if h.hp_frac is not None else "?"
                c = plan.get(seat)
                heroes.append(f"{seat}:{hp} {c.base.replace('macro_', '') if c else '-'}")
        left = view.seconds_to_limit()
        self._log(
            f"[hok] t={t:.0f}s v={view.version} left={'?' if left is None else f'{left:.0f}s'} "
            f"posture={self.policy._posture} structures dealt/taken={dealt:.0f}/{taken:.0f} "
            f"k/d={kills:.0f}/{deaths:.0f} enemies_seen={len(view.visible_enemies())} "
            f"enemies_dead~{self.policy.enemies_dead} | {' '.join(heroes)} | "
            f"sends={self.stats['sends']} waits={self.stats['waits']} refused={sum(v for k, v in self.stats.items() if k.startswith('refused:'))} "
            f"fallbacks={self.stats['fallbacks'] + self.stats['illegal']}"
        )


# -- legality -------------------------------------------------------------------------------------


def _legal(decision: Any, state: GameState) -> bool:
    """Whether ``decision`` is an answer the server lists for this state (or ``WAIT``)."""
    if decision is WAIT:
        return True
    if isinstance(decision, WithReasoning):
        inner = decision.action
        return inner is not WAIT and not isinstance(inner, WithReasoning) and _legal(inner, state) \
            and (decision.reasoning_summary is None or isinstance(decision.reasoning_summary, str))
    actions = state.legal_actions or []
    if isinstance(decision, LegalAction):
        return any(a.action_id == decision.action_id for a in actions)
    if isinstance(decision, str):
        return any(a.action_id == decision for a in actions)
    if not isinstance(decision, dict):
        return False
    seat = decision.get("seat")
    if not isinstance(seat, int) or isinstance(seat, bool):
        return False
    offers = offers_by_seat(actions).get(seat)
    if offers is None:
        return False
    if "macro" in decision:
        return set(decision) == {"macro", "seat"} and decision["macro"] in offers.macros
    button = decision.get("which_button")
    if not isinstance(button, int) or isinstance(button, bool):
        return False
    for offer in offers.buttons.values():
        payload = offer.payload
        if payload.get("which_button") != button:
            continue
        ok = True
        for key, value in decision.items():
            if key in ("seat", "which_button"):
                continue
            legal = payload.get(f"legal_{key}")
            if isinstance(legal, list):
                if value not in legal:
                    ok = False
                    break
            elif payload.get(key) != value:
                ok = False
                break
        needed = [k[len("legal_"):] for k in payload if k.startswith("legal_")]
        if ok and all(n in decision for n in needed):
            return True
    return False
