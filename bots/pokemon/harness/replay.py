"""Replay a live recording through the bot: every recorded state, in order, through one agent.

    .venv/bin/python -m bots.pokemon.harness.replay /release/recordings/<session_id>/seat0.jsonl.gz [--why]

Prints each decision next to the one played live and counts fallbacks, so a change can be checked
against the platform's real payloads. With ``--why`` it lists each slot's best options on every turn.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from altruagent import DecisionContext, GameState

from ..agent import PokemonAgent, fmt
from ..battle import TurnPlanner, read_board

GAME = "pokemon_vgc_doubles_draft"


def states(path: Path):
    """(raw state, the move played live) pairs from a seat recording."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    for i, row in enumerate(rows):
        if "raw" not in row:
            continue
        played = rows[i + 1].get("move") if i + 1 < len(rows) and rows[i + 1].get("kind") == "action" else None
        yield row["raw"], played


def seat_id(path: Path) -> str:
    """Our seat's id: the seat whose turn it is in the first draft state we were asked about."""
    for raw, _ in states(path):
        obs = raw.get("observation")
        if isinstance(obs, dict) and obs.get("phase") == "draft" and obs.get("current_seat"):
            return str(obs["current_seat"])
    return ""


def describe(answer: object) -> str:
    if isinstance(answer, dict) and answer.get("type") == "doubles_turn":
        return f"{fmt(answer['slot_0'])} | {fmt(answer['slot_1'])}"
    if isinstance(answer, dict) and answer.get("type") == "select_lineup":
        return f"bring {answer['bring']} lead {answer['leads']}"
    if isinstance(answer, dict):
        return json.dumps(answer, sort_keys=True)
    return str(getattr(answer, "action_id", answer))


def live_text(played: object) -> str:
    if isinstance(played, str) and "action_id='" in played:
        return played.split("action_id='", 1)[1].split("'", 1)[0]
    return describe(played)


def why(agent: PokemonAgent, state: GameState, top: int = 4) -> None:
    template = state.legal_actions[0].input.get("action") or {}
    obs = state.raw.get("observation") or {}
    slots = sorted(template.get("slots") or [], key=lambda s: s.get("slot", 0))
    my_cards, foe_cards = agent._species_cards()  # noqa: SLF001
    board = read_board(obs, slots, my_cards, foe_cards)
    planner = TurnPlanner(board, slots)
    print(f"    field {board.fld}  sides {board.my_side} / {board.foe_side}")
    for i in range(2):
        me = board.mine[i]
        hp = f"{me.hp:.2f}" if me else "-"
        print(f"    slot {i} {me.mon.name if me else None} hp {hp}"
              f"  incoming/ko {tuple(round(x, 2) for x in planner.incoming(i)) if me else '-'}")
        mine, theirs = planner.options(i), planner.options(1 - i)
        # An option's worth is its best pair score: its damage is only counted in score_pair.
        best = [(max((planner.score_pair(o, p) if i == 0 else planner.score_pair(p, o)) for p in theirs), o)
                for o in mine]
        for score, o in sorted(best, key=lambda x: -x[0])[:top]:
            print(f"      {fmt(o.send):24s} pair {score:+.2f}  own {o.value:+.2f}  p_act {o.p_act:.2f}  {o.note or ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording", type=Path)
    ap.add_argument("--why", action="store_true", help="show each slot's best options every turn")
    args = ap.parse_args(argv)
    me = seat_id(args.recording)
    ctx = DecisionContext(session_id="replay", tournament_id=None, game_type=GAME, agent_id=me, seat_position=0)
    agent = PokemonAgent(log=False)
    same = differ = 0
    for raw, played in states(args.recording):
        state = GameState.from_mcp_state(raw)
        if not state.legal_actions:
            continue
        before = agent.fallbacks
        answer = agent.choose_action(state, ctx)
        obs = raw.get("observation") or {}
        tag = obs.get("phase") or ("turn %s" % obs.get("turn"))
        now, live = describe(answer), live_text(played)
        mark = "=" if now == live else "!"
        same += now == live
        differ += now != live
        fb = "  FALLBACK" if agent.fallbacks > before else ""
        print(f"{mark} {tag:>14}: {now}{'' if now == live else f'   (live: {live})'}{fb}")
        if args.why and isinstance(answer, dict) and answer.get("type") == "doubles_turn":
            why(agent, state)
    print(f"{same} same as live, {differ} different, {agent.fallbacks} fallbacks of {agent.decisions} decisions")
    return 1 if agent.fallbacks else 0


if __name__ == "__main__":
    sys.exit(main())
