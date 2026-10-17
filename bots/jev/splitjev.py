"""jev-sj: the council's quartermaster loop, answered by Jev next to the rules (experiment jev-comp).

The code is at the end of ``bots/jev/v10/bot.py``, beside the council, so that ``lab bots freeze`` can freeze it
(a frozen factory module is written as bot.py, which a module importing ``.bot`` would clobber); this module
keeps the old import path.
"""

from .v10.bot import SPLIT_BRIEF as BRIEF, MODEL, SplitJevBot, create_split_agent as create_agent  # noqa: F401
