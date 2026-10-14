# The local Red Alert platform (ffsim)

A copy of the hosted Red Alert server that runs on this machine. **Our unchanged
runtime** (`python -m agent --match`) plays on it exactly as it does online. It runs
on the **same engine** as the platform: the platform's own OpenRA fork, at the commit
that `get_game_config` reports.

Use it to:

- practise, and try bot changes, without waiting for Testing matches;
- A/B test two variants of our agent against each other;
- play OpenRA's built-in AIs;
- run the probe and anything else against a server we control.

## Setup (once)

```bash
sim/openra-fork/setup.sh          # ~/openra-sim: .NET 8, the fork + ff-sim.patch, build, RA content
.venv/bin/pip install -e '.[sim]'  # grpcio
```

## Running games

```bash
python scripts/ffsim.py run                                  # our agent vs itself, trapped, 10 min (~35 s)
python scripts/ffsim.py run --seat1 ai:normal --games 12   # vs OpenRA's Normal AI (~2.5 min for all 12)
python scripts/ffsim.py run --map random --games 6 --parallel 3 --swap
python scripts/ffsim.py run --seat0 agent:A --seat1 agent:B --env B:FF_SOME_KNOB=1 --games 10 --parallel 5 --swap
python scripts/ffsim.py run --probe                          # the probe on seat 0, then compare (below)
python scripts/ffsim.py run --time real                      # on the wall clock, like the platform (10 min a game)
python scripts/ffsim.py serve --port 8800                    # just the platform; POST /sim/matches to create games
```

Games run in **virtual time** by default (next section): a 10-minute game takes about
35 s alone, and a batch runs at about 4 games a minute.

**Seats.**

- `agent:LABEL` is played by one runtime per label. Both seats under one label is
  self-play, as in Testing.
- `--agent LABEL:MODULE` and `--env LABEL:K=V` configure a label's runtime.
- `ai:<type>` is an OpenRA bot.

**Results.** These go to `sim/results/<time>/`:

- one JSON per game, with the result, both seats' tiebreak tuples and military, and the
  reasoning summaries;
- `summary.json`, with a win/draw/loss table per label;
- `runtime-<label>.log` and `engine/<session>/engine.log`.

The runtimes write their usual recordings to `recordings/sim-*`.

**Keys and isolation.**

- Local runs use made-up keys (`eak_live_` + a hash of the label), sent only to the
  local server.
- No OpenAI calls unless `FF_SIM_OPENAI=1`.
- Each runtime gets its own remote-control folder (`FF_REMOTE_DIR`) and console ports,
  so it never interferes with the live tournament runtime.

**Cost.**

- Virtual time: a 10-minute game takes ~26 s of wall time alone (23x real time; 28 s in
  self-play). In a batch, 12 games vs Normal AI took ~150 s with `--parallel 3` (about 5
  games a minute). A game costs ~36 CPU-s: ~11-15 s engine (the first game of a lane ~20 s,
  loading included), ~8 s server, ~7-9 s per seat worker (~3 s of it starting up).
- Real time costs more CPU, not less: the engine alone uses ~0.3 core for the whole 10
  minutes (~160 CPU-s a game).
- **This container has 4 CPUs of quota** (`/sys/fs/cgroup/cpu.max`), though it shows 6:
  3 or 4 games at once is best; at 6 the quota throttles everything (each engine tick 2.8x
  slower) and throughput does not grow. `--parallel` defaults to the quota minus one in
  virtual time (all games at once in real time).
- Real time: one headless engine is about 0.3 core and 240 MB; a 10-minute game takes 10
  minutes.

## Virtual time

On the platform the game runs on the wall clock: 25 ticks a second, whatever the agents
do. Locally it runs on a **virtual clock** (`ffsim/vclock.py`):

- the engine runs only the ticks the clock asks for (`RL_LOCKSTEP=1`, the `Step` RPC), as
  fast as the CPU allows, one view (6 ticks) at a time;
- every timing rule of the platform is kept in game time (virtual seconds = ticks / 25):
  the round trip of each MCP call, the 0.2 s hold, the `wait_for_update` polls, the
  hand-over, the publication delay of views, the idle rule and the time limit;
- **the agent's own time** between an answer and its next call is charged as it really
  was (`--think measured`, the default), or as a fixed number of ms (`--think 3`: then a
  game is reproducible for a given `--seed`);
- the clock only moves forward when nothing could still happen before: never past a call
  being handled, never past the earliest moment a seat's next call could arrive. A seat
  that stays silent is timed by the wall clock, as on the platform, where the game does
  not wait.

So whatever an agent does in game time comes out the same as on the platform; only the
waiting is gone.

**Prefetch.** While the agents think, the engine already runs the five ticks before the
next view. That is safe: every order still to come is stamped with the last view or later,
so it is due at the next view at the earliest.

**Reproducible games.** With a fixed think time (`--think 3`) and a `--seed`, a game is
the same every time: the round trips are drawn from one stream per seat, the views'
publication delays from the match's seed, the engine's world seed and OpenRA's bot
randomness (its `LocalRandom`) from the game seed, and the runtimes run with
`PYTHONHASHSEED=0`. Use it for paired A/B tests: the same seeds for both variants.

**Engine lanes.** In virtual time each game is a session of an engine process in the fork's
multi-session mode (`Launch.MultiSession`, extended for two agents per game): no local
server sits between the bots and the world, and a lane (one per `--parallel` slot) plays
its games one after the other without reloading the game data (~5 s), restarted every 20
games. One game at a time per process: concurrent sessions in one process slowed each
other's economies when OpenRA's AI played (half of six parallel sessions harvested their
first 1000 ore ~1500 ticks late), which showed as 3 kills a game instead of 29 vs the AI.
`--engine single` starts a `Launch.Map` engine per game instead (the real-time setup,
stepped).

**Engine calls.** The server talks to its engine lane over a plain TCP protocol
(length-prefixed protobuf, `RL_RAW_PORT`): a call costs ~0.2 ms instead of ~1.3 ms over
gRPC (`FF_ENGINE_GRPC=1` switches back).

**Lean defaults for batches.** The simulated runtimes run without the wire log
(`FF_WIRE=0`; set `FF_SIM_WIRE=1` to keep it: the server sees every call anyway), and
prefetch is on only when one game runs at a time.

**Caveat: background threads.** Only the game loop's own calls are timed. A thread that
works on the wall clock beside it (an LLM commander every 15 s, say) gets about 15 times
less game time per call than on the platform. Test such agents with `--time real`.

**Caveat: wall-clock deadlines in agent code.** Network round trips and server waits cost
no wall time in virtual time. So a loop like "poll for 12 seconds by `time.time()`" covers
about twice as much game time as on the platform. Use game time instead (the state's
`state_version` / `observation.tick`, or `time.game_seconds`). Our bot's timers and the
probe's tracking windows do. `time.sleep` while thinking is fine: it is charged as think
time.

## The platform's timing (measured)

From the wire logs of our games on the platform (`python -m ffsim.timing`, and the
analysis in docs/BACKEND_ANALYSIS.md section 8):

| What | Platform | Replica |
|---|---|---|
| Ticks per second | 25.0 (every 60 s window 24.8-25.8) | 25 (virtual) |
| Views | every 6 ticks, on ticks 5, 11, 17... | same (`RL_OBS_PHASE=5`) |
| Round trip of a call | median 0.195 s, p90 0.21, max 0.34 | drawn from the 426 measured ones |
| `wait_for_update` | answers at once if the state changed, else looks again every 0.1 s: durations cluster at RTT + 0 / 0.1 / 0.2 / 0.3 s (51 / 21 / 4 / 1 %) | 47 / 19 / 3.5 / <1 % |
| Batch stamp | the newest view's tick (`arrival - state_version` is 0, 6 or 12) | same |
| A view in the served state | after its tick, in order: 45 ms (exponential), one view in ten 0.1 s more (fitted) | same |
| Views a wait advances | one in 96.5 %, two 2 %, three 1.2 % | mean gap 6.3-6.5 ticks (platform 6.2-6.4) |
| States the agent sees | 236-242 a minute | 231-240 |
| Lag (arrival - state acted on) | 0 / 6 / 12 ticks: 7-12 / 80-90 / 3-6 % | 3-8 / 87-93 / 3-5 % |
| Orders run | 12 ticks after the stamp; deploy shows its `fact` 24 ticks after | same (24) |

## What is replicated, and how faithfully

| Part | Source of truth | Status |
|---|---|---|
| Engine, rules, maps, fog, combat, economy | the platform's fork `9b271c1` | identical code |
| Lobby | `startingcash 5000`, MCV only, `crates` on, `bounty` off, `explored` off, `shortgame` on | as `match_rules` shows |
| Views | every 6 ticks on ticks 5, 11, 17..., `state_version == tick`, 25 ticks/s | measured equal |
| Order timing | stamped with the newest view's tick; the engine executes it 12 ticks later (OpenRA's own order latency, 4 ticks in real time and 6 stepped, is compensated) | equal: deploy shows its `fact` 24 ticks after arrival on both |
| Network and polls | round trips drawn from the platform's, `wait_for_update` polling every 0.1 s, views published ~45 ms after their tick | wait durations and lags as on the platform (table above) |
| Runner checks | all 29 probe-matrix orders give the platform's exact status/reason/detail | `tests/test_ffsim.py` |
| Engine verdicts | `invalid` / `unknown_actor` / `not_owned` from the engine's own traits | probe pass-through cases equal (`tests/test_ffsim_engine.py`) |
| Pacing | one batch in flight (`NOT_YOUR_TURN`), 0.2 s hold, `STALE_STATE` at arrival − 3000 | as probed |
| State/answer shapes | key order and types of real states | `tests/test_ffsim.py`, `python -m ffsim.conformance` |
| Events | diffs of consecutive views (the platform's own logic is private) | same kinds and fields; exact timing may differ |
| `legal_actions`, `attack_now` | per skill/redalert.md | same keys; ordering details may differ |
| End | victory/draw from the engine, resignation, time limit and idle rule with the tiebreak | as documented |
| Not replicated | queueing, `windows` mode, spectators, cancellation codes other than `no_agents_connected` / `engine_*` | not needed for practice |

**Known differences.**

- **A seat played by an OpenRA bot has no view.** At the tiebreak it scores
  `(our deaths_cost − our kills_cost, 0)`, using the kill-mirroring fact. A `time_limit`
  result against a bot therefore over-weights kills.
- **Factions** are OpenRA's random pick (any of the 5); the platform's draw rule is
  unknown.
- **Wording.** A few platform messages we never saw are guesses, marked `unverified`
  in `ffsim/orders.py`.

## Checking fidelity

```bash
.venv/bin/python -m pytest -q tests/test_ffsim.py tests/test_vclock.py    # fast, fake engine
FF_SIM_ENGINE=1 .venv/bin/python -m pytest -q tests/test_ffsim_engine.py  # real engine, ~30 s
python -m ffsim.conformance recordings/<real session> recordings/<sim session>
python -m ffsim.probe_compare recordings/<real>/probe-seat0.jsonl recordings/<sim>/probe-seat0.jsonl
python -m ffsim.timing recordings/<real session> recordings/<sim session>   # lags, view gaps, cadence in ticks
python -m ffsim.compare_runs sim/results/<run A> sim/results/<run B>         # two batches of games, means ± se
```

**Virtual vs real time, same matchup (6 games each vs Normal AI, `compare_runs`):**

| Per game (agent seat) | Real time | Virtual (lanes) | \|z\| |
|---|---|---|---|
| kills / deaths (cost) | 10,942 / 15,733 | 10,233 / 13,367 | 0.2 / 1.3 |
| units killed / lost | 28.7 / 40.8 | 19.0 / 36.2 | 1.3 / 1.1 |
| assets / army value | 21,217 / 8,150 | 22,392 / 9,492 | 0.7 / 1.0 |
| batches / min, lag 6 share | 10.0, 0.92 | 10.2, 0.91 | 0.1, 0.4 |
| AI wins | 6 / 6 | 5 / 6 | |
| wall time | 628 s | 107 s | |

The probe run in virtual time matches the platform on every step (`probe_compare`),
including `a04` (when a batch sent 0 / 0.5 / 2 s after its state is applied: 24 / 36 / 72
ticks after it, as on the platform). The result JSON of each game has `clock` (the
virtual clock: steps, wall time spent waiting for the agents, think times, the
`wait_for_update` duration histogram) and `engine` (engine time, step time).

## How it is built

```text
runtime (python -m agent --match) --HTTP--> ffsim.server: control plane + /mcp (11 tools)
                                               |
                                    ffsim.gameapi.Match: phases, pacing, verdict book, events,
                                    legal_actions, results (ffsim.orders, ffsim.observe)
                                               |  gRPC stream per seat ("rl-player" header)
                                    OpenRA fork + ff-sim.patch, headless (Game.Platform=Null)
```

`sim/openra-fork/ff-sim.patch` changes these things in the fork:

- **RLLockstep** (new) and `Game.LockstepDriver`: virtual time (`RL_LOCKSTEP=1`): the
  `Step` RPC runs a game to a tick and returns its views; for a `Launch.Map` game through
  the game loop, for a multi-session game on the calling thread.
- **RLSessionManager:** lobby options on `CreateSession`; two agent bridges per session.

- **ActionHandler:** a verdict per command (`ok`, `unknown_actor`, `not_owned`, or
  `invalid` when the actor or target lacks the trait); train counts; place detail.
- **ExternalBotBridge:**
  - holds each action until its `apply_at_tick`;
  - repeats recent verdicts in every view;
  - `RL_OBS_INTERVAL` sets the view interval;
  - unpauses only once both agents are connected;
  - `LookupByPlayer`.
- **RLBridgeService:** routes a stream by its `rl-player` header.
- **ObservationSerializer:**
  - adds `powered_down`, `disabled` and `is_husk`;
  - adds player, faction, enemy faction and winner;
  - adds `explored_percent`;
  - builds the spatial tensor only when `RL_SPATIAL=1`.
- **ToggleConditionOnOrder:** exposes `Enabled`.
- **Game.LoadMap:** reads `OPENRA_LOBBY_OPTIONS`.
