"""The lab: a local laboratory for Red Alert bots, on top of the local platform (ffsim).

- ``lab.db``: the game database (SQLite, ``sim/lab/lab.db``): runs, games, who played whom with
  which version of which bot, results, score timelines, events, every batch of orders;
- ``lab.bots``: the bot registry (``lab/bots.json``): named bots and their versions;
- ``lab.arena``: plays games in virtual time with bots inside the process (the runtime's own
  play loop, ``altruagent.runner.run_game``), and writes them to the database;
- ``lab.batch``: many games at once, in worker processes;
- ``lab.ratings``, ``lab.league``, ``lab.report``: ratings, leagues and gates, reports;
- ``lab.human``: a seat for a person (or Claude) at the controls, with the game paused between moves;
- ``lab.branch``: new games from any moment of a recorded one (its engine trace).

``scripts/lab.py`` is the command line; docs/LAB.md the guide.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _main_checkout(root: Path) -> Path:
    """The repository's main checkout: ``root`` itself, or, in a linked git worktree (a family
    session, AGENTS.md), the checkout its ``.git`` file points into."""
    git = root / ".git"
    if git.is_file():
        text = git.read_text().strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text[7:].strip())  # <main>/.git/worktrees/<name>
            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                return gitdir.parent.parent.parent
    return root


MAIN_ROOT = _main_checkout(ROOT)
# Every session shares one lab (database, traces, moment sets, notes): the main checkout's sim/lab.
LAB_DIR = Path(os.environ.get("FF_LAB_DIR") or MAIN_ROOT / "sim" / "lab")
# A family session's name: its worktree's folder under .worktrees/ (AGENTS.md); None in the main checkout.
FAMILY = os.environ.get("FF_FAMILY") or (ROOT.name if ROOT != MAIN_ROOT and ROOT.parent.name == ".worktrees" else None)

# Lobby options every new lab game is played with, on top of ffsim's DEFAULT_LOBBY (2026-10-16):
# superweapons off. No player on the platform can fire support powers (the API has no such order),
# so the OpenRA AIs' nukes (and iron curtain, chronosphere) are a lab artifact. A branch keeps the
# lobby of the game it branches from. Part of the engine id (lab/bots.py _engine_hash), so games
# played otherwise are never reused for these.
LAB_LOBBY = {"techlevel": "nosuperweapons"}
