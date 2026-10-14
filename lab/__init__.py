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
LAB_DIR = Path(os.environ.get("FF_LAB_DIR") or ROOT / "sim" / "lab")
