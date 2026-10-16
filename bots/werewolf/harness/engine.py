"""A local Werewolf engine that follows GAMES.md ("Werewolf") exactly, for evidence.

7 seats: 2 wolves, 1 seer, 4 villagers, dealt from the seed. Night 1 is peaceful (the seer looks,
nobody dies). Then, each round: day (one discussion window, then the vote, seat by seat with the
tally hidden until the last vote), night (the wolves pick one at a time, the lower seat wins a
disagreement; then the seer looks; the kill lands at dawn). Plurality lynches; a tie or all-abstain
lynches nobody. Every death reveals the true role. Villagers win when both wolves are dead; wolves
at parity (wolves >= living non-wolves) or after 3 days in a row with no vote-out. Discussion: up
to 5 chats of 50 words per player per day, private messages to one seat only.

States are built in the shapes the docs describe (``GameState.from_mcp_state`` over a raw dict with
``game_state``, ``players``, ``new_messages``, ...), so the real agent code runs unchanged. The
shapes of ``dead`` and ``vote_history`` are our reading of the docs; the replay tool
(``harness/replay.py``) is how they are checked against real recordings.

Unknown in the docs, and set by ``Rules``: whether a night kill also resets the 3-day stall counter
(the docs say "3 consecutive days end with no elimination"; we default to the strict reading, where
only a vote-out resets it).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

N = 7
ABSTAIN = 7
WOLF, SEER, VILLAGER = "wolf", "seer", "villager"
MAX_CHATS = 5
MAX_WORDS = 50
ROLE_DECK = [WOLF, WOLF, SEER] + [VILLAGER] * 4


@dataclass(frozen=True)
class Rules:
    night_kill_resets_stall: bool = False
    stall_days: int = 3
    max_rounds: int = 12  # discussion rounds before the window is cut (a safety cap)


@dataclass
class Outcome:
    winner: str  # "wolf" | "villager"
    reason: str
    days: int
    roles: list[str]
    chats: int = 0


@dataclass
class Msg:
    seq: int
    sender: int
    recipients: list[int]
    content: str
    day: int


class Engine:
    def __init__(self, seed: int, make_player: Callable[[int, str, random.Random], Any], *, rules: Rules | None = None,
                 roles: list[str] | None = None, trace: list | None = None) -> None:
        """``make_player(seat, role, rng)`` returns the agent for that seat (the engine tells the role to
        the factory only so a harness can place bots by role; agents must read their role from the state)."""
        self.rules = rules or Rules()
        self.rng = random.Random(f"engine:{seed}")
        self.seed = seed
        self.roles = list(roles) if roles else self.rng.sample(ROLE_DECK, N)
        self.alive = set(range(N))
        self.dead: list[dict] = []
        self.vote_history: list[dict] = []
        self.day = 1
        self.sub_phase: str | None = "night_seer"
        self.messages: list[Msg] = []
        self.seen: dict[int, int] = {i: 0 for i in range(N)}  # last message seq shown to each seat
        self.checks: dict[int, list[tuple[int, str]]] = {i: [] for i in range(N)}
        self.stall = 0
        self.winner: str | None = None
        self.reason = ""
        self.version = 0
        self.session_id = f"sim-{seed}"
        self.trace = trace
        self.players = [make_player(i, self.roles[i], random.Random(f"player:{seed}:{i}")) for i in range(N)]
        self.contexts = [DecisionContext(self.session_id, None, "werewolf", f"a{i}", seat_position=i) for i in range(N)]
        self.chats = 0

    # -- helpers ---------------------------------------------------------------------------------

    @property
    def wolves(self) -> list[int]:
        return [i for i in range(N) if self.roles[i] == WOLF]

    def alive_wolves(self) -> list[int]:
        return [i for i in sorted(self.alive) if self.roles[i] == WOLF]

    def _check_win(self) -> bool:
        wolves = len(self.alive_wolves())
        others = len(self.alive) - wolves
        if wolves == 0:
            self.winner, self.reason = VILLAGER, "both wolves dead"
        elif wolves >= others:
            self.winner, self.reason = WOLF, "parity"
        else:
            return False
        self.sub_phase = None
        return True

    def _kill(self, seat: int, cause: str) -> None:
        self.alive.discard(seat)
        self.dead.append({"day": self.day, "player": f"Player{seat}", "role": self.roles[seat], "cause": cause})

    def observation(self, seat: int) -> str:
        role = self.roles[seat]
        lines = [f"You are Player{seat}. Your role: {role}."]
        if role == WOLF:
            mates = [w for w in self.wolves if w != seat]
            lines.append("Your fellow wolves: " + ", ".join(f"Player{w}" for w in mates) + ".")
        elif role == SEER:
            if self.checks[seat]:
                lines.append("Your investigation results: " + "; ".join(
                    f"Player{t} is a {'wolf' if v == WOLF else 'villager'}" for t, v in self.checks[seat]) + ".")
            else:
                lines.append("You have not investigated anyone yet.")
        else:
            lines.append("You do not know anyone else's role.")
        if seat not in self.alive:
            lines.append("You are DEAD. You may watch, but not act.")
        lines.append(f"Day {self.day}, phase {self.sub_phase}. Living players: {len(self.alive)}.")
        for d in self.dead:
            lines.append(f"Day {d['day']}: {d['player']} died ({d['cause']}); they were a {d['role']}.")
        for h in self.vote_history:
            votes = ", ".join(f"{k}->{v}" for k, v in h["votes"].items())
            lines.append(f"Day {h['day']} votes: {votes}. Eliminated: {h['eliminated']}.")
        if self.winner:
            lines.append(f"Game over: the {'wolves' if self.winner == WOLF else 'villagers'} won.")
        return "\n".join(lines)

    def _raw(self, seat: int, *, phase: str, legal: list[int], new: list[Msg]) -> dict:
        self.version += 1
        raw: dict = {
            "session_id": self.session_id, "game_type": "werewolf", "status": "completed" if self.winner else "in_progress",
            "phase": phase, "is_current_actor": bool(legal) and phase == "moving", "is_terminal": bool(self.winner),
            "state_version": self.version, "observation": self.observation(seat),
            "your_position": seat, "eliminated": seat not in self.alive,
            "players": [{"position": i, "name": f"Player{i}", "agent_id": f"a{i}"} for i in range(N)],
            "game_state": {
                "phase": self.sub_phase, "day": self.day, "alive": [f"Player{i}" for i in sorted(self.alive)],
                "dead": [dict(d) for d in self.dead],
                "vote_history": [{"day": h["day"], "votes": dict(h["votes"]), "eliminated": h["eliminated"]}
                                 for h in self.vote_history],
                "winner": self.winner,
            },
            "new_messages": [{"seq": m.seq, "sender": m.sender, "recipients": list(m.recipients), "content": m.content,
                              "type": "chat", "move_index": 0} for m in new],
            "messaging_enabled": True,
        }
        if legal:
            raw["legal_actions"] = {"session_id": self.session_id, "state_version": self.version, "actions": [
                {"action_id": str(a), "label": self._label(a), "input": {"session_id": self.session_id, "action_id": str(a)}}
                for a in legal]}
        return raw

    def _label(self, a: int) -> str:
        if a == ABSTAIN:
            return "Abstain"
        return {"night_wolf": "Kill", "night_seer": "Investigate", "day_vote": "Vote to eliminate"}.get(self.sub_phase or "", "") + f" Player{a}"

    def _visible(self, seat: int) -> list[Msg]:
        return [m for m in self.messages if m.seq > self.seen[seat] and (not m.recipients or seat in m.recipients or m.sender == seat)]

    # -- phases ----------------------------------------------------------------------------------

    def _ask(self, seat: int, legal: list[int]) -> int:
        new = self._visible(seat)
        if new:
            self.seen[seat] = max(m.seq for m in new)
        state = GameState.from_mcp_state(self._raw(seat, phase="moving", legal=legal, new=new))
        try:
            move = self.players[seat].choose_action(state, self.contexts[seat])
        except Exception as exc:  # noqa: BLE001 -- a crashing bot plays its first legal action, like a timeout
            move = None
            if self.trace is not None:
                self.trace.append(("crash", seat, repr(exc)))
        pick = getattr(move, "action", move)
        try:
            seat_choice = int(getattr(pick, "action_id", pick))
        except (TypeError, ValueError):
            seat_choice = legal[0]
        if seat_choice not in legal:
            if self.trace is not None:
                self.trace.append(("illegal", seat, seat_choice))
            seat_choice = legal[0]
        return seat_choice

    def _night(self) -> None:
        if self.day > 1:
            self.sub_phase = "night_wolf"
            picks = []
            for w in self.alive_wolves():
                picks.append(self._ask(w, [s for s in sorted(self.alive) if self.roles[s] != WOLF]))
            victim = min(picks)
        else:
            victim = None
        seer = next((i for i in sorted(self.alive) if self.roles[i] == SEER), None)
        if seer is not None:
            self.sub_phase = "night_seer"
            t = self._ask(seer, [s for s in sorted(self.alive) if s != seer])
            self.checks[seer].append((t, self.roles[t] if self.roles[t] == WOLF else VILLAGER))
        if victim is not None:
            self._kill(victim, "night_kill")
            if self.rules.night_kill_resets_stall:
                self.stall = 0
            self._check_win()

    def _window(self) -> None:
        self.sub_phase = "day_vote"
        done: set[int] = set()
        counts = {i: 0 for i in self.alive}
        for rnd in range(self.rules.max_rounds):
            living = sorted(self.alive)
            offset = self.rng.randrange(len(living))
            order = living[offset:] + living[:offset]
            active = [i for i in order if i not in done]
            if not active:
                break
            for seat in active:
                new = self._visible(seat)
                if new:
                    self.seen[seat] = max(m.seq for m in new)
                state = GameState.from_mcp_state(self._raw(seat, phase="messaging", legal=[], new=new))
                try:
                    msg = self.players[seat].choose_message(state, self.contexts[seat])
                except Exception as exc:  # noqa: BLE001
                    msg = TERMINATE_MESSAGING
                    if self.trace is not None:
                        self.trace.append(("crash-msg", seat, repr(exc)))
                if msg is TERMINATE_MESSAGING or not isinstance(msg, SendMessage):
                    done.add(seat)
                    continue
                if counts[seat] >= MAX_CHATS or len(msg.content.split()) > MAX_WORDS or len(msg.recipients) > 1 \
                        or any(r == seat or r not in self.alive for r in msg.recipients):
                    if self.trace is not None:
                        self.trace.append(("refused", seat, msg.content[:60]))
                    done.add(seat)
                    continue
                counts[seat] += 1
                self.chats += 1
                self.messages.append(Msg(len(self.messages) + 1, seat, list(msg.recipients), msg.content, self.day))
            if all(i in done for i in self.alive):
                break

    def _vote(self) -> None:
        self.sub_phase = "day_vote"
        ballots: dict[int, int] = {}
        for seat in sorted(self.alive):
            ballots[seat] = self._ask(seat, [s for s in sorted(self.alive) if s != seat] + [ABSTAIN])
        tally: dict[int, int] = {}
        for t in ballots.values():
            if t != ABSTAIN:
                tally[t] = tally.get(t, 0) + 1
        out = None
        if tally:
            top = max(tally.values())
            leaders = [t for t, c in tally.items() if c == top]
            if len(leaders) == 1:
                out = leaders[0]
        self.vote_history.append({"day": self.day, "votes": {f"Player{k}": ("abstain" if v == ABSTAIN else f"Player{v}")
                                                              for k, v in ballots.items()},
                                  "eliminated": f"Player{out}" if out is not None else None})
        if out is not None:
            self._kill(out, "vote")
            self.stall = 0
        else:
            self.stall += 1
        if self._check_win():
            return
        if self.stall >= self.rules.stall_days:
            self.winner, self.reason, self.sub_phase = WOLF, "stalling", None

    def play(self) -> Outcome:
        self._night()  # night 1: the seer looks
        if self.winner is None:
            while True:
                self._window()
                self._vote()
                if self.winner is not None:
                    break
                self.day += 1
                self._night()
                if self.winner is not None:
                    break
        # tell everyone the result once (agents may keep final state)
        return Outcome(self.winner or WOLF, self.reason, self.day, list(self.roles), self.chats)
