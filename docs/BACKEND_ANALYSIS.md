# What's behind the Red Alert game server

Written 2026-10-13, after the analysis of recording `95c1b5f3-…` (Claude vs. the
bot, 12:50, both seats recorded). Scripts: `scratchpad` only; the method is
described here so it can be repeated.

## 1. The engine is public

`get_game_config("red_alert").rules.engine_commit` is
`9b271c1a5562a07deeb1c3f81d9a556cd563a676`.

- **Where the commit lives:** it's in **`github.com/yxc20089/OpenRA`**, a fork
  of OpenRA dated 2026-03-25 ("Add unit_arrived + production_complete to
  CheckInterrupts"). It is 580 commits ahead of `release-20250330`, the
  version our simulator builds.
- **Who made it:** the same author publishes **`yxc20089/OpenRA-RL`** (157★,
  PyPI `openra-rl`), an RL/LLM framework for Red Alert:
  - Python env plus a FastAPI server;
  - a gRPC bridge (`proto/rl_bridge.proto`);
  - a Docker image `ghcr.io/yxc20089/openra-rl`;
  - a headless engine (`OpenRA.Platforms.Null`, no Xvfb).
- **What the fork adds:**
  - `OpenRA.Mods.Common/Traits/Player/ExternalBotBridge.cs`: an external agent
    controls one player through it. This is our planned "Phase 2" component,
    already written.
  - `RLSessionManager.cs`: many games in one process.
  - `mods/ra/rules/rl-bot.yaml`.

**The protocol matches the platform almost field for field:**

| Platform | Protocol |
|---|---|
| `economy` | `RlEconomy` |
| `military` (`kills_cost`, `deaths_cost`, `assets_value`, `army_value`, …) | `RlMilitary` |
| units (`act`, `idle`, `stance`) | `RlUnitInfo` |
| buildings (`powered`, `repairing`, `producing`, `rally`) | `RlBuildingInfo` |
| production (`remaining_s`, `paused`) | `RlProductionInfo` |
| `visible_enemy_buildings` | `visible_enemy_buildings` |
| `explored_percent` | `explored_percent` |
| the 19 order kinds | `ActionType` |
| `resign` | `SURRENDER` |

### What's still hidden: the platform "runner" / GameAPI layer

None of these words appear anywhere in the public code:
`state_version`, `last_orders`, `recent_order_problems`, `attack_now`,
`target_not_visible`, `not_available`. So the private layer is:

- **Two agents in one game.** The public `RLSessionManager` binds a session to
  the *first* `ExternalBotBridge` it finds, i.e. one external agent. The platform
  must patch this or run something else.
- **Real-time pacing:**
  - one batch in flight;
  - 0.2 s spacing;
  - the 3000-tick staleness rule;
  - `NOT_YOUR_TURN`.
- **Order checks and the verdict book:**
  - immediate `pending` / `rejected`;
  - final `ok` / `partial` / `rejected` / `lost`;
  - `failed[]` rows, `carried`, `rev`.
- **State conversion:**
  - WPos → cells, hp as a percent;
  - wreck/husk tagging;
  - `base_center`;
  - `events`, with throttling and `seq`;
  - `legal_actions`, `attack_now`, `enemy_base`;
  - `hint` / `next_actions`.
- **Match lifecycle:**
  - phases, check-in, queue;
  - time limit and tiebreak, idle rule;
  - `result` / `get_result`.
- **The MCP server itself:** tool names and argument schemas.

All of it is documented in `skill/redalert.md` and visible in our recordings,
so it can be rewritten as a thin Python layer.

## 2. What the recordings show (ground truth for a replica)

| Fact | Value |
|---|---|
| Engine speed | 25.05 ticks per wall-clock second |
| Observation cadence | **every 6 ticks (0.24 s)**, the same ticks for both seats (one engine, one clock). Our loop saw 98% of them (step 6; 3% step 12) |
| `state_version` | always equals `observation.tick` |
| Order latency | Orders run **exactly 12 ticks after the batch reaches the server** (`applied_at_tick` = arrival tick + 12, all 113 probe-match batches). The server stamps the batch with the arrival tick: `play_action`'s answer and `last_orders` carry that tick as `state_version`, **not the one sent**. A 2 s-old view is applied at arrival + 12 too. The final verdict shows in the first view ≥ 12 ticks after arrival |
| Kills | `seat0.kills_cost == seat1.deaths_cost` (and vice versa) at all 2,958 shared ticks |
| `assets_value` | the exact sum of the costs of own units and buildings, with `fact` counted at $2,000 (the MCV's cost; `fact` isn't in `rules`) |
| Fog | enemies are listed exactly when within sight; positions agree with the other seat's own view. Sight ranges seen: ~6–9 cells |
| Ids | one actor-id space for both players |
| Observation schema | every key in `skill/redalert.md` is present. Enemy units carry `wreck`; enemy buildings have only `id/type/x/y/hp` |
| Final verdicts seen | `ok`; `partial` with `not_owned` (runner) and `unknown_actor` (engine); `rejected` with `target_not_visible`; `INVALID_ACTION` for a batch with nothing valid; `GAME_ALREADY_COMPLETE` |
| Batches sent | 218 / 300 per seat in ~13 min. The bot sends only when it has something to do, otherwise it reads the next view |

## 3. What's missing from the recordings

1. **Before the game:** `queued` / `starting` / `waiting_for_agents`,
   check-in, `wait_for_update`. The recorder starts at the first decision.
2. **The end:** the terminal state, `result`, `get_result`. The runtime
   doesn't pass them to the agent.
3. **The MCP tool list:** names, schemas, descriptions (`tools/list`).
4. **Error paths never hit:**
   - `STALE_STATE`, `NOT_YOUR_TURN` (overlap, too early);
   - the 20-order and 200-command caps;
   - unknown fields, out-of-bounds cells;
   - `lost` / `not_applied`.
5. **Order kinds never sent:**
   - `stop`, `guard`, `set_stance`;
   - `harvest` with `to`;
   - `enter_transport`, `unload`, `sell`, `set_primary`, `power_toggle`;
   - `queued: true`; `place` with `at` for buildings.
6. **Engine numbers worth measuring:**
   - unit speed in cells per second vs. `rules.speed`;
   - build time vs. power;
   - harvester income;
   - sight range per type;
   - the auto-placement algorithm.
7. **Events never seen:** `building_sold`, the `under_attack` 10 s throttle,
   `seq` gaps.
8. **Whether the +12 ticks is fixed.** It could be measured from the
   `state_version` sent or from when the batch arrives: we always sent within
   one observation window.
9. **Spawns and factions:** which seat gets which spawn, and how factions are
   drawn.

## 4. How to fill the gaps

1. **Log at the transport level.** Every MCP request and answer goes to
   `recordings/<session>/seat<N>.wire.jsonl.gz`, with send and receive times.
   That covers items 1–3 for every future match, at no cost.
2. **A probe match.** Self-play in Testing:
   - seat 0 runs `scripts/ff_probe.py`, a fixed list of experiments;
   - seat 1 runs the normal bot or idles.

   The experiments, each with expected and observed results logged:
   - **Latency:** send at 0, 0.1, 0.3, 0.5, 1, 3 s after reading a state.
     Does `applied_at_tick` follow the `state_version` or the arrival time?
   - **Pacing:**
     - two batches 50 ms apart (hold time);
     - two at once (`NOT_YOUR_TURN`);
     - `state_version` − 3001 (`STALE_STATE`), and one in the future.
   - **Validation matrix:**
     - 21 orders; 201 commands;
     - unknown field; cells between `bounds` and `width × height`;
     - 51 units; duplicate ids; `count: 11`;
     - an enemy id in `move`; an own building in `move`; a wreck id;
     - `place` before ready; `place at` a far cell or a blocked cell (record
       where it landed);
     - `cancel` with nothing queued; `build` with the queue busy.
   - **Every order kind once:** stop, guard, set_stance, harvest `to`, sell
     (`building_sold`), power_toggle (`low_power` / `power_restored`),
     set_primary, `queued: true`, resign at the end (the result object).
   - **Engine numbers:**
     - drive a jeep / e1 / tank a known distance (cells per tick);
     - build the same item at full and low power;
     - one harvester's income per trip;
     - scout toward a known enemy building (sight range per type).
3. **Factions and spawns:** count them across every match from now on (wire
   logs).

## 5. What this means for the simulator

The plan shrinks:
- **Phase 2 (ExternalBot C#):** mostly done by the fork. Build
  `yxc20089/OpenRA` at `9b271c1` with .NET 8 and the null platform.
- **Two agents per game:** the one real C# change. Bind one bridge per player
  and route by `(session, player)`.
- **Phase 3:** a Python "gameapi" that implements the platform's MCP surface
  from `skill/redalert.md` on top of the gRPC bridge:
  - an observation every 6 ticks;
  - orders applied at +12;
  - the verdict book;
  - events;
  - `legal_actions`.

  Validate it against these recordings: the schema, cadence, latency,
  `assets_value` and fog checks above become its tests.

## 6. Probe match results (session `70aee597-…`, 2026-10-13)

Seat 0 ran `agent/probe.py`; seat 1 ran the bot. Raw data:
`recordings/70aee597-…/probe-seat0.jsonl` plus `wire-*.jsonl.gz`.

**What went wrong:** the probe waited for verdicts under the version it had
sent, but the server files them under the arrival tick (fixed since). Every
wait ran into its 6 s timeout, so the opening tests took ~4 minutes. Seat 0
deployed only at 4:10 and lost at ~6:30, so most phase-C tests didn't run.

### Tools

`tools/list` has 11 tools:

- `get_agent_status`, `list_games`, `get_game_config`;
- `get_game_state`, `wait_for_update`, `get_legal_actions`;
- `play_action`, `send_message`, `get_messages`;
- `get_result`, `resign`.

### Pacing

- **Batches too close:** a second batch 0 s after the first took 0.31 s
  instead of 0.20 s. The server held it for the gap; no error.
- **Two batches at once:** one is accepted, the other gets `NOT_YOUR_TURN`
  ("Your previous batch is still being applied…").
- **Staleness:** a `state_version` in the future, or more than 3000 ticks
  old, gets `STALE_STATE`.
  - The detail names the current tick and the oldest tick accepted.
  - It's measured at arrival: a view read 2990 ticks back was already stale
    on arrival.
- **Negative `state_version`:** `INVALID_ACTION` (schema).

### Batch-level errors

All of these get `INVALID_ACTION`, with readable details:

- empty, 21 orders, or `orders` not a list: "non-empty list … (at most
  20)";
- wrong `type`;
- no valid order: lists `[i] cmd: reason` for each order.

Other answers:

- **Engine-command cap:** 250 commands of unknown unit ids never reached it.
  Ownership is checked first, so the batch was simply all `not_owned`.
- **`action_id` (an int) or a missing `state_version`:** answered as an MCP
  text error ("Error executing tool … validation error"), not as JSON.
- **Unknown tool:** "Unknown tool: teleport".
- **Wrong session:** `SESSION_NOT_FOUND`.
- **700-character reasoning:** accepted.

### Per-order checks

These are answered at once, as `rejected` with a reason and detail:

| Order | Reason | Detail |
|---|---|---|
| Unknown field | `invalid` | "unknown field(s) for stop: ['foo']" |
| Unknown `cmd` | `invalid` | lists the 19 valid ones |
| `to` outside width × height | `invalid` | |
| Non-integer cell | `invalid` | |
| Duplicate ids | `invalid` | "units must be distinct" |
| 51 units | `invalid` | |
| String id, empty units | `invalid` | |
| `count: 11` | `invalid` | |
| Unknown or unavailable item | `not_available` | "… is not in available_production" |
| `place` with nothing ready | `not_ready` | "… is not in any production queue" |
| `cancel` with nothing queued | `not_available` | |
| `attack` on an unseen id | `target_not_visible` | |
| Both `target` and `to` | `invalid` | |
| Bad stance | `invalid` | lists the 4 stances |
| repair/sell/rally on a unit | `not_owned` | "… is not one of your buildings" |
| Unknown unit id | `not_owned` | listed in `failed` |
| `move` on a building | `is_building` | |

These pass the runner's checks (`pending`) and are left to the engine:

- `to` inside the map but outside `bounds`;
- `attack` of your own unit (`target`: self);
- `guard` self;
- `unload` / `enter_transport` / `harvest` on an MCV;
- `queued: true`;
- `stop` / `set_stance` on a building.

The final verdicts of these are in the probe log.

### Not yet measured in this match

Phase C didn't run here. It ran in the next match; see section 7.

## 7. Second probe match (session `8d78b9e8-…`, 2026-10-13)

Seat 0 was England on the map `trapped`, played by the probe plus console. The probe now waits on the server-stamped arrival tick, so phase A took about 16 s of game time (it finished by tick ~400). Phase C started at tick ~3850. All 19 steps passed.

### Timing

- An order takes effect **18 ticks after the tick we sent from**, at every delay we tried (0 s, 0.5 s, 2 s). That is about 6 ticks for the batch to arrive and 12 ticks of fixed delay.
- `e3` (rocket infantry) moves about **1.07 cells/s** (12.9 cells in 300 ticks).

### Orders

| Order | Result |
|---|---|
| `set_stance`, `guard`, `move` with `queued: true` | all `ok`; the queued move runs after the first |
| `set_primary` on a barracks / war factory | `ok` |
| `set_primary` on the construction yard | engine `invalid` |
| `repair` on an undamaged building, on or off | `ok` (no `already_*` detail) |
| `rally` to a far cell | `ok`; the building's `rally` field shows the cell |
| `train` with count 5, then `cancel` 1 | both `ok` |
| `build` the same building twice, then `cancel` | all `ok`, so the queue accepts repeats |
| `sell` | `ok`; a `building_sold` event follows about 66 ticks after the order |
| `harvest` with `to` | `ok`; the harvester walks toward the cell |

**`place` with `at` is only a hint.** The verdict is always `ok`, but the building lands on a valid cell picked by the engine, not on the requested one:
- asked for (62,75), it went to (64,69);
- asked for the construction yard's own cell (66,72), it went to (62,72);
- asked for (49,49), far away, it went to (66,76).

Don't count on exact placement. To know where a building really went, read `buildings` afterwards.

**`power_toggle` works only on buildings that can be powered down.** Its fields are `building` and `on`; sending `units` is refused as an unknown field. Asking for off on `fact`, `powr`, `proc`, `tent` or `weap` gives engine `invalid`, each time.

The runner answers `on: true` on a building that's already on itself: `ok` with detail `already_on`, and nothing is sent. In the fork's `mods/ra/rules`, `ToggleConditionOnOrder: PowerDown` comes from `^DisableOnLowPowerOrPowerDown`, which only these inherit:

- MSLO (missile silo)
- GAP (gap generator)
- IRON (iron curtain)
- PDOX (chronosphere)
- TSLA (tesla coil)
- AGUN (anti-aircraft gun)
- DOME (radar dome)
- SAM (SAM site)

So a bot can only free power by switching off a dome, a tesla coil or an anti-air defence.

### Tools

- `list_games` lists 110 game types. `red_alert` is the only one on the `openra` adapter, with `action_model: structured`; all the others are on `openspiel`.
- `send_message` and `get_messages` answer `RUNTIME_UNAVAILABLE`: "Runtime adapter 'openra' does not support …". Red Alert has no chat.
- Two `play_action` arguments we hadn't used:
  - `action_id` is only a label and is ignored when `action` is given, so it is no idempotency key;
  - `reasoning_summary` is stored for spectators.
- `wait_for_update` also takes `since_message_seq`, `since_is_current_actor`, `since_phase` and `timeout_seconds` (max 25). The docs say at most 2 waits per game can run at once (`TOO_MANY_WAITS`). This wasn't observed: in Red Alert every wait returns within one view (0.24 s), so 4 parallel waits all succeeded.

## 8. The platform's timing, from the wire logs (2026-10-14)

Measured on the wire logs of both probe matches and the bot's games
(`python -m ffsim.timing`, plus the histograms below). This is what the local
platform's virtual time reproduces (docs/SIMULATOR.md, "The platform's timing").

- **Clock.** 25.0 ticks per wall second over whole games (every 60 s window between
  24.8 and 25.8). Views come every 6 ticks, always on ticks 5, 11, 17... (state_version
  % 6 == 5 in every recording).
- **Round trip.** `get_game_state`, `play_action` and `get_legal_actions` take a median
  0.195 s (p10 0.19, p90 0.21, max 0.34); the agent's own gap between calls is 1-4 ms.
- **`wait_for_update` polls every 0.1 s.** It answers at once when the state already
  changed; otherwise it looks again every 0.1 s after it arrived. The call durations
  cluster at 0.19 s (51 %), 0.29 s (21 %), 0.39 s (4 %) and 0.49 s (1 %), with the rest
  in between (round-trip jitter).
- **Batches are stamped with the newest view's tick**, not an interpolated engine tick:
  `arrival - state_version` is always 0, 6 or 12 (24 or 30 when the agent was slow).
- **The served state lags that tick.** A batch sent right after a state was read lands
  one view later in 80-85 % of cases (0: 7-12 %, 12: 3-6 %). Polling alone predicts only
  ~60 %. Adding a delay between a view's tick and its appearance in the served state,
  about 45 ms (exponential), reproduces both the lags and the wait histogram. A likely
  cause is the platform building the state (legal actions, events) after it learns the
  tick.
- **Execution.** Orders run 12 ticks after the stamp; a deploy shows its `fact` 24 ticks
  after the stamp (24 in the local replica too).
- **`starting_units: "none"`** in `match_rules` is RA's lobby value for "MCV only" (the
  `StartingUnits@mcvonly` class is `none`). The local engine had been given `mcvonly`,
  which the lobby silently rejected; a session world given it spawns nothing.
