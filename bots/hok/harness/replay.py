"""Replay a live HoK recording through the bot, and report what the real payloads look like.

    .venv/bin/python -m bots.hok.harness.replay ~/srv/friendly-fighter-hok/recordings/<session>/seat<N>.jsonl.gz
    .venv/bin/python -m bots.hok.harness.replay <file> --schema-only
    .venv/bin/python -m bots.hok.harness.replay <file> --knobs '{"retreat_hp": 0.35}' --show 40

The recorder (agent/recorder.py) writes every state the agent decided on, each
move it sent and each answer. This tool:

1. **Schema report**: the fields actually present (top level, ``my_hero``,
   ``other_heroes``, ``structures``, ``simultaneous``, ``pacing``), the structure
   types, the macro labels' shapes (does "nearest: <what>, <d> away" hold?), the
   raw buttons and their ``legal_*`` fields, the standing orders' format, and
   every refusal the server sent with its detail. This is where the known
   unknowns of README.md get answered.
2. **Game summary**: duration, steps, our heroes' deaths and kills, structure
   HP over time, the order mix we sent.
3. **Replay**: each recorded state through a fresh agent (its clock is the
   recording's), checking every answer is legal, counting where it differs
   from what was played live (expected after a policy change).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

from altruagent import WAIT, DecisionContext, GameState, WithReasoning

from .. import create_agent
from ..agent import _legal
from ..view import read_view

GAME = "honor_of_kings_5v5"
_LABEL_SHAPE = re.compile(r"\(nearest: (.*?), (\d+(?:\.\d+)?) away\)")


def rows(path: Path) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn last line from a killed worker
            if isinstance(row, dict):
                yield row


def describe(move: Any) -> str:
    if isinstance(move, WithReasoning):
        move = move.action
    if isinstance(move, dict) and "action" in move and "reasoning" in move:
        move = move["action"]
    if move is WAIT or move == "WAIT":
        return "WAIT"
    if isinstance(move, dict):
        if "macro" in move:
            return f"s{move.get('seat')}:{move['macro']}"
        return json.dumps(move, sort_keys=True)
    text = str(getattr(move, "action_id", move))
    m = re.search(r"action_id='([^']+)'", text)
    return m.group(1) if m else text


def schema_report(all_rows: list[dict]) -> list[str]:
    keys: dict[str, Counter] = defaultdict(Counter)
    struct_types: Counter = Counter()
    labels: dict[str, Counter] = defaultdict(Counter)
    label_shapes: Counter = Counter()
    buttons: dict[str, set] = defaultdict(set)
    standing_values: Counter = Counter()
    phases: Counter = Counter()
    pacing: Counter = Counter()
    refusals: Counter = Counter()
    targets: Counter = Counter()
    max_steps, time_limits = set(), set()
    for row in all_rows:
        kind = row.get("kind")
        if kind == "result":
            res = row.get("result")
            if isinstance(res, dict) and res.get("error"):
                refusals[f"{res.get('error')}: {str(res.get('detail'))[:120]}"] += 1
            elif isinstance(res, dict):
                keys["play_action answer"].update(res.keys())
            continue
        if kind != "state":
            continue
        raw = row.get("raw")
        if not isinstance(raw, dict):
            continue
        keys["state"].update(raw.keys())
        phases[str(raw.get("phase"))] += 1
        pacing[json.dumps(raw.get("pacing"), sort_keys=True)[:80]] += 1
        max_steps.add(raw.get("max_steps"))
        time_limits.add(raw.get("time_limit_s"))
        sim = raw.get("simultaneous")
        if isinstance(sim, dict):
            keys["simultaneous"].update(sim.keys())
            for v in (sim.get("standing_orders") or {}).values():
                standing_values[type(v).__name__ + ":" + str(v)[:40]] += 1
        obs = raw.get("observation")
        if isinstance(obs, dict):
            keys["observation"].update(obs.keys())
            for seat_entry in (obs.get("seats") or {}).values():
                if not isinstance(seat_entry, dict):
                    continue
                keys["observation.seats[*]"].update(seat_entry.keys())
                summary = seat_entry.get("summary")
                if not isinstance(summary, dict):
                    continue
                keys["summary"].update(summary.keys())
                if isinstance(summary.get("my_hero"), dict):
                    keys["my_hero"].update(summary["my_hero"].keys())
                for o in summary.get("other_heroes") or []:
                    if isinstance(o, dict):
                        keys["other_heroes[*] visible" if o.get("visible") is not False else "other_heroes[*] fogged"].update(o.keys())
                for s in summary.get("structures") or []:
                    if isinstance(s, dict):
                        keys["structures[*]"].update(s.keys())
                        struct_types[f"camp {s.get('camp')} {s.get('type')}"] += 1
        legal = raw.get("legal_actions")
        for a in (legal or {}).get("actions") or [] if isinstance(legal, dict) else []:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("action_id"))
            base = aid.split(":", 1)[-1]
            label = str(a.get("label"))
            labels[base][re.sub(r"\d+", "N", label)[:90]] += 1
            m = _LABEL_SHAPE.search(label)
            label_shapes[f"{base}: {'nearest-shape' if m else 'other'}"] += 1
            if m:
                targets[re.sub(r"\d+", "N", m.group(1))] += 1
            action = (a.get("input") or {}).get("action")
            if base.startswith("button") and isinstance(action, dict):
                buttons[base].add(json.dumps({k: (v if not isinstance(v, list) else f"list[{len(v)}]: {v[:4]}...")
                                              for k, v in action.items()}, sort_keys=True)[:160])
    out = ["== schema report =="]
    out.append(f"phases: {dict(phases)}")
    out.append(f"pacing: {dict(pacing)}")
    out.append(f"max_steps: {max_steps}  time_limit_s: {time_limits}")
    for name, counter in keys.items():
        out.append(f"{name} keys: {sorted(counter)}")
    out.append(f"structure types (rows): {dict(struct_types.most_common(20))}")
    out.append(f"label shapes: {dict(label_shapes)}")
    out.append(f"label targets ('nearest: ...'): {dict(targets.most_common(15))}")
    for base, counter in sorted(labels.items()):
        out.append(f"labels {base}: {[t for t, _ in counter.most_common(4)]}")
    for base, shapes in sorted(buttons.items()):
        out.append(f"button {base}: {sorted(shapes)[:3]}")
    out.append(f"standing order values: {dict(standing_values.most_common(10))}")
    out.append(f"refusals: {dict(refusals.most_common(15)) or 'none'}")
    return out


def game_summary(all_rows: list[dict]) -> list[str]:
    states = [r for r in all_rows if r.get("kind") == "state" and isinstance(r.get("raw"), dict)]
    actions = [r for r in all_rows if r.get("kind") == "action"]
    out = ["== game summary =="]
    if not states:
        return out + ["no states"]
    first, last = states[0], states[-1]
    out.append(f"{len(states)} states, {len(actions)} answers, {last.get('t', 0) - first.get('t', 0):.0f} s wall, "
               f"state_version {first['raw'].get('state_version')} -> {last['raw'].get('state_version')}")
    mix = Counter(describe(a.get("move")).split(":", 1)[-1] for a in actions)
    out.append(f"answers: {dict(mix.most_common())}")
    marks = [states[int(i * (len(states) - 1) / 4)] for i in range(5)]
    for row in marks:
        view = read_view(GameState.from_mcp_state(row["raw"]))
        dealt = sum((s.max_hp or 0) - (s.hp or 0) for s in view.enemy_structures() if s.kind != "spawn")
        taken = sum((s.max_hp or 0) - (s.hp or 0) for s in view.own_structures() if s.kind != "spawn")
        heroes = " ".join(
            f"{s}:{'dead' if h.dead else (f'{h.hp_frac:.0%}' if h.hp_frac is not None else '?')}"
            f"/L{h.level or '?'}/k{h.kills or 0}d{h.deaths or 0}" for s, h in sorted(view.heroes.items()))
        out.append(f"v={view.version} t={view.game_seconds:.0f}s camp {view.camp}: listed structure damage "
                   f"dealt/taken {dealt:.0f}/{taken:.0f}, enemies visible {len(view.visible_enemies())} | {heroes}")
    if isinstance(last["raw"].get("result"), dict):
        out.append(f"result: {json.dumps(last['raw']['result'])[:300]}")
    return out


def replay(all_rows: list[dict], *, knobs: dict | None, show: int) -> list[str]:
    t_now = [0.0]
    agent = create_agent(knobs, clock=lambda: t_now[0], sleep=lambda s: None, log=lambda _l: None)
    context = DecisionContext(session_id="replay", tournament_id=None, game_type=GAME, agent_id="replay")
    out = ["== replay =="]
    n = differ = illegal = 0
    live_next: dict[int, str] = {}
    for i, row in enumerate(all_rows):
        if row.get("kind") == "state":
            j = i + 1
            while j < len(all_rows) and all_rows[j].get("kind") not in ("action", "state"):
                j += 1
            live_next[i] = describe(all_rows[j].get("move")) if j < len(all_rows) and all_rows[j].get("kind") == "action" else "-"
    shown = 0
    for i, row in enumerate(all_rows):
        if row.get("kind") != "state" or not isinstance(row.get("raw"), dict):
            continue
        t_now[0] = float(row.get("t") or 0.0)
        state = GameState.from_mcp_state(row["raw"])
        if not state.is_current_actor:
            continue
        n += 1
        ours = agent.choose_action(state, context)
        if not _legal(ours, state):
            illegal += 1
        mine, live = describe(ours), live_next.get(i, "-")
        if mine != live:
            differ += 1
            if shown < show:
                shown += 1
                out.append(f"  v={row['raw'].get('state_version')}: replay {mine:28s} live {live}")
        agent.on_action_result({"accepted": True}, context)
    out.append(f"{n} decisions replayed: {illegal} illegal, {differ} differ from live; "
               f"agent exceptions {sum(v for k, v in agent.stats.items() if k.endswith('exception'))}, "
               f"fallbacks {agent.stats.get('fallbacks', 0)}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--knobs", default=None, help="policy knobs (JSON) for the replay")
    ap.add_argument("--schema-only", action="store_true")
    ap.add_argument("--show", type=int, default=20, help="differences to print")
    args = ap.parse_args(argv)
    all_rows = list(rows(args.path))
    for line in schema_report(all_rows):
        print(line)
    if args.schema_only:
        return 0
    for line in game_summary(all_rows):
        print(line)
    for line in replay(all_rows, knobs=json.loads(args.knobs) if args.knobs else None, show=args.show):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
