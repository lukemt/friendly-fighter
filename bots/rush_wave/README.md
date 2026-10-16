# The rush-wave family

> A family of bots built in its own session (AGENTS.md). Keep this file current: it is how the
> coordinator and the other sessions learn what this family is and what it has shown.

## Brief

*From the coordinator, 2026-10-16. Read AGENTS.md first: the rules and the funnel.*

**Goal.** Make the champion (`ff-council-champ-v10`) survive the Rush AI's wave and win those
games, without losing ground anywhere else. Your bots are v10 with changes for that fight.

**The weakness, with its evidence.**
- **At 20 minutes, v10 beats the Rush AI only 4 of 8** (run 116, wiped 3 times at 17:04-17:25).
  At 10 minutes it was 7 of 8 (run 64): the second half of the game is where it breaks.
- **How it dies** (docs/STRATEGY.md "How v10 dies to the late Rush"; game `8f1c3997`, sidestep):
  - At 12:00 it leads: score +7,700, 32 combat units (22 rocket soldiers, 7 heavy tanks, 3 V2),
    cash 0.
  - Rush's wave (riflemen and rocket soldiers ~30 each, Tanya, jeeps, light and medium tanks,
    8 artillery) wins the fight at 12:00-12:30: 32 → 9 units.
  - With no cash it can't rebuild. Its harvesters die (6 → 1 by 16:00), then the base
    (15:41-17:09).
- **From 2 minutes before the wipe, nothing helps** (probe runs 120-123, set `@late-rush`): the
  decisive moment is the fight, not the wipe.
- **An earlier wave can break it too:** Rush wiped v10 at ~9 minutes on trapped
  (runs 126-127, seeds 770539 and 770540).
- **The board** (`lab board`, first run 128): v10's points against `ai-rush` there are your
  starting line.

**Start from.**
- **Moment set `@rush-wave`** (shared; don't change it): 16 moments 30 s before the first
  6,000+ score fall after 9:00 against Rush (run 116). Five are v10's (ids 17, 20, 22, 26, 29);
  the rest are the greedy turtles'. Make your own sets named `rush-wave-...` when you need others,
  for example v10's moments only, or earlier ones:
  `lab moments find rush-wave-v10 --runs 116 --vs ai-rush --bot ff-council-champ-v10 --kind swing --after 9:00 --lead 60`.
- **The code:**
  1. Copy the frozen champion `bots/frozen/ff_council_champ_v10/` to `bots/rush_wave/v10/`. Its
     imports are relative, so the copy runs as is.
  2. Register it: `lab bots add rush-wave-base --kind module --factory bots.rush_wave.v10.bot:create_agent --kwargs '{"oracle": "rules", "plan": "boom", "keep_first": 240, "early_refineries": 1}'`
     (v10's settings in lab/bots.json).
  3. **Check it plays exactly like v10 first:**
     `lab play rush-wave-base ai-rush --games 2 --seed 77 --map trapped` must end like v10's
     runs 126-127, with v10's side at net -25,700 and -24,600.
  4. From then on, add every change as a setting (a keyword argument) that is off by default,
     so `rush-wave-base` stays v10. The copy is yours: change it freely. Shared code stays
     untouched (AGENTS.md rule 8).
- **Opponents:** `ai-rush` first, then the board's six for regressions.

**Ideas to try** (the idea cards; the first four first):
1. **Anti-infantry production** against an infantry-heavy enemy: flamethrowers, riflemen and heavy
   tanks instead of rocket soldiers. The enemy's mix is visible.
2. **Fall back** and fight under the defenses when a wave much stronger than the army comes. The
   scripted bot has had a `fallback` knob since 2026-10-15 (`agent/redalert_bot.py`). Your copy
   of v10's `redalert_bot.py` predates it: port the idea into your copy.
3. **More defenses at the approach by ~11:00** (tesla coil, flame towers). v10 caps defenses at 2.
4. **A cash reserve** after ~10:00 (e.g. 1,500), to rebuild right after a lost fight.
5. Kill the artillery first.
6. Pull the harvesters home when the army is gone.
7. Hit the wave while it gathers.

**Method** (the funnel):
1. **Probe:** `lab probe @rush-wave --variants rush-wave-a,rush-wave-b --halving --stop-after 180 --stop-quiet 45 --experiment rush-wave-<idea>`.
2. **Screen:** `lab compare ff-council-champ-v10,rush-wave-x --baselines ai-rush --games 8 --seed <a new one>`, then a second seed.
3. **Fast suite:** `--baselines @ais,@exploiters`.
4. **The board:** freeze, `lab bots tag ... --tags +board`, then `lab board play`.

**Done when** either:
- a frozen entry beats v10 against `ai-rush`, with its paired interval above 0 over 16 games on 2
  seeds, and the board shows it no worse than v10 overall; or
- the ideas run out, and this file holds a written negative result: what was tried, with
  numbers, so nobody repeats it.

**Limits:** no shared code changes; one big run at a time; leave the board's reference games and
the shared moment sets alone.

## The idea

v10 trains rocket soldiers whenever it has seen enemy vehicles (`Rules.production`), which the Rush AI
always has. Its wave is mostly infantry (riflemen and rocket soldiers ~30 each), which rockets are
poor against. Idea 1 (`anti_infantry`) switches the factories to anti-infantry units when the enemy
seen lately is mostly infantry.

## Bots

| Bot | What it is | Status |
|---|---|---|
| `rush-wave-base` | Copy of frozen v10 (`bots/rush_wave/v10/`), v10's settings. Every change from here is a keyword argument that is off by default. | Reproduces v10 (runs 130, 133) |
| `rush-wave-a` | Base + `anti_infantry=0.6` (idea 1): when 60%+ of the enemy combat units seen in the last 240 s are infantry (10+ seen, from 5:00), train flamethrowers, riflemen and grenadiers instead of rocket soldiers; artillery, no V2s. | Screened on 2 seeds (runs 137, 139): no effect |
| `rush-wave-b` | Base + `guard_defenses=3`, `guard_infantry=16` (idea 3, early). | Trapped screen, seed 990 (run 154): no clear gain |
| `rush-wave-c` | b + `late_buildings_from=300` (no service depot or dome before 5:00). | Run 154: no gain (3/8) |
| `rush-wave-d` | Base + `guard_mix` (the guard alternates rifles and rockets). | Trapped, 2 seeds (runs 157, 158): 7/16 vs v10's 6/16, no clear difference |
| `rush-wave-e` | d + `guard_infantry=14`, `guard_defenses=2`. | Worse: 2/16 (seed 990: net -25,600, CI -46,277..-4,923) |
| `rush-wave-f` | Base + `defend_radius=9` (the army only leaves the towers for enemies within 9 cells, v10: 16). | 3 Rush maps (run 162): 2/12 vs v10's 5/12, no clear difference (-0.25, CI -0.51..+0.01) |
| `rush-wave-g` | f + `guard_mix`. | Run 162: 2/12 (-0.25, CI -0.60..+0.10) |
| `rush-wave-h` | Oracle test: knows Rush is coming. `plan=hold`, no early refineries, no boom (a safe opening). | Run 164: 3/12 vs v10's 7/12 (-0.33, CI -0.70..+0.04) |
| `rush-wave-i` | Oracle test: the same with `plan=fortify`. | Run 164: identical to h (3/12) |
| `rush-wave-k` | Base + `defend_radius=22` (the army goes out to enemies up to 22 cells from home; v10: 16). | **Lead.** Seeds 7001-7004 (3 Rush maps, 12 games each): 33/48 vs v10's 27/48; net better on 4 of 4 seeds; no single seed's CI excludes 0. Confirming on the new engine |
| `rush-wave-l` | Base + `defend_radius=28`. | Run 167: 7/12 = v10 (no effect) |

## Results

(Each claim with its evidence: run ids, `lab probe` / `lab compare` output, games to look at.)

- **`rush-wave-base` plays exactly like v10** (run 130, `lab play rush-wave-base ai-rush --games 2
  --seed 77 --map trapped`, 2026-10-16): net -24,600 (game `02524faf`, wiped 8:44) and -25,700
  (game `e197e04a`, wiped 9:06), the same as v10's runs 126-127. Re-checked after the `anti_infantry`
  code went in (run 133, games `a21a9a43`, `36825e00`): -24,600 and -25,700 again.
- **Idea 1, probe: inconclusive** (experiment `rush-wave-antiinf`, runs 135-136, `@rush-wave`, halving,
  180 s): `rush-wave-a` -5,900 against the control over 6 moments (95% CI -21,097..+9,297), the
  base -933 over 3. Too few pairs. What it did show: the setting fires (`build=antiinf` in 5 of 5
  `rush-wave-a` games, never in the 2 base games), but only once the wave is in view (20-51 enemy
  units seen). **A probe branch starts with an empty memory of the enemy**, so a trigger that reads
  what was seen earlier is judged unfairly; it needs full 20-minute games (stage 2).
- **Idea 1, stage 2 screen: no effect** (`lab compare ff-council-champ-v10,rush-wave-a --baselines
  ai-rush --games 8`, 20 minutes, experiment `rush-wave-antiinf`):

  | seed | run | v10 | rush-wave-a | paired points a game (95% CI) | paired net (95% CI) |
  |---|---|---|---|---|---|
  | 880 | 137 | 6/8, +15,988 | 7/8, +23,538 | +0.12 (-0.12..+0.37) | +7,550 (-1,739..+16,839) |
  | 990 | 139 | 5/8, +17,725 | 5/8, +12,400 | +0.00 (-0.15..+0.15) | -5,325 (-10,921..+271) |

  Pooled 12/16 against 11/16, the net difference changes sign between seeds: nothing better. It
  switched on at 5:00-6:12 in every game (the first rush is mostly infantry), so this tests
  "anti-infantry units from 5:00 on". It does not change the early wipe: on trapped, seed 990, v10 and
  `rush-wave-a` ended identically (games `9368b4cc`, `eb80938d`, -25,100, wiped at 8:36).
  **Idea 1 does not clear the bar** (a paired interval above 0 over 16 games on 2 seeds). Not frozen,
  not entered on the board.

- **Where v10 dies on trapped: the first wave, not the late game** (games `eb80938d`, `28d5a9f5`, seed 990).
  Rush's first wave arrives at ~4:38-5:04: 11 rifle, 6 rocket soldiers and 2 light tanks (~$4,300).
  v10 has 11 riflemen and 1 flame tower, cash 0 since 3:30 (it spent on 3 refineries and a service
  depot); it loses all 11 in ~10 s, then the barracks, power and construction yard fall before 6:00 and
  it never recovers (wiped 8:36). So the 12:00 fight of the brief is one way to die; on trapped it
  happens before 6:00. A unit mix from 5:00 (idea 1) comes after that fight.
- **A bug in v10's guard opening:** the guard orders infantry in twos and picks
  `infantry[ordered % 2]`; `ordered` is always even, so it **only ever trains riflemen, never a rocket
  soldier**, whatever the enemy has (light tanks run through them). Fixed only behind `guard_mix`.
- **Guard variants b, c, base** (run 154, `lab compare ... --map trapped --games 8 --seed 990`, 20
  minutes): v10 4/8 (+11,062), base 4/8 (+11,062, identical to v10 game by game: the new settings
  leave the defaults alone), b 5/8 (+4,494), c 3/8 (+1,300). Paired against v10: b +0.12 points a game
  (-0.32..+0.57), c -0.12 (-0.57..+0.32): no clear difference. A bigger riflemen-only guard still
  loses the 5:04 fight (b, game `28d5a9f5`: 21 lost, 13 killed).

- **Guard variants d, e** (trapped, 20 min, 8 games each): seed 990 (run 157): v10 4/8, d 3/8, e 1/8;
  seed 880 (run 158): v10 2/8, d 4/8, e 1/8. `guard_mix` (d) is neutral; a bigger guard (e) is worse:
  money spent on the early guard costs more than it buys.
- **Fight at the towers** (`defend_radius=9`, f/g; run 162, `--map trapped,sidestep,shadowfiend-ii --games 12
  --seed 4242`): v10 5/12, f 2/12, g 2/12; no clear difference, but the direction is negative.
- **What the database says about v10 vs `ai-rush`** (80 full 20-minute games, version `154ffc3c`; wins/losses
  on trapped 16/29, sidestep 13/4, shadowfiend-ii 6/7): at 3:00 and 4:00 the wins and the losses look
  alike (about 10-11 combat units, 4-5 harvesters, 10 buildings); first contact is at ~4:55-5:10 either
  way. They differ in the first fight: by 6:00 the losses have lost 11.0 units (trapped; 5.2 in wins)
  and killed 12.7 (14.8). So the first fight at the base decides most games, and it is close to even
  for the same opening: a bigger guard or another stance is a coin flip around that fight, not a fix.

- **Detecting Rush early is worth nothing** (oracle test, experiment `rush-wave-opener`, run 164, 3 Rush
  maps, 12 games, seed 7001): an opening that knows Rush is coming and plays safe (h: hold, i: fortify; no
  greed, no early refineries) went 3/12 against v10's boom at 7/12, net -14,242 (CI -29,470..+987). v10's
  economy-first opening is better even against Rush. (`STRATEGY.md`: "pick the opening by opponent" is
  not supported against Rush.)
- **The database, deduplicated** (80 v10 vs `ai-rush` games = 56 distinct scenarios, deterministic: no repeat
  differed; v10 wins 28: trapped 10/28, sidestep 12/16, shadowfiend-ii 6/12):
  - **Nukes are a minor cause against Rush** (lab note 51): 4 of its 40 losses show a nuke-like burst, all
    at 18.8-19.8 min. 17 losses end before 12:00 (most on trapped, 8.4-9.1 min, after a lost first fight).
  - **The late losses (21 scenarios, >= 12 min) are attrition at home, not our attacks.** Wins and late losses are
    equal at 3:00-4:00; by 8:00 the late losses have 23 units (wins 33) and 15.0k of army (18.5k); by 10:00 23
    against 37 units. They lose twice the units per two minutes (9.0 against 4.5 at 6-8 min, 12.9 against
    7.1 at 8-10 min) and build about the same from 8:00. The bot is in the defend posture 90-97% of the time
    from 6:00 to 12:00 in both groups, with no attack waves (14 games with logs).

- **Bigger defend radius** (experiment `rush-wave-reach`; `--map trapped,sidestep,shadowfiend-ii --games 12`;
  paired against v10 on the same scenarios):

  | seed | run | engine | v10 | k (22) | paired points a game (95% CI) | paired net (95% CI) |
  |---|---|---|---|---|---|---|
  | 7001 | 167 | with nukes | 7/12 | 9/12 | +0.17 (-0.16..+0.49) | +7,921 (-7,719..+23,560) |
  | 7002 | 171 | with nukes | 5/12 | 8/12 | +0.25 (-0.10..+0.60) | +11,621 (-865..+24,107) |
  | 7003 | 176 | with nukes | 7/12 | 9/12 | +0.17 (-0.16..+0.49) | +10,221 (-7,813..+28,255) |
  | 7004 | 182 | no superweapons | 8/12 | 7/12 | -0.08 (-0.37..+0.21) | +3,158 (-12,099..+18,416) |

  Radius 28 (l, run 167): 7/12 = v10. Radius 9 (f, run 162): 2/12 against v10's 5/12. The trend
  across 9 / 16 / 22 / 28 is not monotonic, so 22 may be a lucky draw: more games on the new engine
  (no superweapons) decide it.

## Core changes

(Every change to shared code, rule 8: the file, what it adds, that it is off by default, its test.)

## Open questions and next steps

- **Idea 1 against the jev family's riflemen rule** (coordinator's request): both trigger on 60%+ of the enemy
  units seen recently being infantry. Mine (`anti_infantry`): a 240 s window, 10+ units seen, from 5:00,
  and it trains flamethrowers, grenadiers and riflemen (artillery, no V2s); no effect in 16 full games (runs
  137, 139). Theirs: riflemen only, a 90 s window; a gain on 10:00 moments (run 190), whole games pending
  (run 192). If theirs gains in whole games, the difference to test is the unit (riflemen vs flamers and
  grenadiers) and the window (90 s vs 240 s: mine stayed on from 5:00 to the end in every game).
