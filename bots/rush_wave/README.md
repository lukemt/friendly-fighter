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

## Core changes

(Every change to shared code, rule 8: the file, what it adds, that it is off by default, its test.)

## Open questions and next steps
