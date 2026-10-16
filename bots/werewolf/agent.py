"""The Werewolf agent: reads each state, decides by role, talks, and never answers illegally.

One ``WerewolfAgent`` per match (the worker creates it once), so ``Memory`` carries from day to day.
Every answer passes ``check_action`` / ``check_message`` against what the server offered; any
exception, or an answer the checks refuse, falls back to a legal one (a vote: the most suspicious
legal seat, never an abstention by default; a message: ``TERMINATE_MESSAGING``) and is logged.
"""

from __future__ import annotations

import random
import re
import traceback
from typing import Any, Callable

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, LegalAction, SendMessage

from .belief import Belief, Memory
from .chat import ABSTAIN, MAX_WORDS, N_SEATS, parse_message
from .policy import Settings, night_seer, night_wolf, vote_villager, vote_wolf
from .talk import ScriptedTalker, Talker, TalkContext, Utterance
from .view import SEER, WOLF, Snapshot, read_snapshot

WEREWOLF = "werewolf"
MAX_LOG = 50

_SEER_RESULT_RE = r"player\s*{t}\b[^\n]{{0,40}}?(?:(not\s+(?:a\s+)?(?:wolf|werewolf))|(wolf|werewolf)|(villager))"


class Unacceptable(ValueError):
    pass


def check_action(move: Any, state: GameState) -> None:
    """Raise ``Unacceptable`` unless ``move`` is one of the actions the server offered right now."""
    if not isinstance(move, LegalAction):
        raise Unacceptable(f"not a LegalAction: {move!r}")
    if not any(a.action_id == move.action_id for a in state.legal_actions):
        raise Unacceptable(f"{move.action_id!r} is not in the legal actions {[a.action_id for a in state.legal_actions]}")


def check_message(message: Any, snap: Snapshot) -> None:
    """Raise ``Unacceptable`` unless ``message`` can be sent: limits, recipients."""
    if message is TERMINATE_MESSAGING:
        return
    if not isinstance(message, SendMessage):
        raise Unacceptable(f"not a SendMessage: {message!r}")
    words = message.content.split()
    if not words or len(words) > MAX_WORDS or len(message.content) > 1024:
        raise Unacceptable(f"message length: {len(words)} words")
    if len(message.recipients) > 1:
        raise Unacceptable("2+ recipients are refused by the platform")
    for r in message.recipients:
        if r == snap.me or r not in snap.alive or not 0 <= r < N_SEATS:
            raise Unacceptable(f"bad recipient {r}")


class WerewolfAgent:
    def __init__(self, *, settings: Settings | None = None, talker: Talker | None = None,
                 log: Callable[[str], None] | None = None, record: Callable[[Any, dict], None] | None = None) -> None:
        self.settings = settings or Settings()
        self.talker: Talker = talker or ScriptedTalker()
        self.mem = Memory()
        self.errors: list[str] = []  # what went wrong, newest last (bounded)
        self._log = log if log is not None else _default_log
        self._record = record  # record(context, dict): the runtime's recorder, to keep what we said and saw
        self._rng: random.Random | None = None

    # -- the runtime's hooks ---------------------------------------------------------------------------

    def choose_action(self, state: GameState, context: DecisionContext):
        snap = None
        try:
            snap = self._read(state, context)
            move = self._decide(state, context, snap)
            check_action(move, state)
            return move
        except Exception as exc:  # noqa: BLE001 -- the fallback below must always run
            self._fail("action", exc)
            return self._fallback_action(state, snap)

    def choose_message(self, state: GameState, context: DecisionContext):
        try:
            snap = self._read(state, context)
            self._note_view(state, context)
            message = self._speak(state, context, snap)
            check_message(message, snap)
            if message is not TERMINATE_MESSAGING:
                self._note_sent(snap.day, message)
            else:
                self.mem.done.add(snap.day)
            self._note_message(context, message)
            return message
        except Exception as exc:  # noqa: BLE001
            self._fail("message", exc)
            return TERMINATE_MESSAGING

    def on_action_result(self, result: dict, context: DecisionContext) -> None:
        return None

    # -- reading ---------------------------------------------------------------------------------

    def _rng_for(self, context: DecisionContext) -> random.Random:
        if self._rng is None:
            self._rng = random.Random(f"{context.session_id}:{context.seat_position}")
        return self._rng

    def _read(self, state: GameState, context: DecisionContext) -> Snapshot:
        snap = read_snapshot(state, context)
        self._ingest_messages(snap)
        if snap.role == SEER:
            self._read_checks(snap)
        return snap

    def _ingest_messages(self, snap: Snapshot) -> None:
        mem = self.mem
        for m in snap.messages:
            seq = getattr(m, "index", None)
            if seq in mem.seen or getattr(m, "type", "chat") != "chat" or m.sender == snap.me:
                mem.seen.add(seq)
                continue
            mem.seen.add(seq)
            private = bool(m.recipients)
            if private and snap.role != WOLF:
                continue
            events = parse_message(m.content, m.sender, already_claimed=m.sender in mem.claims,
                                   from_partner=m.sender == snap.partner, private=private)
            if private and m.sender != snap.partner:
                events = []
            mem.add_events(events, snap.day)

    def _read_checks(self, snap: Snapshot) -> None:
        """Our own seer results, from the observation text (the platform puts them there)."""
        mem = self.mem
        for t in list(mem.my_checks) + ([mem.pending_check] if mem.pending_check is not None else []):
            if t in mem.my_checks:
                continue
            m = re.search(_SEER_RESULT_RE.format(t=t), snap.observation, re.I)
            if m:
                mem.my_checks[t] = "villager" if (m.group(1) or m.group(3)) else WOLF
                mem.check_days[t] = snap.day
                mem.version += 1
                if mem.pending_check == t:
                    mem.pending_check = None
        if mem.pending_check is not None and mem.pending_check in snap.dead:
            # no result text found, but the target's role came out in public
            d = snap.dead[mem.pending_check]
            if d.role:
                mem.my_checks[mem.pending_check] = WOLF if d.role == WOLF else "villager"
                mem.check_days[mem.pending_check] = snap.day
            mem.pending_check = None

    # -- deciding ----------------------------------------------------------------------------------

    def _decide(self, state: GameState, context: DecisionContext, snap: Snapshot) -> LegalAction:
        rng = self._rng_for(context)
        by_id = {a.action_id: a for a in state.legal_actions}
        if not by_id:
            raise Unacceptable("no legal actions")
        bel = Belief(snap, self.mem)
        sub = snap.sub_phase
        if sub == "night_seer" or (sub is None and snap.role == SEER and ABSTAIN not in snap.legal):
            seat = night_seer(snap, self.mem, bel, rng)
            self.mem.pending_check = seat
        elif sub == "night_wolf":
            seat = night_wolf(snap, self.mem, bel, rng, self.settings)
        elif sub == "day_vote" or ABSTAIN in snap.legal:
            if snap.role == WOLF:
                seat = vote_wolf(snap, self.mem, bel, Belief(snap, self.mem, private=False), rng, self.settings)
            else:
                seat = vote_villager(snap, self.mem, bel, rng, self.settings)
        else:
            raise Unacceptable(f"unknown sub-phase {sub!r}")
        move = by_id.get(str(seat))
        if move is None:
            raise Unacceptable(f"chose {seat}, not in {sorted(by_id)}")
        return move

    def _fallback_action(self, state: GameState, snap: Snapshot | None) -> LegalAction:
        """A legal answer without any of the above: the most suspicious seat we can name, else the first."""
        legal = list(state.legal_actions)
        if not legal:
            return None  # type: ignore[return-value] -- the runner raises its own error for no move
        try:
            voting = any(a.action_id == str(ABSTAIN) for a in legal)
            if snap is not None:
                bel = Belief(snap, self.mem)
                seats = [int(a.action_id) for a in legal if a.action_id != str(ABSTAIN)]
                if seats:
                    if snap.sub_phase == "night_wolf" and not voting:
                        seat = max(seats, key=lambda s: (bel.p_seer(s), -s))
                    else:
                        seat = max(seats, key=lambda s: (bel.p_wolf(s), -s))
                    for a in legal:
                        if a.action_id == str(seat):
                            return a
        except Exception as exc:  # noqa: BLE001
            self._fail("fallback", exc)
        non_abstain = [a for a in legal if a.action_id != str(ABSTAIN)]
        return (non_abstain or legal)[0]

    # -- talking -----------------------------------------------------------------------------------

    def _speak(self, state: GameState, context: DecisionContext, snap: Snapshot):
        mem, st = self.mem, self.settings
        day = snap.day
        if not st.talk or day in mem.done or mem.chats.get(day, 0) >= st.max_chats or snap.eliminated:
            return TERMINATE_MESSAGING
        bel = Belief(snap, mem)
        village = Belief(snap, mem, private=False)
        ctx = TalkContext(snap, mem, bel, village, st, self._rng_for(context))
        utt = self.talker.next_message(ctx)
        if utt is None or not isinstance(utt, Utterance):
            return TERMINATE_MESSAGING
        utt = Utterance(_trim(utt.text), tuple(utt.recipients), utt.act, utt.commit)
        self._pending = utt
        return SendMessage(utt.text, list(utt.recipients))

    def _note_sent(self, day: int, message: SendMessage) -> None:
        utt = getattr(self, "_pending", None)
        self._pending = None
        self.mem.chats[day] = self.mem.chats.get(day, 0) + 1
        if utt is not None:
            self.mem.sent.setdefault(day, []).append(utt.act)
            if utt.commit is not None:
                utt.commit()

    # -- records and errors ------------------------------------------------------------------------

    def _note_view(self, state: GameState, context: DecisionContext) -> None:
        if self._record is not None:
            self._record(context, {"kind": "message_state", "game_type": WEREWOLF, "raw": state.raw})

    def _note_message(self, context: DecisionContext, message: Any) -> None:
        if self._record is not None:
            body = "terminate" if message is TERMINATE_MESSAGING else {"content": message.content, "recipients": message.recipients}
            self._record(context, {"kind": "message", "message": body})

    def _fail(self, where: str, exc: BaseException) -> None:
        line = f"{where}: {type(exc).__name__}: {exc}"
        self.errors.append(line)
        del self.errors[:-MAX_LOG]
        self._log(f"[ff] werewolf {line}\n" + "".join(traceback.format_exception(exc)[-3:]))


def _trim(text: str) -> str:
    from .chat import trim

    return trim(text)


def _default_log(line: str) -> None:
    try:
        from altruagent.console import print_line

        print_line(line)
    except Exception:  # noqa: BLE001
        print(line)


def create_agent(*, settings: dict | Settings | None = None, record: Callable[[Any, dict], None] | None = None,
                 talker: Talker | None = None, log: Callable[[str], None] | None = None) -> WerewolfAgent:
    """One agent per match. ``settings``: a ``Settings`` or a dict of its fields (for simulator ablations)."""
    if isinstance(settings, dict):
        settings = Settings(**settings)
    return WerewolfAgent(settings=settings, talker=talker, record=record, log=log)
