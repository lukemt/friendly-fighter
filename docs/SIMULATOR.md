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
python scripts/ffsim.py run                                  # our agent vs itself, trapped, 10 min (~40 s)
python scripts/ffsim.py run --seat1 ai:normal --games 6 --parallel 6   # vs OpenRA's Normal AI (~85 s for all 6)
python scripts/ffsim.py run --map random --games 6 --parallel 3 --swap
python scripts/ffsim.py run --seat0 agent:A --seat1 agent:B --env B:FF_SOME_KNOB=1 --games 10 --parallel 5 --swap
python scripts/ffsim.py run --probe                          # the probe on seat 0, then compare (below)
python scripts/ffsim.py run --time real                      # on the wall clock, like the platform (10 min a game)
python scripts/ffsim.py serve --port 8800                    # just the platform; POST /sim/matches to create games
```

Games run in **virtual time** by default (next section): a 10-minute game takes about
40 s, and parallel games share one engine process.

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

- Virtual time: a 10-minute game takes 35-45 s of wall time (15x real time). Six in
  parallel finished in 84 s (43x one-by-one real time); the machine was half busy then, so
  more parallel games help further. Per game: ~12-15 s of engine CPU, ~5 s of server,
  ~5 s per seat worker.
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

**Engine host.** In virtual time every game is a session of one shared engine process
(the fork's multi-session mode, `Launch.MultiSession`, extended for two agents per game):
the map data is loaded once, a new game takes about 1 s, and no local server sits
between the bots and the world. `--engine single` starts one engine per game instead
(the real-time setup, but stepped).

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
| `wait_for_update` | answers at once if the state changed, else looks again every 0.1 s: durations cluster at RTT + 0 / 0.1 / 0.2 / 0.3 s (51 / 21 / 4 / 1 %) | same |
| Batch stamp | the newest view's tick (`arrival - state_version` is 0, 6 or 12) | same |
| A view in the served state | ~45 ms after its tick (fits exponential) | same |
| Lag (arrival - state acted on) | 0 / 6 / 12 ticks: 7-12 / 80-85 / 3-6 % | 5 / 93 / 1-2 % |
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
```

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
