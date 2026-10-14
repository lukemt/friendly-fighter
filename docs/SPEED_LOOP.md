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

## Strategies played (virtual vs real time)

| Round | Our bot | Opponent, map | Real time | Virtual | Largest |z| |
|---|---|---|---|---|---|
| 1 | rush: waves of 4, infantry, nearest target | Normal AI, trapped | 0/6, net -8758 | 0/6, net -11475 | 1.8 (6-tick lag share) |
| 2 | eco: refineries first, waves of 14, vehicles, enemy base | Rush AI, sidestep | 1/6 (5 bases wiped out in 5-9 min) | first 0/6 *at the time limit* (bug, below); fixed: 0/6, 5 wiped out in 4-8 min | 3.4 game length, then 1.3 |

**Found by round 2's strategy: stepped games never ended.** OpenRA ends a game 1.5 s after the
last opponent lost, with `Game.RunAfterDelay`: a wall-clock timer that only `Game.Loop` runs,
for the one current world. A multi-session engine has no game loop, so a wiped-out player
played on with nothing until the time limit (seen before in a few virtual games with 0 assets).
Stepped games now count the delay in game ticks (38 at 25 ticks/s); real time is unchanged.

## What the strategies taught

- Small infantry waves (4) against the Normal AI feed it kills: ~60 units lost for 9-13 killed.
- Greed loses to a rush: the eco posture (extra refineries before an army) was wiped out by
  OpenRA's Rush AI in 5 of 6 games, in both clocks; an early army or defenses come first.
