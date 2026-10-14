# The lab

A laboratory for Red Alert bots on this machine, on top of the local platform (ffsim, see
[SIMULATOR.md](SIMULATOR.md)). It plays games between any bots, you included, and keeps everything
in one database: who played whom with which version of which bot, the result, the score every
5 seconds, every event, and every batch of orders. Every game can be looked at again at any moment
and played on from there with other bots. The command line is `scripts/lab.py` (below: `lab`):

```bash
alias lab='.venv/bin/python scripts/lab.py'
```

## Quick start

```bash
lab bots                                          # the bots, their versions, games played
lab play ff-default ai-normal --games 12          # 12 games (maps rotate, seats swap): ~75 s
lab league --tag ours ai-normal ai-rush --games 60
lab ratings --matrix                              # who is better than whom
lab report --last 40                              # what the last 40 games say
lab games --bot ff-default --limit 10             # then: lab show <game>
lab human start --vs ai-normal                    # play yourself: lab h go 10, lab h attack 60 66 all ...
lab look <game> --at 6:30                         # the board at that moment of a recorded game
lab branch <game> --at 6:00 --variants ff-turtle,ff-rush --repeats 3 --stop-after 120
lab gate ff-turtle --champion ff-default --games 6
lab dashboard                                     # sim/lab/dashboard.html: ratings, games with charts, runs, notes
lab import                                        # the games our runtime played on the platform
lab status                                        # the database, engine lanes, human session, live runtime
```

## Bots

`lab/bots.json` lists the bots by name (`lab bots`, `lab bots show NAME`):

| Kind | What plays | Settings |
|---|---|---|
| `ff` | our agent (`agent/agent.py`, the tournament bot) | `knobs`: posture, attack_threshold, unit_focus, target |
| `module` | any agent factory | `factory: "package.module:function"`, `kwargs` |
| `ai` | OpenRA's own AI, inside the engine | `type`: normal, rush, turtle, naval |
| `human` | you (or Claude) at the controls | (`lab human`) |

**Bots in the list today:**
- **Our variants:** `ff-default` (the submitted bot), `ff-turtle`, `ff-rush`, `ff-eco`, `ff-harass` and `ff-tanks`.
- **Slow versions:** `ff-slow` and `ff-sluggish` are `ff-default` deciding in 0.8 and 2.5 s, like LLM agents.
- **The starter's LLM player:** `starter-llm`. It needs `OPENAI_API_KEY` and `FF_SIM_OPENAI=1`
  (lab processes blank the key otherwise). Its think time is `measured`, so its real latency is game time.
- **Weak baselines:** `builder` builds a base and never an army; `idle` never acts.
- **OpenRA's AIs:** `ai-normal`, `ai-rush`, `ai-turtle` and `ai-naval`.

Each bot has a `think` value: the game time each of its decisions costs, which is 5 ms for our agent.
A slower bot is a bigger number, not a sleep, so it costs no wall time.

Add a bot:

```bash
lab bots add ff-big-waves --kind ff --knobs '{"attack_threshold": 16, "unit_focus": "vehicles"}' --desc "..." --tags ours
lab bots add my-bot --kind module --factory bots.my_bot:create_agent
```

A new kind of bot is any module with a `create_agent()` that returns what the runtime expects (an
object with `choose_action(state, context)`, as `agent/agent.py`).

**Versions.** A bot's version (`ff-default@41fd6065`) is a hash of its settings and of the code it
runs (for `ff`: `agent/agent.py`, `agent/redalert_bot.py`, `agent/commander.py`,
`examples/llm/redalert.py`, `altruagent/runner.py`). Every seat in the database names its
version, so a change to the bot shows up as a new version even before it is committed. Each
run also records the git commit, with `+` when the tree had uncommitted changes. `--version current` limits
`stats`/`ratings` to the code as it is now.

## Playing games

```bash
lab play A B --games 12 [--map all|trapped|sidestep,trapped] [--minutes 10] [--seed 1] [--workers 4]
lab league BOT... [--tag ours] --games 60 [--balance]
lab gate CANDIDATE --champion ff-default --baselines ai-normal,ai-rush,ai-turtle --games 6
```

- **`play`:** A against B. Maps rotate, seats swap every other game, and the seeds come from `--seed`.
- **`league`:** every pair of the bots, the pairs with the fewest games first.
  - `--balance` counts the games already in the database.
  - Two OpenRA AIs can't play each other here: the lab steps the engine for agents.
- **`gate`:** the candidate plays the champion, and both play every baseline on the same seeds
  (paired games). It fails if the candidate doesn't beat the champion head to head, or does
  clearly worse than the champion against any baseline.

`--note TEXT` and `--experiment NAME` label a run. Games are played by worker processes (default:
the CPU quota, 4 here), each on an engine lane of its own; their bots' log lines go to
`sim/lab/logs/run-<id>-w<n>.log`.

**Priority.** Workers and their engines run at nice 10, so a live tournament match on this machine
keeps priority (`lab status` says when the live runtime is up).

**Draws against an OpenRA AI.** When a game against an OpenRA AI ends level on kills, the
platform would compare assets, but the AI has no view here, so its assets are unknown. Such games
are recorded as draws: in practice the 0-0 games where nobody fought.

**Reproducible.** The same bots, map and seed play the same game every time. Bots run inside the
lab's worker process with the runtime's own play loop (`altruagent.runner.run_game`) on
`lab/driver.py`'s `LocalGame`. That replaces only the HTTP and the processes: every call still
goes through the virtual clock and JSON, as on the wire. Checked against the real runtime
(`scripts/ffsim.py run`, the same seed, think 5 ms):
- **Against the Normal AI:** the same game, down to the factions, kills, losses, assets and units.
- **Self-play:** the lab gave the same result 3 times out of 3, and one of two runtime runs
  matched it exactly. The runtime's processes can drift a little; the lab doesn't.

**Cost.**
- **Time:** a 10-minute game takes 15-25 s in a worker, so with 4 workers about 6 s a game, or
  ~600 games an hour.
- **Disk:** a game is ~40 KB of trace plus its database rows.

## Playing yourself

```bash
lab human start --vs ai-normal [--map trapped] [--seat 0] [--autopilot macro] [--name claude]
lab human start --vs ai-normal --from <game> --at 5:00     # take over a recorded game at 5:00
lab h help
lab h state | map | map 60 66 10 | units [3tnk|combat] | enemies | events | log
lab h build powr | train 3tnk 3 | place powr [X Y] | attack 60 66 all | move 40 40 12,13 | target 455 idle
lab h go 10                                               # play 10 seconds of game time
lab h autopilot off|macro|full | knobs posture=defend | note "flanking works here" | resign | quit
```

The game **waits for you**. Between commands no game time passes: your seat's think time is
`paused` in the virtual clock. `go N` plays N seconds:
- your queued orders go out in the first batch;
- then each view, the autopilot adds its own orders;
- at the end you see what happened: your orders, any that were refused, and the events (fights,
  losses, kills, buildings).

**Autopilot modes:**
- **`macro`** (the default): our bot runs the economy and production (building, placing,
  training, harvesters, repairs, rallies) and gives the army no orders, so the army is yours.
  It also places every finished building, including defenses you ordered; switch to `off` to
  place them yourself.
- **`full`:** our bot plays everything, and your orders are added on top.
- **`off`:** only your orders.

The map (`lab h map`) shows our buildings (`F` construction yard, `B` building, `D` defense), our
units (`U` combat, `H` harvester) and what we see of the enemy (`X` building, `x` unit) on the
terrain. `map X Y R` zooms to full size around a cell.

Your game is saved like any other lab game: the bot is `human-<name>`, and every command and its
answer is in `human_log`. One human session at a time; it runs in the background
(`sim/lab/human/session-<run>.log`) until `lab h quit`.

## Looking back

```bash
lab games [--bot X] [--vs Y] [--map M] [--run R] [--since 2h] [--branches]
lab show <game> [--decisions 30]      # seats, results, the score every 30 s, buildings, fights, reasoning
lab look <game> --at 6:30 [--seat 0] [--units]   # the board then (replayed from the trace, ~4 s)
lab watch <game> --every 0:20 --from 7:00 --to 9:00 [--zoom 46,56,16] [--no-map]   # a time-lapse
lab replay <game>                     # check that the trace replays exactly
lab stats <bot> --by opponent|map|faction|seat|version [--vs Y] [--version current]
lab h2h A B
lab ratings [--bots a,b,c] [--by-version] [--matrix]
lab report [--last 40 | --run R | --since 1h] [--bots ff-default]
lab runs | lab sql "select ..."
```

Games are named by their id. Any unique start of it works (`lab show 800536ca`), and so do `last`
and `last-1`.

- **`show`:** the game in detail.
  - **Seats:** results, kills and losses in credits, assets, units and buildings killed and lost,
    batches and refused orders.
  - **Every 30 s:** both sides' score, cash, army value, units and buildings.
  - **Events:** our buildings made and lost, and fights (losses and kills less than 10 s apart:
    what we lost, how many we killed).
  - **Reasoning:** with `--decisions N`, the bot's reasoning each time it changed.
- **`look`:** rebuilds any moment from the engine trace: the replay is checked step by step against
  the trace, and you get the seat's summary, the events before it and the map. For a platform
  game (no trace) it shows the recorded state at that moment.
- **`watch`:** the game as a time-lapse, from one replay of its trace: every N seconds, each
  agent seat's score, army, cash, buildings and enemies in sight, and the map (whole, or a
  full-size window with `--zoom X,Y,R`). It is the way to see how a fight unfolded.
- **`report`:** the observer's report on a batch of games, per bot:
  - wins, draws and losses;
  - the mean score at each minute (when a bot falls behind);
  - what it loses per game by value;
  - kills, orders and refusals;
  - results per opponent;
  - its worst games, each with its biggest 30-second swing and what was lost in it;
  - games without a result or with bot errors.
- **`ratings`:** a Bradley-Terry fit over all games on the Elo scale, with standard errors; draws
  count half.
- **`stats`:** win rates come with 95 % intervals.

Branch games are left out of statistics and ratings unless you pass `--branches`, because they don't start at the start.

**Platform games.** `lab import` reads our runtime's recordings of real matches
(`recordings/<session>/`). It skips the local simulator's `sim-*` ones, and running it again adds only
new games. Each becomes a game with:
- **Bots:** `ff-live` for our seat, `platform:<name>` for the opponent.
- **The record:** the result, the score every 5 s, the events and every batch.

`stats`, `report`, `show` and `look` work on them too, but they have no trace to branch from.

**Housekeeping.** `lab forget --run R --yes` (or `--game G`) removes a run's games and traces: a
test run, or one played with a bug. `lab dashboard` writes `sim/lab/dashboard.html`, a single
page with:
- ratings and the pair matrix;
- the recent games, each opening to its charts of score, army and cash, and its fights;
- bots, runs, experiments and notes.

## Branching: other choices from the same moment

```bash
lab branch <game> --at 6:00 --variants ff-turtle,ff-rush,ff-default [--seat 0] [--opponent BOT]
           [--repeats 3] [--stop-after 120] [--no-control]
lab branches <game>
```

Every lab game keeps its engine trace, and the game is deterministic. So the engine can rebuild
the world at any moment by replaying the trace, checked step by step against the recorded views,
and new games go on from there:
- **The branched seat:** played by each variant in turn.
- **The control:** the original bot, added automatically unless the seat was a person's.
- **The other seat:** its original bot, or `--opponent`.

Every branch starts from the same world. `--repeats` varies the seeds, which changes the timing
after the snapshot. `--stop-after S` ends each branch S seconds later, on the score there.
`lab branches` compares the variants by how the score moved since the snapshot: after 30, 60
and 120 s, and to the end.

**Caveats:**
- **Fresh memory.** The bots start the branch with fresh memory, the control included, which is
  what makes it the fair comparison.
- **Same players.** A branch keeps the game's players: an agent seat can get any bot, but an
  OpenRA AI stays the same AI (it lives in the world).

## Experiments and notes

```bash
lab experiment new turtle-vs-normal --hypothesis "defending beats attacking into the Normal AI"
lab play ff-turtle ai-normal --games 24 --experiment turtle-vs-normal
lab note --experiment turtle-vs-normal "turtle 9/24 vs default 3/24 on the same seeds"
lab experiment close turtle-vs-normal --conclusion "..."
lab experiment show turtle-vs-normal
lab note --game <game> "the wave walked into two pillboxes at 6:29"
```

## What is where

| Path | Contents |
|---|---|
| `sim/lab/lab.db` | the database (SQLite; `lab sql`, or any SQLite client) |
| `sim/lab/traces/<game>.trace.gz` | each game's engine trace (replay, look, branch) |
| `sim/lab/logs/` | the workers' logs (the bots' log lines) |
| `sim/lab/human/` | the human session's address and logs |
| `sim/lab/dashboard.html` | the dashboard (`lab dashboard`) |
| `lab/bots.json` | the bot registry (committed) |

Database tables:

| Table | Contents |
|---|---|
| `runs` | each command that played games, with its git commit and note |
| `bot_versions` | each version of each bot that played |
| `games` | map, seed, length, how the game ended, trace; branch origin |
| `seats` | bot, version, faction, result, kills, losses, assets, net, batches, refusals |
| `timeline` | each agent seat every 5 s: cash, ore, power, harvesters, units, army value, kills, losses, assets, explored, enemies in sight |
| `events` | units and buildings made and lost, enemies spotted and killed, attacks |
| `decisions` | every batch: tick, orders, reasoning, verdicts (compressed; `LabDB.decisions(game, seat)`) |
| `notes` | notes on games, runs and experiments |
| `experiments` | what we tried and what we concluded |
| `human_log` | every command at a human seat |

An OpenRA AI has no view of its own: its kills and losses are the other seat's, mirrored, and its
assets are unknown.

## How it fits together

| Module | Role |
|---|---|
| `lab/arena.py` | plays one game: the match on an in-process platform, a seat per bot, the capture; saves it |
| `lab/driver.py` | `LocalGame` (`MCPGameSession`'s methods on the local platform) and `BotSeat` (`run_game` on a thread) |
| `lab/capture.py` | a match watcher: timeline, events, batches and verdicts |
| `lab/batch.py` | worker processes, one lane each |
| `lab/human.py` | the human seat and its session server |
| `lab/branch.py` | snapshots from traces (`look`) and branch games |
| `lab/league.py`, `lab/ratings.py`, `lab/report.py` | scheduling, ratings, reports |
| `lab/views.py` | the text summary and map |
| `lab/importer.py` | platform recordings into the database |
| `lab/dashboard.py` | the HTML dashboard |

The platform side gained these:
- **Per-seat think times:** `ffsim/vclock.py`'s `seat_think`, including `paused`.
- **Match watchers:** `Match.watchers`, and the event tracker's `sink`.
- **Per-game traces and snapshots:** in `ffsim/engine_openra.py`, `OpenRAEngine(trace_path=, prefix=)`.
- **Trace reading:** `ffsim/trace.py`.
