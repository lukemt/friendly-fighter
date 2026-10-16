# AGENTS.md: rules for every agent working here

> For Claude Code, Codex and any other agent. Read all of it before your first change, and follow
> it over your own defaults. `CLAUDE.md` imports this file. If something here is unclear or seems
> wrong, ask the user instead of guessing, and fix this file when the answer is known.

## What this is

**friendly-fighter**: a self-hosted Red Alert agent for the UCLA AltruAgent tournament. One user
owns it. The bot that plays live is **`ff-council-champ-v10`** (the champion), on the live
tournament runtime. Everything else is about finding a better bot: a local copy of the platform
(`ffsim/`) and a **lab** (`lab/`, `scripts/lab.py`) that plays bots against each other, records
every game and compares them.

Read before working:

| File | What it holds |
|---|---|
| `docs/STRATEGY.md` | What wins and loses, with evidence. Read it first. |
| `docs/LAB.md` | How to use the lab: every command |
| `docs/LAB_PLAN.md` | The plan, and Part 0: the funnel every idea goes through |
| `GAMES.md` | The game's rules (20-minute games, the tiebreak) |
| `docs/PROJECT_NOTES.md` | The project's history and setup |
| `docs/EXTERNAL_BOTS.md`, `docs/SIMULATOR.md` | Contestant bots; the local platform |
| `bots/<family>/README.md` | Each bot family: its idea and its results |

## Roles

Several sessions may work at once, each in its own folder. There are two roles:

- **The coordinator:** the session in the main checkout, `/workspace`, on branch `main`. There is one.
- **A family session:** one per bot family, in `/workspace/.worktrees/<family>`, on branch
  `family/<family>`. It builds and tests bots of its own kind. To see which you are: `lab status`
  prints "this session: family X" or "the main checkout".

| Thing | Family session | Coordinator |
|---|---|---|
| `bots/<family>/`, `lab/bots.d/<family>.json`, `bots/frozen/<family>_*` | yours | reviews and merges |
| Shared code: `agent/`, `altruagent/`, `ffsim/`, `lab/`, `scripts/`, `bots/*.py`, `lab/bots.json`, `lab/suites.json` | only small, opt-in changes (rule 8) | owns |
| `docs/STRATEGY.md`, `docs/LAB_PLAN.md`, `AGENTS.md`, `CLAUDE.md` | propose in your README | owns |
| The live runtime, `agent/champion.py`, `lab/champions.json`, `recordings/` | never | only with the user's go |
| The engine (`~/openra-sim`, `sim/openra-fork/`) | never | owns |
| Merging into `main`, full gates, crowning a champion | never | owns |

## Hard rules (both roles)

"Enforced" means the setup refuses it; everything else depends on you.

1. **Never push.** The GitHub repository is public: a push publishes our bots and strategy. Only
   the user pushes, or tells you to for one push (`FF_ALLOW_PUSH=1 git push ...`). *Enforced: a
   pre-push hook in every worktree; Claude's settings deny `git push`.*
2. **Never touch the live runtime.** Don't start, stop or restart `python -m agent --tournament`,
   don't edit `agent/champion.py`, and never delete or change anything in `recordings/` (a match
   may be writing it; a deleted open file there is lost for good). The coordinator does these only
   when the user says so.
3. **Never rebuild or change the engine** (`~/openra-sim/fork`, `~/openra-sim/lanes`,
   `sim/openra-fork/`) while games run: it breaks every running game. Only the coordinator does
   it, after `lab status` shows no slots in use, and it announces it with `lab note`.
4. **Lab games only through `scripts/lab.py`.** It plays on the shared database and takes the
   machine's game slots, so all sessions together never run more games than there are CPUs.
   *Enforced: every lab game waits for a slot (`FF_LAB_SLOTS`, default the CPU quota).* Don't
   start engines or `scripts/ffsim.py` runs of your own for experiments.
5. **Kill processes by PID**, after checking what they are. Never `pkill -f` (it kills your own
   shell). *Enforced for Claude.* Never kill another session's processes: `lab status` shows who
   holds the game slots.
6. **No secrets in commits or output.** API keys live only in `.env`, which is gitignored. Don't
   print them.
7. **Games are 20 minutes**, as on the platform. Claims need evidence: paired seeds, run ids, and a
   second seed before anything is called better (docs/STRATEGY.md: "one seed fooled us").
   10-minute games are only for opening checks, and say so.
8. **Shared code changes are small, opt-in and tested.** A family session may change shared code
   only when its bots need it. Then the change must be off by default, so that every existing
   bot plays exactly as before. It needs a test, and a line under "Core changes" in your
   README so the coordinator can review it. Never change what an existing bot does, and never
   edit `bots/frozen/` copies (frozen bots must never change).
9. **Don't destroy shared data.** The lab database, traces, moment sets and notes belong to every
   session. `lab forget` only your own runs (*enforced in family sessions*). Never delete
   `sim/lab/`.
10. **Ask before anything irreversible or outward-facing**: deleting what you didn't create,
    anything that leaves this machine, spending money (API credits), changing the live bot.

## Starting a family session

The user or the coordinator runs, from `/workspace`:

```bash
.venv/bin/python scripts/session.py new <family>      # e.g. rush-hunter
cd /workspace/.worktrees/<family> && claude            # or codex; give it its brief
.venv/bin/python scripts/session.py list              # all family sessions: branches, bots, games
```

`new` creates the worktree and branch, `bots/<family>/` with a README template, and
`lab/bots.d/<family>.json`. The lab finds the shared database by itself. Nothing needs setting.

**Codex:** its sandbox must allow writing to `/workspace/sim/lab/` and `~/openra-sim/lanes/` (the
shared lab and the engine lanes, outside the worktree), and local network connections (the
engines talk over localhost TCP). Without both, lab runs fail.

## Working in a family session

**Your area.**
- Your bots live in `bots/<family>/`.
- A module bot's factory is `bots.<family>.<module>:create_agent`. It can build on the scripted
  bot (`agent/redalert_bot.py`, `Knobs`) or on the champion's code (`bots/jev_bot.py`,
  `bots/jev_council.py`).
- Register with `lab bots add <family>-<name> --kind module --factory bots.<family>.x:create_agent
  [--kwargs '{...}']`. Names must start with `<family>-` (*enforced*). The entry goes to
  `lab/bots.d/<family>.json` by itself.
- Name your moment sets and experiments `<family>-...` too. Sets without a prefix are shared
  (made by the coordinator, e.g. `@rush-wave`): use them freely, but don't change them.

**The funnel** (docs/LAB_PLAN.md, Part 0). An idea moves up a stage only if it shows promise:

| Stage | Command (examples) | About |
|---|---|---|
| 0. Idea card | `lab experiment new <family>-<topic> --hypothesis "..."` | nothing |
| 1. Probe the situation | `lab probe @rush-wave --variants <family>-a,<family>-b --halving --stop-after 180 --stop-quiet 45` | 5-15 min |
| 2. Targeted screen | `lab compare ff-council-champ-v10,<family>-a --baselines ai-rush --games 8 --seed N` | 5-15 min |
| 3. Fast suite | `lab compare ff-council-champ-v10,<family>-a --baselines @ais,@exploiters --games 8 --seed N` | 15-25 min |
| 4. Gate | ask the coordinator (`lab note "<family>-a: ready for a gate, runs ..."`) | about an hour |

- Put the champion first in `lab compare`: the first bot is the reference.
- Pass `--experiment <family>-<topic> --note "..."` on every run.
- Freeze a candidate before you report its numbers: `lab bots freeze <family>-a --as <family>-a-v1`.
- Run one big run at a time. Check `lab status` first.

**Recording what you learn.**
- Keep `bots/<family>/README.md` current. It holds the idea, the bots, each result with its run
  ids, and the open questions.
- Use `lab note --experiment ...` for findings. Notes are shared and show in `lab status`.
- Commit often on `family/<family>`. That branch is your only branch: never commit to `main`.

**Handing work back.**
1. When a bot passes stage 3, write it up in the README.
2. Leave a `lab note`.
3. Commit, and tell the user. The coordinator reviews, runs the gate and merges.
4. If `main` moved on and you need it: `git merge main` into your branch (don't rebase shared history).

## The coordinator's duties

- **Merge** a family with `git merge --no-ff family/<family>` from `main`. Before merging:
  - the tests pass (`.venv/bin/python -m pytest -q`),
  - the README is current,
  - shared code changes are reviewed against rule 8.
- **Run gates, keep the champion** (`lab champion set`) and keep `docs/STRATEGY.md`, which
  collects the families' findings.
- **Handle the engine and the shared moment sets and suites.** Announce changes with `lab note`
  (and by message to other Claude sessions on this machine, where possible).
- **Change the live bot only on the user's go.**
- **Keep this file true.** When a rule changes, change it here first.

## The machine

- **CPUs:** about 5.5 (the Docker Desktop VM, shared with the host). Game slots default to the CPU
  quota, and every session's lab games share them. `lab status` shows who holds them, recent notes,
  and whether the live runtime runs. Lab games run at a lower priority than the live runtime.
- **Python:** `/workspace/.venv/bin/python`, shared by all sessions. The scripts put their own
  checkout first on the path, so a worktree runs its own code.
- **git:** add `-c safe.directory=<the checkout>` (and `-c safe.directory=/workspace` in a
  worktree), with identity `-c user.name="Lukas Petry" -c user.email=lukas.petry@pn-consulting.de`
  on each command. Never force-push, rewrite `main`, or delete a branch that isn't merged.
- **A stopped run leaves nothing behind:** its workers die with it, and their slots free
  themselves. Engine lanes stay loaded between runs on purpose.
- **Bot versions:** each bot's version is a hash of its settings and its code (a module bot: its
  module and every `bots/`/`agent/` module it imports). A branch played the same way is reused,
  never replayed. Changing shared code changes the versions of the bots built on it.
