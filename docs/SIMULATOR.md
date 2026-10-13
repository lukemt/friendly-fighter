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
python scripts/ffsim.py run                                  # our agent vs itself, trapped, 10 min
python scripts/ffsim.py run --seat1 ai:normal --games 4      # vs OpenRA's Normal AI (also rush, turtle, naval)
python scripts/ffsim.py run --map random --games 6 --parallel 3 --swap
python scripts/ffsim.py run --seat0 agent:A --seat1 agent:B --env B:FF_SOME_KNOB=1 --games 10 --parallel 3 --swap
python scripts/ffsim.py run --probe                          # the probe on seat 0, then compare (below)
python scripts/ffsim.py serve --port 8800                    # just the platform; POST /sim/matches to create games
```

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

**Cost.** One headless game uses about 0.3 core and 240 MB, plus our two seat workers.
The 6-core container runs about 6 games at once in real time (a 10-minute game takes 10
minutes).

## What is replicated, and how faithfully

| Part | Source of truth | Status |
|---|---|---|
| Engine, rules, maps, fog, combat, economy | the platform's fork `9b271c1` | identical code |
| Lobby | `startingcash 5000`, MCV only, `crates` on, `bounty` off, `explored` off, `shortgame` on | as `match_rules` shows |
| Views | every 6 ticks, `state_version == tick`, 25 ticks/s | measured equal |
| Order timing | stamped with the arrival tick; the engine executes it 12 ticks later (4 of them are OpenRA's own order latency, compensated) | equal: deploy shows its `fact` 24 ticks after arrival on both |
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
.venv/bin/python -m pytest -q tests/test_ffsim.py                        # fast, fake engine
FF_SIM_ENGINE=1 .venv/bin/python -m pytest -q tests/test_ffsim_engine.py  # real engine, ~2 min
python -m ffsim.conformance recordings/<real session> recordings/<sim session>
python -m ffsim.probe_compare recordings/<real>/probe-seat0.jsonl recordings/<sim>/probe-seat0.jsonl
```

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
