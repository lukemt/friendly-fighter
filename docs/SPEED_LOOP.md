# Local games: the speed loop

A log of making local games (ffsim, virtual time) faster without changing the game, kept as
the work goes. Each round:

1. **The same game?** Engine changes are checked by replaying recorded games
   (`FF_ENGINE_TRACE`, `scripts/ffbench.py replay`): every step's views must match the old
   build's. Whole runs are checked with seeded games (`--think 3 --seed N`): the same outcomes,
   to the last credit.
2. **Faster?** The same seeded batch, wall time and CPU per process kind (engine, the agent's
   match processes, the platform server).
3. **Still the platform?** One strategy of our bot, played in virtual time and in real time
   (`--time real`, the wall clock like the platform), compared with `python -m ffsim.compare_runs`.
   The strategy changes every round, so other parts of the game get exercised (and we learn
   which strategies work).

## Where the time went (start of the loop)

9 games vs OpenRA's Normal AI (`--think 3 --seed 21`, 3 at once): **112 s, 319 CPU-s**
(engine 189, agent processes 79, server 50).

- The engine alone, replaying the same games step by step: 7.4 CPU-s a game. In a run it used
  21: starting an engine (~6 CPU-s) and compiling its code during its first game (~10), and
  steps that come one at a time between the agents' calls cost more than back-to-back ones
  (+35 %: the VM's cores go idle in between).
- OpenRA's logging thread woke every millisecond (~6 % of a core per engine, all the time).
- In the engine's tick: unit sight updates allocated and zeroed arrays (~20 %), sprite
  animations kept screen bounds for a screen nobody draws (~8-12 %), the actor map scanned every
  bin when a unit moved (~6 %), the explored share was recounted cell by cell (~4 %), and an
  exception was thrown and caught for every view.
- The agent's match process read `remote/control.json` on every view (a fifth of its CPU: the
  workspace is a shared filesystem) and wrote recordings with gzip level 9.
- The server encoded each state three times with `json`; in single-game mode it started a
  thread per view for prefetching.
- The runner slept 5 s at the end of every run.

## Rounds

| Round | Change | Same game? | 9 games (3 at once) | Single game |
|---|---|---|---|---|
| 0 | start | - | 112 s, 319 CPU-s | 26 s |
| 1 | engine: logging thread waits, sight cells, explored share, no exception; persistent lanes; control file once a second; gzip 1; orjson | replays match; seeded outcomes equal | 93 s, 227 CPU-s | 21 s |
| 2 | engine: unrendered (no screen map, no sprite bounds, no decorations/production bars/advice), actor map cleans only touched bins, leak of every finished game fixed, games of a map share its loaded sprites (a game starts in 0.31 s, not 0.58); one prefetch thread; engine liveness not checked per step; no 5 s sleep; 4 games at once | replays match; seeded outcomes equal | 12 games: 90 s at 4 at once | 13 s |

| 3 | match processes forked from preloaded modules (forkserver, ~1 s CPU each); MCP replies parsed with orjson; engine: footprint sight without LINQ, unrendered animations without an end action don't tick (-10 % engine CPU) | 11 recorded games (wipe-outs, self-play, rush) replay identically | 12 games: 89 s, 281 CPU-s | 12.6 s |

| 4 | engine: sight offsets precomputed on rectangular maps (-3 %); unrendered, no control groups or wall sprites (-1 %) | 11 recorded games replay identically | 12 games: 88 s, 267-275 CPU-s | 12.6 s |

| 5 | stepped engines on .NET 10 (installed by setup.sh; real time stays on .NET 8): ticks -10 %, same CPU; one encoding of each MCP reply (orjson.Fragment); `ffbench.py corpus` / `check` | the 11 recorded games replay identically on .NET 10; the probe matches the platform on all 19 steps | 12 games: 86 s, 254 CPU-s | 11.6 s |

**Where it ended** (same seeded games, outcomes identical to the last credit every time): one
10-minute game in **11.6 s** (from 26 s), 12 games vs the Normal AI in **86 s** (from ~150 s),
**21 CPU-s a game** (from 36: engine 10, agent process 6, server 5).

Tried and dropped (measured, no gain): Python's cycle collector less often (server CPU the same), GC young-generation sizes (1-256 MB alone; 2-64 MB
with 4 engines competing for the cache, and in whole runs: 88 s either way), the segment
GC, no background GC, pinning engines to CPUs, prefetching with 4 games at once (92 s vs
89 s), 6 games at once (100 s vs 90 s), reusing a unit's sight array (within noise).

**The machine is the limit now.** Replaying games with no Python at all: one engine alone uses
6.7 CPU-s a game, two at once 8.5 each, three 11, four 14.8 (a VM on a 4-core laptop CPU: shared
caches and hyperthreads). Engine throughput tops out at ~3.7 s a game with 3-4 at once; whole
games run at ~7.4 s a game (4 at once), each game waiting on its own engine, server and agent in
turn, with ~3.2 of the 4 CPUs busy.

## Strategies played (virtual vs real time)

| Round | Our bot | Opponent, map | Real time | Virtual | Largest |z| |
|---|---|---|---|---|---|
| 1 | rush: waves of 4, infantry, nearest target | Normal AI, trapped | 0/6, net -8758 | 0/6, net -11475 | 1.8 (6-tick lag share) |
| 2 | eco: refineries first, waves of 14, vehicles, enemy base | Rush AI, sidestep | 1/6 (5 bases wiped out in 5-9 min) | first 0/6 *at the time limit* (bug, below); fixed: 0/6, 5 wiped out in 4-8 min | 3.4 game length, then 1.3 |
| 3 | self-play: default bot (A) vs turtle (T: defend posture, waves of 20) | each other, shadowfiend-ii | T won 5/6 | T won 6/6 | 1.2 |
| 4 | slow agent: default bot thinking 800 ms per decision (`FF_THINK_MS`) | Turtle AI, trapped | 2/6 | 2/6 | 1.7 (kills); timing exact: 58 states/min, orders 24-30 ticks late, in both |
| 5 | harvester hunter: vehicles, target harvesters | Normal AI, shadowfiend-ii | 0/6, net -3808 (all close); rerun 0/6, -6825 | 2/6, net -2333 | outcomes <= 2.6; lag shares 2.2-5.0 (below) |
| 6 | self-play: default bot (A) vs rush variant (R) | each other, trapped | A 4-2 (3 wipe-outs) | A 4-2 (3 wipe-outs) | 3.0 (6-tick lag share, now the other way round); outcomes <= 0.3 |

**Local real time is not the platform either.** In round 5 the lag shares disagreed (6-tick
lag 0.94 in real time, 0.90 virtual). The real platform's own recordings have 0.79-0.90
(12-tick lag 0.03-0.17, more for agents that send more batches); the virtual run (0.90 /
0.04) is inside that range, the local real-time run, played while the machine was busy with
benchmarks, is not. A clean rerun gave the same 0.94, but in round 6 the difference went the other way (real 0.91,
virtual 0.93): the lag shares differ by strategy and run more than their per-game spread says,
in either direction, while the platform's recordings span 0.79-0.90.

**Found by round 2's strategy: stepped games never ended.** OpenRA ends a game 1.5 s after the
last opponent lost, with `Game.RunAfterDelay`: a wall-clock timer that only `Game.Loop` runs,
for the one current world. A multi-session engine has no game loop, so a wiped-out player
played on with nothing until the time limit (seen before in a few virtual games with 0 assets).
Stepped games now count the delay in game ticks (38 at 25 ticks/s); real time is unchanged.

## What the strategies taught

- Small infantry waves (4) against the Normal AI feed it kills: ~60 units lost for 9-13 killed.
- Greed loses to a rush: the eco posture (extra refineries before an army) was wiped out by
  OpenRA's Rush AI in 5 of 6 games, in both clocks; an early army or defenses come first.
- A slow agent still plays: at 800 ms a decision it sees a quarter of the views (58 a
  minute instead of 236) and its orders land a second late, yet it held its own against the
  passive Turtle AI (2/6, like the fast bot's typical results). Virtual time can't save an
  agent's own thinking: those games took 491 s instead of 634 s.
- In a mirror, the default mixed army beat the infantry rush 8 of 12 (4-2 in each clock); the
  rush only wins by wiping a base out early.
- Hunting harvesters with vehicles kept games close against the Normal AI on shadowfiend-ii
  (net -700 to -9450) but won none in real time.
- Attacking into a defended base loses on the tiebreak: in self-play the turtle (never attacks
  until 20 units, defends) beat our default bot in 11 of 12 games, real and virtual, by
  +2000 to +10000 net. Most games end at the time limit, where kills minus losses decide: an
  attack must trade well or not happen.

## Ideas left

None that are promising: what remains costs 1-3 % each (allocation-free targeting and
production checks in the engine, a leaner HTTP layer in the server) or would change what is
replicated (the platform's double payload, the runtime's HTTP client). The machine is the
limit: engines running side by side slow each other down, and each game waits on its own
engine, server and agent in turn.
