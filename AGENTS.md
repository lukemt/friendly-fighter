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
| `bots/<package>/README.md` | Each bot family: its idea and its results (the package is the family's name with underscores) |

## Roles

Several sessions may work at once, each in its own folder. There are two roles:

- **The coordinator:** the session in the main checkout, `/workspace`, on branch `main`. There is one.
- **A family session:** one per bot family, in `/workspace/.worktrees/<family>`, on branch
  `family/<family>`. It builds and tests bots of its own kind. To see which you are: `lab status`
  prints "this session: family X" or "the main checkout".

| Thing | Family session | Coordinator |
|---|---|---|
| `bots/<package>/`, `lab/bots.d/<family>.json`, `bots/frozen/<package>_*` | yours | reviews and merges |
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
11. **Paid Jev where the experiment needs its answers, within $1 an hour.** TypeSafe's Jev costs
    real credits, but little: about $0.005 a Jev seat in a 20-minute game (run 151), so the user's
    budget of $1 an hour, all sessions together, pays for ~150 Jev seats an hour. Use it: whole
    games and suites that test what Jev decides are fine now. Lab runs get no key unless they ask
    for a number of games: `FF_LAB_PAID_JEV=N lab ...` (*enforced: a run with more Jev games is
    refused, and so is a run that would take the last hour's spend over the budget*). Don't spend
    it on games that don't need Jev's answers: reruns of games already played, controls that don't
    ask Jev (`--oracle rules`). A local server (`JEV_URL` in your worktree's `.env`) is free and
    needs no opt-in.

## Starting a family session

The user or the coordinator runs, from `/workspace`:

```bash
.venv/bin/python scripts/session.py new <family>      # e.g. rush-hunter
cd /workspace/.worktrees/<family> && claude            # or codex; give it its brief
.venv/bin/python scripts/session.py list              # all family sessions: branches, bots, games
```

`new` creates the worktree and branch, `bots/<package>/` with a README template, and
`lab/bots.d/<family>.json`. The lab finds the shared database by itself. Nothing needs setting.

**Codex:** its own sandbox can't run in this container. Its sandboxing tool (bubblewrap) needs
Linux user namespaces, which the container doesn't allow; checked with `codex sandbox` on
codex-cli 0.157. So start Codex without its sandbox and with approvals on:
`codex -s danger-full-access -a on-request`. The lab needs that anyway: it writes to
`/workspace/sim/lab/` and `~/openra-sim/lanes/`, outside the worktree, and the engines talk over
localhost TCP. With its sandbox off, Codex is held only by this file and the git hook. Claude's
deny rules don't apply to it, so the hard rules above are Codex's to keep.

## Working in a family session

**Your area.**
- Your bots live in `bots/<package>/`: the family's name with underscores, because Python packages
  can't have hyphens (family `rush-wave`: `bots/rush_wave/`, bots named `rush-wave-...`).
- A module bot's factory is `bots.<package>.<module>:create_agent`. It can build on the scripted
  bot (`agent/redalert_bot.py`, `Knobs`) or on the champion's code (`bots/jev_bot.py`,
  `bots/jev_council.py`).
- Register with `lab bots add <family>-<name> --kind module --factory bots.<package>.x:create_agent
  [--kwargs '{...}']`. Names must start with `<family>-` (*enforced*). The entry goes to
  `lab/bots.d/<family>.json` by itself.
- Name your moment sets and experiments `<family>-...` too. Sets without a prefix are shared
  (made by the coordinator, e.g. `@rush-wave`): use them freely, but don't change them.

**The funnel** (docs/LAB_PLAN.md, Part 0). An idea moves up a stage only if it shows promise:

| Stage | Command (examples) | About |
|---|---|---|
| 0. Idea card | `lab experiment new <family>-<topic> --hypothesis "..."` | nothing |
| 1a. Play it by hand | `lab human start --moment ID --stop-after 180 --stop-quiet 45`, then `lab h ...` (the game waits between commands) | your time, almost no CPU |
| 1. Probe the situation | `lab probe @rush-wave --variants <family>-a,<family>-b --halving --stop-after 180 --stop-quiet 45` | 5-15 min |
| 2. Targeted screen | `lab compare ff-council-champ-v10,<family>-a --baselines ai-rush --games 40 --seed 2026 --sprt` | stops when clear, 5-15 min |
| 3. Fast suite | `lab compare ff-council-champ-v10,<family>-a --baselines @screen --games 40 --seed 2026 --sprt`, then `--baselines @smoke --games 2` | 15-30 min |
| 4. Gate | ask the coordinator (`lab note "<family>-a: ready for a gate, runs ..."`) | about an hour |

- **Manual play (stage 1a)** tries a tactic before anyone codes it, or finds out why a bot loses.
  - `--moment` plays a moment of a set as the probe would: same seat, opponent and stop rule, you
    as the variant. `lab probe SET --report` pairs it with the control (play that with
    `lab probe SET --moments ID --control-only ...`).
  - Autopilot `macro` runs the economy and leaves the army to you. `lab h note` records what you did.
  - A manual result is 1-3 games: it shows something *can* work, never that it's better. Write
    down the tactic as an idea card, code it as a setting, and probe it as a bot.
- **Probes on moments chosen by a bad outcome need a placebo** (`@rush-wave`, `@normal-fight`,
  `@late-rush`): any change beats a control that replays the selected bad outcome. Add
  `ff-council-champ-v10-placebo` as a variant and compare against it (docs/LAB.md, "Moments and
  probes"), and confirm with whole games.
- Put the champion first in `lab compare`: the first bot is the reference.
- **Screen on seed 2026 with `--sprt`** (docs/LAB.md, "Stop when clear"): the champion's games on
  that seed are already played and get reused, so a compare plays only your bot's games, and
  `--sprt` stops each candidate as soon as its answer is clear. A screen's BETTER is checked on
  a fresh seed (the gate): many sessions screening on one seed will find its lucky games.
- Pass `--experiment <family>-<topic> --note "..."` on every run.
- Freeze a candidate before you report its numbers: `lab bots freeze <family>-a --as <family>-a-v1`.
- Run one big run at a time. Check `lab status` first.

**Recording what you learn.**
- Keep `bots/<package>/README.md` current. It holds the idea, the bots, each result with its run
  ids, and the open questions.
- Use `lab note --experiment ...` for findings. Notes are shared and show in `lab status`.
- Commit often on `family/<family>`. That branch is your only branch: never commit to `main`.

**The board** (`lab board`) ranks every family's entries against the champion on one fixed
schedule: 6 opponents (the AIs, the exploiters, the contestant `ext-ryan`) × 8 games, 20 minutes.
- **Entering:** freeze a module bot, then tag the copy:
  `lab bots freeze <family>-a --as <family>-a-v1`, then `lab bots tag <family>-a-v1 --tags +board`.
  A knobs bot (kind `ff`) can't be frozen and is tagged as is. At most 2 entries per family, and
  module bots only when frozen (*enforced*).
- **Playing:** `lab board play` plays your entries' games now (48 per entry, ~10 minutes). Games
  already played the same way are reused. Otherwise the coordinator's `lab board run` plays them.
- **Reading:** `lab board` shows the standings and `lab board history` the snapshots. An entry's
  "vs reference" is its points a game minus the champion's on the same games.

**Handing work back.**
1. When a bot passes stage 3, write it up in the README.
2. Leave a `lab note`.
3. Commit, and tell the user. The coordinator reviews, runs the gate and merges.
4. If `main` moved on and you need it: `git merge main` into your branch (don't rebase shared history).

## The coordinator's duties

The coordinator keeps what the sessions share coherent, and does whatever is risky. It doesn't
build bots itself while families run: it would compete with them for slots, and its own changes
to shared code are the ones the rules are there to keep careful.

1. **The live bot.** Run the tournament runtime (start, stop, `agent/champion.py`) and protect
   `recordings/`. Change the live bot only on the user's go. Import platform games
   (`lab import`) after each live match.
2. **The champion.** Run the gates (stage 4) when a family reports a candidate, crown champions
   (`lab champion set`), and keep the champion history.
3. **Merging.** Merge a family with `git merge --no-ff family/<family>` from `main`. Before merging:
   - the tests pass (`.venv/bin/python -m pytest -q`),
   - the family's README is current,
   - shared code changes are reviewed against rule 8.

   Resolve conflicts between families.
4. **Shared infrastructure.** The engine (rule 3), the lab's code, the suites (`lab/suites.json`),
   the shared moment sets, and the board's settings (`lab/board.json`).
5. **Shared knowledge.** Keep `docs/STRATEGY.md`, which collects the families' findings, and this
   file. When a rule changes, change it here first.
6. **Announcements.** Post engine rebuilds, champion changes and board results as `lab note`s,
   which every session sees in `lab status`. Message Claude sessions directly as well; Codex can't
   receive messages.
7. **Briefs.** Start each family with a written brief in its README's first section: its idea,
   the weakness it targets (with evidence), the moment sets and baselines to start from, and what
   counts as done. Two families never chase the same idea.
8. **A fair share of the machine.** The slots prevent overload, but not one family taking every
   slot all day. Set priorities: big runs (leagues, long sweeps) overnight or by arrangement, one
   big run per family at a time. Watch `lab status`.
9. **The board** (`lab board`, docs/LAB.md). Run it after merges, new entries, or a champion change:
   `lab board run`, then post the standings as a note. Families enter frozen bots by tagging
   them `board`.
10. **Health checks**, about every few hours while families work:
    - `lab status`: stuck runs, slots held for hours;
    - `scripts/session.py list`: families with uncommitted work, branches far behind `main`
      (ask them to `git merge main`), sessions gone quiet.
11. **Closing a family** when it is done or stuck: merge what is useful, write its result into
    `docs/STRATEGY.md`, `scripts/session.py remove <family>`. The branch stays.
12. **Reporting to the user.** Tell the user what each family does and has shown, what looks
    promising, and the decisions that are theirs: switching the live bot, pushing, spending
    money, closing a family.

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
  module and every `bots/`/`agent/` module it imports). A branch or a whole game played the same way is
  reused, never replayed (`lab compare`/`play`/`gate` too: docs/LAB.md, "Played once"). Changing shared code changes the versions of the bots built on it.
