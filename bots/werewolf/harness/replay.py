"""Replay a live recording through the bot: every recorded view, in order, through one agent.

    .venv/bin/python -m bots.werewolf.harness.replay ~/srv/friendly-fighter-werewolf/recordings/<session>/seat3.jsonl.gz [--audit]

The runtime writes ``seat<N>.jsonl.gz`` with one JSON object per line: ``state`` (a view we were asked to
move on), ``action`` (what was played), ``message_state`` / ``message`` (the discussion views and what we
said, written by the Werewolf bot), ``result``. This tool feeds the states to a fresh ``WerewolfAgent`` and
prints each decision next to the one played live, then counts fallbacks (the bot's ``errors``), moves that
differ, and refused answers.

``--audit`` also prints what the bot READ from each view (role, partner, day, sub-phase, living, deaths,
votes, our seer checks, chat events): this is how the guessed payload shapes (``dead``, ``vote_history``,
the seer's results in the observation) are checked against what the platform really sends. A view where the
role is unknown, a wolf has no partner, or the observation mentions deaths the reader didn't find, is flagged.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState

from ..agent import WerewolfAgent
from ..view import read_snapshot

GAME = "werewolf"


def rows(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def live_action(row: dict | None) -> str:
    if row is None:
        return "?"
    move = row.get("move")
    if isinstance(move, str) and "action_id='" in move:
        return move.split("action_id='", 1)[1].split("'", 1)[0]
    if isinstance(move, dict):
        return json.dumps(move, sort_keys=True)
    return str(move)


def audit(state: GameState, context: DecisionContext) -> list[str]:
    """What the reader made of this view, plus the things that look wrong."""
    snap = read_snapshot(state, context)
    flags = []
    if snap.role is None:
        flags.append("ROLE UNKNOWN (observation: %r)" % state.observation[:120])
    if snap.role == "wolf" and snap.partner is None and len(snap.alive) > 1:
        flags.append("WOLF WITHOUT PARTNER (observation: %r)" % state.observation[:160])
    gs = (state.raw.get("game_state") or {}) if isinstance(state.raw, dict) else {}
    if gs.get("dead") and not snap.dead:
        flags.append("DEAD LIST NOT READ: %r" % (gs["dead"],))
    if gs.get("vote_history") and not snap.votes:
        flags.append("VOTE HISTORY NOT READ: %r" % (gs["vote_history"],))
    if not snap.alive:
        flags.append("NO LIVING SEATS READ")
    line = (f"role={snap.role} partner={snap.partner} day={snap.day} sub={snap.sub_phase} alive={sorted(snap.alive)} "
            f"dead={ {s: (d.role, d.cause, d.day) for s, d in snap.dead.items()} } votes={len(snap.votes)} "
            f"legal={snap.legal} msgs={len(snap.messages)}")
    return [line] + flags


def replay(path: Path, *, show_audit: bool = False, out=print) -> dict:
    data = rows(path)
    config = next((r for r in data if r.get("kind") == "config"), {})
    agent = WerewolfAgent(log=lambda line: out("    [bot] " + line.splitlines()[0]))
    first = next((r for r in data if r.get("kind") in ("state", "message_state")), None)
    raw0 = first["raw"] if first else {}
    seat = raw0.get("your_position", 0)
    context = DecisionContext(str(raw0.get("session_id", path.parent.name)), None, GAME, "replay", seat_position=seat)
    n = {"actions": 0, "same": 0, "messages": 0, "flags": 0}
    for i, row in enumerate(data):
        kind = row.get("kind")
        if kind == "state":
            state = GameState.from_mcp_state(row["raw"])
            nxt = next((r for r in data[i + 1:i + 3] if r.get("kind") == "action"), None)
            mine = agent.choose_action(state, context)
            played, said = live_action(nxt), getattr(mine, "action_id", str(mine))
            n["actions"] += 1
            n["same"] += played == said
            gs = state.raw.get("game_state") or {}
            out(f"  [{i}] day {gs.get('day')} {gs.get('phase')}: live {played}  bot {said}{'' if played == said else '   <- differs'}")
            if show_audit:
                for line in audit(state, context):
                    out("      " + line)
                    n["flags"] += not line.startswith("role=")
        elif kind == "message_state":
            state = GameState.from_mcp_state(row["raw"])
            nxt = next((r for r in data[i + 1:i + 3] if r.get("kind") == "message"), None)
            mine = agent.choose_message(state, context)
            said = "terminate" if mine is TERMINATE_MESSAGING else f"{mine.recipients or 'all'}: {mine.content}"
            live = "?" if nxt is None else json.dumps(nxt["message"])
            n["messages"] += 1
            out(f"  [{i}] message view (day {(state.raw.get('game_state') or {}).get('day')}): live {live}\n        bot  {said}")
            if show_audit:
                for line in audit(state, context):
                    out("      " + line)
                    n["flags"] += not line.startswith("role=")
    out(f"\n{path}: {n['actions']} moves ({n['same']} same as live), {n['messages']} message views, "
        f"bot errors/fallbacks {len(agent.errors)}, audit flags {n['flags']}; config: {bool(config)}")
    for e in agent.errors:
        out("  ERROR " + e)
    n["errors"] = len(agent.errors)
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path)
    ap.add_argument("--audit", action="store_true", help="print what the reader made of every view, flag misreads")
    a = ap.parse_args(argv)
    n = replay(a.recording, show_audit=a.audit)
    return 1 if n["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
