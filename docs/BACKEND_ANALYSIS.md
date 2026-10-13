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

### Not yet measured

Phase C didn't run, so these remain:

- stance, guard and queued moves;
- unit speed;
- power toggle;
- repair / primary / rally;
- queue and cancel;
- where `place at` puts a building;
- sell;
- harvest `to`;
- the end-of-game answers after `resign`.
