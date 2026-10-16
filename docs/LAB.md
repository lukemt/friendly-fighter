# The lab

A laboratory for Red Alert bots on this machine, on top of the local platform (ffsim, see
[SIMULATOR.md](SIMULATOR.md)). It plays games between any bots, you included, and keeps everything
in one database: who played whom with which version of which bot, the result, the score every
5 seconds, every event, and every batch of orders. Every game can be looked at again at any moment
and played on from there with other bots. The command line is `scripts/lab.py` (below: `lab`):

```bash
alias lab='.venv/bin/python scripts/lab.py'
```

**Where it goes next:** [LAB_PLAN.md](LAB_PLAN.md) (infrastructure, bots to add, experiments).

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
lab moments find late-rush --runs 116 --vs ai-rush --kind wiped --after 10:00   # a set of moments
lab probe late-rush --variants ff-turtle,ff-guard-turtle --stop-after 180      # every moment, each variant vs the control
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
| `ai` | OpenRA's own AI, inside the engine | `type`: normal, rush, turtle, naval, beginner, easy, medium |
| `human` | you (or Claude) at the controls | (`lab human`) |

**Bots in the list today:**
- **Our variants:** `ff-default` (the submitted bot), `ff-turtle`, `ff-rush`, `ff-eco`, `ff-harass` and `ff-tanks`.
- **Slow versions:** `ff-slow` and `ff-sluggish` are `ff-default` deciding in 0.8 and 2.5 s, like LLM agents.
- **The starter's LLM player:** `starter-llm`. It needs `OPENAI_API_KEY` and `FF_SIM_OPENAI=1`
  (lab processes blank the key otherwise). Its think time is `measured`, so its real latency is game time.
- **Weak baselines:** `builder` builds a base and never an army; `idle` never acts; `chaotic` has our
  economy, but sends its army to random places every 15-40 s (an erratic, LLM-like player).
- **OpenRA's AIs:** `ai-normal`, `ai-rush`, `ai-turtle` and `ai-naval`; weaker: `ai-beginner`, `ai-easy`
  and `ai-medium` (suite `@ais-all` has all seven).
- **Other contestants' bots** (`@field`, docs/EXTERNAL_BOTS.md): `ext-ryan` (ryanxxhuang's rule bot) and
  `ext-ryan-claude` (the same bot with Claude as its advisor, see "Claude as a bot's advisor").

Each bot has a `think` value: the game time each of its decisions costs, which is 5 ms for our agent.
A slower bot is a bigger number, not a sleep, so it costs no wall time.

Add a bot:

```bash
lab bots add ff-big-waves --kind ff --knobs '{"attack_threshold": 16, "unit_focus": "vehicles"}' --desc "..." --tags ours
lab bots add my-bot --kind module --factory bots.my_bot:create_agent
```

### Other contestants' bots

A bot built on the AltruAgent starter is a `module` bot with the factory `bots.external:create_agent`
and its repository pinned in `kwargs` (`repo`, `url`, `commit`; `env` for its settings; `stubs` to
switch off one of its functions). Its code is cloned into `sim/external/<repo>` (gitignored, never
copied into ours) and played in-process:

```bash
lab external fetch          # clone every contestant's bot at its pinned commit
lab external list           # which are ready
```

What `bots/external.py` does for each seat:
- **Its own copy, under another name.** Their `agent` package would clash with ours.
- **The game's clock.** Their `time.monotonic()` is the game time, tick / 25, so their timers keep
  their meaning in virtual time.
- **Its own environment.** `env` overlays the process environment for that seat only.
- **No API ever.** `anthropic` is a stub.

Their log lines are kept per seat (written to `sim/lab/external/<bot>/` with `LAB_EXTERNAL_LOGS=1`,
and always in advise sessions). A changed checkout or another commit is refused, so a bot's version
always says what played.

A new kind of bot is any module with a `create_agent()` that returns what the runtime expects (an
object with `choose_action(state, context)`, as `agent/agent.py`).

**Versions.** A bot's version (`ff-default@41fd6065`) is a hash of its settings and of the code it
runs (for `ff`: `agent/agent.py`, `agent/redalert_bot.py`, `agent/commander.py`,
`examples/llm/redalert.py`, `altruagent/runner.py`). Every seat in the database names its
version, so a change to the bot shows up as a new version even before it is committed. Each
run also records the git commit, with `+` when the tree had uncommitted changes. `--version current` limits
`stats`/`ratings` to the code as it is now.

**Freezing a version.** A module bot's file changes as we improve it; to keep a version playable
(a champion to gate against, an old self to play), freeze it:

```bash
lab bots freeze ff-jev-push --as ff-jev-push-v4   # copies to bots/frozen/ff_jev_push_v4/
lab gate ff-jev-push --champion ff-jev-push-v4 --baselines ai-normal,ai-rush,ai-turtle --games 6
```

The copy holds the bot's module and the scripted bot it builds on (`agent/redalert_bot.py`), with
the import pointed at the copy, so neither the bot's file nor the scripted bot changing moves it.
Its version hashes only its own files. Commit `bots/frozen/` with the registry.

### Bots steered by Jev

`ff-jev` and `ff-jev-push` (`bots/jev_bot.py`) are the scripted bot with the strategy Claude used
when playing by hand as a few decision loops answered by Jev, TypeSafe's typed decision model
(about 0.3 s a call, about $0.004 a game). Code counts and compares, and Jev judges:

- **Every 10 s of game time**, and every 3 s during a fight or an attack, the bot describes the game
  in words. The description covers the clock, the score, the recent trades, both armies, known
  defenses, raids, the odds in the current fight and past attacks. It asks Jev in one call who the
  opponent is, what the army should do (defend, gather, commit, retreat), what to train, and (in the
  first 4 minutes) which opening to play.
- **Code turns the answers into the scripted bot's knobs**, with rails from `docs/STRATEGY.md`:
  - attack only when behind, with a big army;
  - hold a lead in the last minute;
  - never turn around in a fight we are winning;
  - wait after a failed attack.
- **A scout** looks at the enemy base twice a game and turns back at the first sight of anything.
- **`ff-jev-push`** falls back from an attack only when Jev says so and the counted odds are against
  it too.

It needs `TYPESAFE_API_KEY` (in the environment or `.env`); without it, it plays as
`ff-guard-turtle`. Every call is logged to `sim/lab/jev/<session>-<seat>.jsonl`:

```bash
lab jev last              # what it decided over the game, and why (changes only; --all for every call)
lab jev last --at 7:40    # everything Jev was shown at that moment, and every probability it answered
lab jev                   # calls, tokens, cost and latency over every logged game
lab jev --attacks         # every logged attack by opponent: did it gain?
```

**Working on the questions without playing.** `lab/jev_cases.json` holds moments from real games,
plus a few written ones, each with the answer Claude would give and why. `lab jev eval` asks Jev
the bot's questions on every case in a few seconds and lists the misses. A variant of the playbook
or the criteria is a JSON file (`bots/jev_variants/`); try it there first, then play it as a bot
(`kwargs: {"questions": "bots/jev_variants/x.json"}`):

```bash
lab jev eval [--questions bots/jev_variants/literal2.json]
lab jev case GAME --at 7:40 --expect "posture=defend,gather;opponent=turtle" --why "ahead late: hold"
```

**Labels from outcomes.** `lab coach GAME --at 7:40` branches the game at that moment four ways,
with the Jev bot's posture forced to defend, gather, commit or retreat (`ff-jev-force-*`, which make
no calls). It prints what each scored from there. With `--label`, it saves the moment as an eval
case whose right answers are the postures that scored within `--margin` (300) of the best:

```bash
lab coach last --at 7:34 --repeats 2 --label
```

Lessons so far (docs/STRATEGY.md): write numbers literally, keep the options' criteria from
claiming the same situation, and say what was never seen.

**The council** (`ff-jev-council*`, `bots/jev_council.py`, docs/JEV_LOOPS.md) asks Jev as three
loops instead of one call:
- a strategist (opponent, plan, "attack soon?");
- a quartermaster (what to train);
- a field commander (posture, only when there is something to command).

Each loop has its own briefing in the state, its own view of the game and its own pace. It uses
about half the tokens of one call. `lab jev eval --council` asks the labelled cases the same way,
the strategist first. `lab jev` shows a council's requests and tokens by loop.

**When Jev is unavailable** (no key, out of credits, down), `lab play/compare/gate` refuse to start
a run with Jev bots and say why. After a run they warn, and leave a note on it, when Jev bots went
without answers: those games played on frozen decisions, so drop them from conclusions.

**Paid Jev is off in the lab unless a run asks for it** (AGENTS.md rule 11). Lab processes get the
key `off`, frozen Jev bots included, so they can't spend credits by accident. A run that needs
Jev's answers names how many games may use it: `FF_LAB_PAID_JEV=4 lab compare ...`. A run with
more Jev games than that is refused before it starts, and the run's parameters record
`paid_jev_games`. `lab jev eval` needs `FF_LAB_PAID_JEV=1`. A local server (`JEV_URL`) is free and
needs none of this.

**The budget: $1 an hour, all sessions together** (the user, 2026-10-16). A paid run is also
refused when its estimate plus what the call logs show was spent in the last hour goes over it
(`lab/jevlog.py` `spent`). The estimate is $0.0003 a Jev seat-minute, rounded up from run 151
($0.0023 a 10-minute seat), so about $0.006 a 20-minute seat: up to ~150 Jev seats in 20-minute
games an hour. `FF_LAB_JEV_BUDGET` sets another hourly amount. `lab jev` shows the spend.

Jev answers vary by about 0.02 in probability between identical calls, but the choices (and so the
seeded games) replay the same. `ff-jev-off` is the same code with Jev switched off (a control);
`ff-jev-v6-measured` charges its real decision time, Jev calls included, as game time, as the
platform would. Jev games take about 2.5 times the wall time of the others (the calls), but the
CPU is free meanwhile: run them with `--workers 7`.

## Playing games

Opponent sets have names (`lab/suites.json`): `@ais`, `@standard` (the AIs and our two turtles),
`@weak` (builder, idle), `@scripted` and `@wide`. Use them wherever bots are listed.

```bash
lab compare REF,B,C --baselines @standard --games 8     # several candidates on the same seeds, paired against REF
lab exploit TARGET --tries 16 --games 2 --finalists 4   # which settings of the scripted bot beat TARGET?
```

`lab curves [--runs 50,53]` shows each bot's army value, harvesters and assets at 3, 6 and 9
minutes. These measure the hands, not the result: why a bot loses before it loses. `lab champion`
shows the champion over time (`lab/champions.json`); `lab champion set BOT --run N --why ...` adds one.

`compare` is `gate` for several candidates. Each one plays each baseline on the same seeds, and
each one's difference from the reference (the first) comes with a 95% interval. The gate's verdict
also pools the paired games: BETTER, WORSE, or UNCLEAR with the number of games a difference that
size would need. `exploit` samples settings of the scripted bot (`knobs:posture=attack,...`; any
`knobs:` name plays without a registry entry). It plays each against the target, then plays the
best again, and prints the command to keep the winner as a sparring partner.

```bash
lab play A B --games 12 [--map all|trapped|sidestep,trapped] [--minutes 20] [--seed 1] [--workers 4]
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

**Played once** (`lab/reuse.py`, 2026-10-16). Since every game replays the same way, `play`,
`compare`, `league`, `gate` and `exploit` don't play a game again that the database already has:
the same bot versions in the same seats, map, seed, length and engine build, ended normally
(no bot errors, no stop rule). The run links it (table `run_games`) and its report counts it as
its own; the run's start says how many were reused. Every family puts the champion first in
`compare`, so its reference games on the usual seeds now cost nothing after the first time.
Never reused: bots whose games don't replay (Jev bots, language-model and advisor bots,
`think: measured`), branches and probes (they have their own reuse), and anything with
`--no-reuse`. A version or engine change means new games, as it should.

**No superweapons** (`LAB_LOBBY` in `lab/__init__.py`, 2026-10-16). No player on the platform
can fire support powers (the API has no such order), so lab games are played with the tech level
"no superweapons": no missile silo, iron curtain or chronosphere, so the OpenRA AIs can't nuke. The
AIs' airfield powers stay (paratroopers, Ukraine's parabombs, the spy plane): they would need a change
to the engine's rules. The lobby is part of the engine id, so a game played with superweapons is
never reused for one without; a branch keeps the lobby of the game it branches from (so old
moment sets still replay). The board counts only games of the current engine id: it starts over.

**Games a superweapon decided** (`lab/superweapons.py`). `lab superweapons [--bot B] [--decided]`
lists games where an agent seat lost 5+ buildings within 3 s against an OpenRA AI, with its net
just before and at the end; "decided" means it was level or ahead before and lost. Each game is
flagged in `info.superweapon` (`--flag` checks older games; new games are checked when saved,
since parabombs remain). On 2026-10-16: 91 whole games flagged, 68 likely decided. For v10 in 20-minute
games: Rush 9 of 104 games, Normal 9 of 44, Turtle 5 of 24 (repeats of the same game included).

**Cost.**
- **Time:** games are 20 minutes by default, the platform's length (tournament games are 20;
  pass `--minutes 10` for quick checks). A 10-minute game takes 15-25 s in a worker, a
  20-minute one about twice that, so with 4 workers about 12 s a game, or ~300 games an hour.
- **Disk:** a game is ~40 KB of trace plus its database rows.

## Playing yourself

```bash
lab human start --vs ai-normal [--map trapped] [--seat 0] [--autopilot macro] [--name claude]
lab human start --vs ai-normal --from <game> --at 5:00     # take over a recorded game at 5:00
lab h help
lab h state | map | map 60 66 10 | units [3tnk|combat] | enemies | events | log | info 3tnk tsla
lab h history                                             # where this opponent built, and what it fielded, before
lab h build powr | build tsla at 58 66 | train 3tnk 3 | rally all 61 66 | attack 60 66 all | move 40 40 12,13 then
lab h group tanks 3tnk | attack 40 50 @tanks | target 455 idle | stance defend all | bombard 31 44 @art
lab h go 10                                               # play 10 seconds of game time (stops early on trouble)
lab h autopilot off|macro|full | knobs posture=defend | note "flanking works here" | resign | quit
```

The game **waits for you**. Between commands no game time passes: your seat's think time is
`paused` in the virtual clock. `go N` plays N seconds:
- your queued orders go out in the first batch;
- then each view, the autopilot adds its own orders;
- it **stops early** when enemies reach your base or your units come under fire after a quiet
  spell (`go N nostop` runs through). A fight can be played 2-3 seconds at a time.

At the end you see what happened, in a few lines:
- **Activity:** what was made and built, what was spotted, and the fights (lost what, killed how many).
- **Feedback on your orders:** orders that were refused, and where a building really went.
- **Warnings:**
  - unspent money and idle factories;
  - artillery or harvesters with enemies close;
  - aircraft (riflemen can't hit them) and mines;
  - units killed by something out of sight, with the nearest known defenses;
  - the odds: enemies in sight against your units nearby.

Selections, memory and recall:
- **Groups:** `group NAME UNITS` saves a selection, then `@NAME` uses it.
- **Enemy memory:** `enemies` remembers every enemy building seen.
- **`history`:** asks the database where this opponent built in earlier lab games on this map,
  and which units it used. You see its defenses before you meet them.

**Special orders:**
- **`bombard X Y UNITS`:** fires at the ground, so artillery hits defenses it can't see.
- **`stance defend`:** units hold their ground.
- **`build ITEM at X Y`:** places the building there when it's ready.
- **`rally all X Y`:** every production building, new ones too, sends its units there.

What was learned playing OpenRA's AIs this way is in [STRATEGY.md](STRATEGY.md).

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

## Claude as a bot's advisor

A bot that asks a model for advice can ask Claude instead. Each request becomes a question at the
desk, and the game waits for the answer. `ext-ryan-claude` is ryanxxhuang's bot with its own
three-layer Claude advisor:
- **strategy:** posture, unit mix, build targets, raids, the plan. Every 60 s here.
- **tactical:** production and the army's action and target. Every 20 s.
- **fast:** micro. Off.

The prompts, payloads and answer handling are theirs, unchanged; Claude takes the model's place.

```bash
lab advise start --bot ext-ryan-claude --vs ai-normal [--map trapped] [--seat 0] [--seed N]
lab adv wait                 # the next question: layer, game time, the payload, the fields to answer
lab adv brief strategy       # that layer's system prompt (their playbook and role): read once per layer
lab adv answer '{...}'       # checked against the question's JSON schema; refused with the reasons
lab adv skip                 # no answer: the bot keeps its last instructions
lab adv look | log [N]       # the board now | the bot's log lines (it logs each answer it applied)
lab adv detach               # no more questions: the bot finishes the game on its rules
lab advise stop
```

- **No game time passes while a question waits** (the seat's think is `paused`). The answer is
  applied in the same decision, so this is an advisor without latency: an upper bound on what
  advice can add.
- **The game is a normal lab game**, saved with a note pointing to the transcript
  (`sim/lab/advise/<game>.jsonl`: every question's payload and the answer).
- **The pacing is the bot's setting** (`env` in its entry: `STRATEGY_EVERY`, `TACTICAL_EVERY`,
  `FAST_MODEL=off`). About 40 questions make a 10-minute game.
- **One session at a time**, running in the background (`sim/lab/advise/session-<run>.log`).
- **Generic desk:** `lab/advise.py` `Desk.ask(system, payload, schema)` serves any bot that can
  put its question as a JSON schema.
- **Scripted advisors:** `advisor: "package.module:Class"` in an external bot's kwargs puts code at
  the desk instead of Claude, with no pausing. `exploit-ryan-plan` is ext-ryan advised by
  `bots/ryan_plan.py` `PlanDesk`, the plan Claude played against v10 (runs 75-77):
  - economy first;
  - one massed strike on the ore-field guard, clear of known defenses;
  - hold a lead, all-in when behind late.

  It wins 22-33% against v10, where every earlier bot won none.

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

**One moment replays one way.** A branch keeps the game's engine seed (that is what makes it the
same game), so the same bots from the same moment always play out identically. `--repeats N`
therefore branches at N moments 2 s apart (`lab coach` uses `--spread`), each a real trajectory of
its own. A game is also chaotic after a big fight: one recorded game is a single draw from its
moment, and the control branch can end far from the original. `lab coach` prints this check
("fidelity") and saves no label when they disagree.

## Moments and probes: an idea against the situations it is for

The first stage of the experiment funnel (LAB_PLAN.md, Part 0). A **moment set** is a named list of
moments (a seat of a recorded game at a time) that share a situation; a **probe** branches all of
them with each variant and compares it to the control moment by moment.

```bash
lab moments find NAME --kind wiped|building-lost|swing|at [--runs 116] [--bot B] [--vs ai-rush] [--map M]
                 [--lead S] [--at 13:00] [--drop 5000 --window 60] [--after 10:00] [--before T] [--limit N] [--dry-run]
lab moments add NAME --game G --at 13:00 [--seat 0] [--why "..."]
lab moments list | show NAME | drop NAME [--ids 3,4]
lab probe NAME --variants A,B [--stop-after 300] [--stop-quiet 45] [--spread 1] [--limit N] [--workers W]
lab probe NAME --variants A,B,C,D --halving [--start 3]   # successive halving (below)
lab probe NAME --report [--stop-after 300] [--stop-quiet 45] [--runs 119,120]
```

- **Finders** (one moment per game and seat at most; only our bots' seats, games with a trace):
  - `wiped`: the seat lost its base, `--lead` (120 s) before the end;
  - `building-lost`: its first lost building (after `--after`), 30 s before;
  - `swing`: its score fell by `--drop` within `--window` s; 15 s before the fall began;
  - `at`: every game at `--at`, for situations that aren't events (standoffs).
- **The control** is the bot that played the moment, with the same fresh memory as the variants
  (see Branching), so the comparison is fair. Moments of different bots share one control row.
- **A placebo on selected moments** (the jev family's finding, run 185, 2026-10-16). Sets found by
  an outcome (`wiped`, `building-lost`, `swing`) are moments where the control went badly, and the
  control branch replays exactly that bad outcome. So *any* change, even one that does nothing,
  sends the game down another path and regresses toward the mean: a no-op variant gained +3,383 and
  was better in 10 of 15 moments on `@normal-fight`. Always add a placebo variant to such probes
  and judge a variant against the placebo, not the control:
  `ff-council-champ-v10-placebo` is v10 with each decision landing one tick later (think 45 ms).
  Or use unselected moments (`--kind at`), and confirm with whole games either way. The same goes
  for hand-played moments (stage 1a): they too are compared with a selected bad outcome.
- **The report** per variant: the mean score change since the moment, the paired difference to the
  control at the same moments with a 95% t-interval (a verdict, BETTER/WORSE/unclear, from 3 pairs
  on), in how many moments it did better, how often the seat was wiped, W-D-L at the stop, and how
  long the branches lasted. Branches of a set from earlier probes count too, for the same stop rule
  and only at the bots' current versions (a changed bot is another variant); `--runs` narrows it.
- **Reuse:** a branch already played the same way (same moment and offset, bots at their current
  versions, seed, stop rule, engine build) is not played again: one moment with the same bots
  replays one way, so the recorded result is the result. A control is played once per moment,
  ever. `--no-reuse` plays everything.
- **`--stop-quiet S`:** also ends a branch once nothing was killed or lost for S seconds: the
  situation is decided (a push beaten off, a standoff). A wipe-out ends it anyway.
- **`--halving`:** successive halving for many ideas. Round 1 plays every variant on `--start`
  moments (3; the set in a fixed shuffled order), then the better half by paired difference (minus
  any already WORSE) plays on twice as many, and so on until the set is used up or one is left.
  With reuse, each round plays only its new moments. 10 variants on 13 moments: ~45 branches
  instead of 130.
- **Cost:** a branch replays the game up to the moment first, and that replay is mostly the engine
  simulating the world: 17 minutes take ~26 s on an idle machine (~21 s of it engine ticks; ~44 s
  with 4 other games running), 5 minutes ~10 s. Steps without orders are merged in the replay (~13%
  faster; checked against the trace at every step with orders). OpenRA's own AI thinks ~6% of that
  time (`RL_BOT_TIMING=1`), so replaying with the AI asleep, as OpenRA loads a save game, would gain
  little. A late branch costs about a game played to its moment: probes save by playing only the
  situations an idea is for, reuse, halving and early stops; engine snapshots would change the rest
  (LAB_PLAN L14).

## Playing a moment by hand (the funnel's stage 1a)

```bash
lab human start --moment 17 --stop-after 180 --stop-quiet 45   # moment 17 of its set (lab moments show)
lab h units | enemies | move X Y all | target ID UNITS | go 4 | note TEXT
lab probe @rush-wave --moments 17 --control-only --stop-after 180 --stop-quiet 45
lab probe @rush-wave --report --stop-after 180 --stop-quiet 45   # the human-<name> row, paired with the control
```

The game is the probe's branch at that moment, with you as the variant. Human and advisor
sessions are one per lab session (`sim/lab/human/<family or main>/`). They take no game slot,
since a paused game uses no CPU.

## The board: every family against the champion

```bash
lab board                      # the standings
lab board play                 # play this checkout's entries (a family: its own; main: reference and fixed)
lab board run                  # the coordinator: main's entries, then every family's from its worktree; saves a snapshot
lab board history [BOT]        # the snapshots over time
lab bots tag BOT --tags +board # enter a frozen bot (a family: at most 2)
```

- **One schedule** (`lab/board.json`, the coordinator's): every entry plays each baseline (`ai-normal`,
  `ai-rush`, `ai-turtle`, the two exploiters, `ext-ryan`) 8 times, maps rotating and seats
  swapped, on the board's seeds, at 20 minutes: 48 games an entry. Every entry plays the same games,
  so any two are compared game by game.
- **Entries:** the reference (`ff-council-champ-v10`) and the fixed bots (`ff-greedy-turtle-eco`,
  `ff-default`) from the main checkout. Each family's bots tagged `board` (module bots frozen) are played
  from the family's own worktree, since its code exists only on its branch until merged.
- **Played once:** lab games are deterministic, so a game already played the same way (the same
  versions in the same seats, map, seed, length, engine build) is reused. Checked on 2026-10-16:
  two games played twice gave identical scores, lengths, timelines and events (runs 126-127).
  Rerunning the board plays only new entries and new versions. A new engine build plays everything again.
- **Standings:** per entry, its latest version's games: points, the paired difference to the
  reference (points a game, 95% interval: a verdict once it clears 0), mean net, points by baseline.
  `lab board run` saves a snapshot and posts the top 3 as a `lab note`.

## Several sessions at once

Bot families are built by separate agent sessions (Claude Code, Codex), each in a git worktree
of its own (`scripts/session.py new <family>`). The rules are in [AGENTS.md](../AGENTS.md). What
the lab does for it:

- **One lab for all:** a worktree finds the main checkout's `sim/lab` by itself (`lab.MAIN_ROOT`):
  the same database, traces, moment sets and notes. Runs record their session (`runs.params.family`).
- **Game slots:** every lab game takes one of the machine's slots (`sim/lab/slots/`, `FF_LAB_SLOTS`,
  default the CPU quota). All sessions' games together never outnumber the CPUs, and a stopped
  run's workers die with it. `lab status` shows who holds the slots.
- **Family registries:** `lab/bots.d/<family>.json`, names `<family>-...` (a family session can
  register nothing else); `lab bots add/freeze` write each bot back to its own file.
- **Limits:** a family session `lab forget`s only its own runs.

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
| `sim/lab/advise/` | the advise session's address and logs, and each advised game's transcript |
| `sim/lab/external/` | contestants' bots' log lines and their advisors' last prompts |
| `sim/external/` | contestants' repositories at their pinned commits (`lab external fetch`) |
| `sim/lab/dashboard.html` | the dashboard (`lab dashboard`) |
| `sim/lab/jev/` | the Jev bots' call logs (`lab jev`) |
| `lab/bots.json` | the bot registry (committed) |
| `bots/` | bots of our own (`jev_bot.py`, `example.py`); `bots/frozen/`: frozen copies; `bots/external.py`: contestants' bots |

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
