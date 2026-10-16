# What wins: the knowledge base

What we know about winning Red Alert games here, each point with its evidence: lab runs, games
and branches you can look up (`lab show <game>`, `lab experiment show <name>`, `lab runs`). It
grows with every experiment; a point without evidence doesn't go in. Started 2026-10-14 from
the first lab leagues and gates, and from Claude playing OpenRA's three AIs by hand
(`lab stats human-claude --by opponent`).

## The score decides almost everything

- **The tiebreak decides.** Nearly every game ends at the time limit (20 minutes on the platform;
  the lab played 10 until run 114, see "20-minute games" below). The tiebreak is
  kills minus losses, in credits.
  - **Evidence:** 184 simulator games of `ff-default` against the Normal AI, before the lab:
    every one went to the time limit. Lab leagues 9, 15, 26 and 28: wipe-outs only against the
    Rush AI and against bots that never defend.
- **Attacking into defenses loses.** Defending is cheaper than attacking: a unit that dies
  attacking a fortified position costs its full price. In self-play the defensive variant beat
  the attacking one.
  - **Evidence:** gate run 12: `ff-turtle` beat `ff-default` 6/6. In the ratings, `ff-turtle`
    is first and `ff-default` last.
- **Against a passive opponent: lose nothing, kill what comes.** One kill with zero losses wins.
  - **Evidence:** `ff-guard-turtle`'s win over the Turtle AI was a single kill at 9:03.
    Claude's Turtle game 3 was +1350 from one Yak shot down at 9:51, with no losses.
- **A 0-0 draw is worth 0, but it is replayed in the tournament,** so it is not a loss. Against
  an opponent you can't beat, not losing is the floor.
  - **This holds against OpenRA's AIs only.** Between two agents, equal kills minus losses goes to the
    second tiebreak, **assets**: the cost of your own units and buildings, with the construction yard
    at $2,000. Cash doesn't count.
  - **Evidence:** run 48: every turtle mirror ended 0-0 and was decided by assets, with both sides at
    $0 cash.
- **Assets are won with the economy.** A refinery comes with a free harvester, so it adds about
  $3,100 of assets for $2,000.
  - **Evidence:** experiment `greed` (run 49): the guard turtle with `greed` 1 or 2 (extra refineries
    and harvesters) beat `ff-turtle` and `ff-guard-turtle` 8/8 each, from 2/8 and 8/16. It scored a
    little less against the AIs (19/24 against 21/24), probably the Rush AI meeting a thinner army.
    Overall: +0.20 points a game, 95% CI +0.04 to +0.36.
- **A scout that dies loses standoffs.** Two dead riflemen (−200) decided many mirror games.
  - **Evidence:** run 48: with a scout, 0/8 and 1/8 against our turtles; without, 4/8 and 4/8.

## 20-minute games (run 116, the champion compare of run 64 at the platform's length)

The platform plays 20 minutes; everything up to run 114 was 10. Same bots, suites and seeds as run
64 (`lab compare ... --seed 41`); 239 games (one lost to an engine stopped by hand).

| Bot | 10 min (run 64) | 20 min (run 116) |
|---|---|---|
| `ff-council-champ-v10` | 95% | **86%** (+0.21 points a game over the greedy turtle, CI +0.10..+0.32) |
| `ff-greedy-turtle-eco` | 86% | 77% |
| `ff-greedy-turtle` | 76% | 64% |

- **v10 stays the best, and the gap grew** (+0.21 vs +0.19). It loses most where the others do.
- **The Rush AI breaks bases late.** v10 against Rush: 7/8 at 10 minutes, 4/8 at 20. Rush wiped the
  greedy turtles 12 times (from 7:48) and v10 3 times, at 17:04-17:25. Moments 2 min before those
  wipes are the set `@late-rush` (`lab moments show late-rush`).
- **Wipe-outs happen now, but only against us.** 41 games ended with a base destroyed, and every
  time it was one of the three candidates that lost it. v10 was wiped 7 times (Rush 3, Normal, Turtle, `exploit-eco-tanks`, `ff-default`), all
  after 12:48. Our bots still never destroy a base.
- **The Turtle AI turns our way with time** (v10 6/8 at both lengths, the turtles 3-4/8 → 5/8):
  the bigger economies pull ahead on kills in the second half.
- **`exploit-eco-tanks` comes back:** v10 8/8 → 6/8 (one wipe at 12:48); the greedy turtles 2-3/8.
- **How v10 dies to the late Rush** (`8f1c3997`, sidestep): at 12:00 it leads (+7,700, 32 combat
  units: 22 rocket soldiers, 7 heavy tanks, 3 V2; cash 0). Rush's wave (riflemen and rocket
  soldiers ~30 each, Tanya, jeeps, light and medium tanks, 8 artillery) wins the fight at
  12:00-12:30 (32 → 9 units, -10,100 in losses). With no cash it can't rebuild; its harvesters go
  (6 → 1 by 16:00), then the base (15:41-17:09). The decisive moment is that fight, not the wipe.
- **From 2 minutes before a wipe nothing saves the base** (probe of @late-rush, runs 120-123: five
  of our bots, 10-11 of 13 wiped whichever plays; at some moments every bot plays the identical
  game). Probe the fight instead: `@rush-wave` (30 s before the first 6,000+ score fall after 9:00).
- 10-minute results overrate every bot against attackers. Judge on 20 (the lab's default since
  2026-10-16); 10 only for quick opening checks.

## OpenRA's AIs (as seen in the lab)

| AI | What it does | What beats it | Evidence |
|---|---|---|---|
| Rush | Attacks at 3:14-4:10 with ~20 infantry, then waves (APCs, flamers, grenadiers) at ~5:30-6:10 and ~8:30. When the first rush lands, every standard opening is wiped out. | A defense right after the barracks; ~10 infantry on `defend` next to it by 3:00; fight inside the base with everything when a wave breaks in (sell what you don't need, buy infantry). | Claude's Rush game: +6500 (`bd4da0a5`). `ff-guard` 3/6 against it, `ff-default` 0/6 (gate run 25). |
| Normal | Fortifies its ore field (tesla, flame towers, SAM, pillboxes around 24-35,36-47 from the corner at 26,32). Keeps a reaction army of ~25-35 infantry with rocket soldiers at home. Rarely attacks bases (in ~25% of games, after 8:30). Allied Normal AIs shell from out of sight with artillery. | Defending until it comes (the turtle wins 4.5-6/6). Or: one big blob of 25-30, committed all at once at their base edge around 8:00: its army is finite, and once it is spent the base edge is open. | Gate run 29: `ff-guard-turtle` 6/6. Claude's Normal game 4: +2000 (`624f5e33`); at 8:40 it killed 20 in 8 s after being down 1350. |
| Turtle | Builds pillboxes, turrets and teslas around its ore field, and sometimes a forward base. Lays mines (seen in 13 of 28 earlier games). Has MH60 helicopters (5 games) and Yak planes (6), and a fast reaction group (jeeps, light tanks, APC). | Defending: rocket infantry or AA for its aircraft, and no losses. Its raiders and planes come to you late. | Claude's Turtle game 3: +1350, zero losses (`lab games --vs ai-turtle`). Turtle games 1-2 lost by attacking (mines, helicopter, reaction group). |

`lab h history` shows these per opponent and map during a game (where it built, what units it
used).

### How the AIs decide (read from the engine's code, 2026-10-16)

From `mods/ra/rules/ai.yaml` and `OpenRA.Mods.Common/Traits/BotModules/SquadManagerBotModule.cs`
and `Squads/` in `~/openra-sim/fork`, at 25 ticks/s. These are the rules, not lab observations:
test before relying on them.

| | Rush | Normal | Turtle |
|---|---|---|---|
| Wave size (`SquadSize` + random 0-29) | 20: a wave leaves within ~1 s of 20 idle units at home | 40-69: rarely reached, so it rarely attacks | 10, but the first attack force is formed at a random time in 0-66 min, and then never again |
| "Rush" check (all its squads converge on our base) | every 24 s, once it has 20+ ground units | every 24 s, 40+ units | never |
| Defends its own things within (`ProtectUnitScanRadius`) | 15 cells | 15 | 30 |

- **The rush check compares its idle units at home with our armed things within 15 cells of our
  construction yard**, defense structures included, by summed damage per second. It goes only when
  it is clearly stronger (about 1.3x or more). Then every squad it has turns on that target, which
  is why one late wave can be all of its army at once. A strong armed presence around the
  construction yard at those checks keeps that from happening. Units fighting 10+ cells out don't
  count.
- **A wave goes for whatever of ours is closest to its leader** (reachable, anything it can
  attack), and on the way it switches to anything within 12 cells. So it hits the first thing of
  ours on its path: harvesters, an army standing forward, an outlying building.
- **A wave flees only when it is hurt.** At high health it always attacks, whatever it faces. Once
  it is down to about half health and we are stronger nearby (10 cells), it runs to a random
  building of its own, unless one of its buildings is within 10 cells. Then it rejoins the pool for
  the next wave. So waves that break on our defenses come back as the next wave: chasing the
  fleeing units is free kills.
- **Hitting any of its buildings or harvesters pulls its idle home units after the attacker,** plus
  any squad within 15 cells (30 for the Turtle). These defenders never flee: they chase while they
  see the attacker, give up a few seconds after losing sight, then look within 12 cells (Turtle 15)
  for something else to hit. **This can be baited.** One fast unit hits a harvester and runs back
  to our army, standing out of reach of their defenses, and the whole reaction group follows into
  it. This is untested (an idea for anyone).
- **v10's late wipes are mostly the AI's nuke, not a siege** (found by the jev family, lab note 48;
  the rules checked in `ai.yaml` and `structures.yaml`). In 34 of v10's 20-minute games against
  the AIs, 6-17 buildings and the army bunched near them died within 1-5 s, at 14.3-19.8 min. Before
  the first building fell, v10 was ahead in 12 of 18 of these wipes (`38de5f1a`: +45,100 at 17:36).
  - **Every AI builds one missile silo** (`mslo`, after a tech center). It charges for 9 min
    (13,500 ticks) and fires at any spot with 3,000+ of our structure value within 5 cells, but
    not where its own units stand. Ukraine adds parabombs; every AI sends a spy plane
    every ~2.5 min.
  - **The spy plane can't be shot down** (`-Targetable@AIRBORNE`), so AA can't deny the nuke
    its vision.
  - **The charge pauses while the AI is low on power** (`PauseOnCondition: disabled`, which is
    `lowpower || powerdown`; the silo draws 150). So killing its power plants stops the clock, and
    killing the silo (2,500) ends it. Both are within V2 range (10) of the outside of its
    defenses (tesla 7).
  - **We have no support powers through the platform** (no such order), so we can't nuke back.
  - **Selling books no loss:** a sold building isn't counted in losses, pays 50% of its value
    (times its health) and gives infantry worth 40%. A player is out only with no building and no
    MCV (jev runs 148-149). The jev family is testing "bank the lead": sell before the nuke while
    ahead, with an MCV kept safe.
- **No capturing through the platform.** The action set has no capture order, so the oil derricks
  on trapped (5), sidestep (2) and shadowfiend-II (5) can't be taken. Each would pay 100 every
  15 s, but the AIs don't use them either.

## Units and tactics

- **V2s (range 10) and artillery (12) outrange every defense:** tesla 7, SAM 7.5, turret 6.5,
  flame tower and pillbox ~5.
  - **How:** shell the ground where a defense stands (`bombard`; no vision needed).
  - **But:** they die in seconds to infantry and fast raiders. Every time we used them without
    a screen in front, they were lost.
  - **Evidence:** Claude's Normal games 2-3 and Turtle game 1. In game 3, artillery
    attack-moving into a fight lost all three.
- **Artillery holds position.** Never `attack_move` artillery: it walks into the fight. Move
  it, set it to `defend`, and bombard.
- **Rocket infantry (e3) are the answer to aircraft and armor;** riflemen can't hit aircraft.
  - **Evidence:** Turtle game 3: e3 shot down the Yak.
- **Defenses must stand near the base.**
  - **The rule:** a building can only be placed next to existing ones. The `at` cell is a hint:
    too far, and the engine puts it by the construction yard, sometimes behind the base.
  - **Evidence:** Claude's Rush game, and Turtle game 3.
  - **Bot hint:** `RedAlertBot._place_spot` asks 6 cells toward the enemy, which is often
    too far.
- **The economy caps the army.** Cash sits at 0 from ~2:30 in most games of our bot. A second
  war factory and spending surplus money matter, and early defenses or infantry delay the
  economy (Turtle game 3: 1 harvester at 4:32).
- **Commit together.** A blob that commits at once breaks an army; units sent in twos and
  threes die one by one.
  - **Evidence:** Normal game 4 at 8:35-8:44. The lost games show waves of 4 dying.

## Openings

| Opening | Against rushers | Against passive opponents | Against attacking bots | Evidence |
|---|---|---|---|---|
| standard (`ff-default`, the live bot) | wiped (0/6) | loses the tiebreak (attacks into defenses) | even | gates 12, 25 |
| guard (`opening: guard`) + attack posture (`ff-guard`) | 3/6 | weak | 5/6 vs `ff-default` | gate 25, league 28 |
| defend posture (`ff-turtle`) | 1/6 | 5.5/6 Normal, 4/6 Turtle | 6/6 vs `ff-default` | gates 12, 29 |
| guard + defend (`ff-guard-turtle`) | 3/6 | 6/6 Normal, 4/6 Turtle | 4/6 vs `ff-default` | gate 29 |
| guard + defend + Jev decisions (`ff-jev-push`) | 6/6 | 3/6 + 2 draws Normal, 5/6 + 1 draw Turtle | 6/6 vs `ff-default`, 4/6 vs `ff-turtle`, 5/6 vs `ff-guard-turtle` | runs 34-43 (experiment `jev-v3`) |

## Decisions by Jev (`bots/jev_bot.py`)

- **What it adds.** A scripted bot does what it is told. The Jev bot rereads the game every 10 s
  (every 3 s in a fight) and changes its posture. It attacks late only when behind, pulls back from
  a losing attack within 3 s, and holds a lead.
  - **Evidence:** `lab jev <game>` on runs 34-43.
- **Rails matter as much as judgment.** Jev followed its playbook literally, and each rail fixed a
  loss seen in the logs:
  - **Committed at an even score:** the attack lost.
  - **Turned around mid-fight when slightly ahead:** the army was cut down while turning back.
  - **"Behind" after the scout died:** the deficit triggered an attack into a turtle.
- **Attacks by opponent** (`lab jev --attacks`):
  - against the Turtle AI they lose (−1056 on average when slightly behind);
  - against our turtles they gain (+1800 to +2300);
  - against the Normal AI they gain only from a real deficit (+370 when behind by more than 500,
    −105 when behind by less).
- **Writing questions for Jev** (`lab jev eval` on `lab/jev_cases.json`):
  - **State numbers literally:** "clearly behind (by 500 credits or more)", not "behind".
  - **Never let two options' criteria claim the same situation:** "defend when even" against
    "commit against a passive opponent" made it defend against an empty base.
  - **Say what was never seen:** "never seen their army: size unknown, not zero". Otherwise
    "nothing seen" reads as a passive opponent.

## Jev as several loops (`bots/jev_council.py`, docs/JEV_LOOPS.md)

- **Half the tokens, same strength so far.** Three loops (strategist, quartermaster, field
  commander), each with its own briefing sent once in the state, played as well as the
  one-call champion v7.
  - **Pooled:** 78% against 75% over runs 59–60, within noise.
  - **Tokens:** about 50k a game against about 100k.
- **It fixes the eco-tank exploiter.** 5/8 and 4/6 against 1/8 and 2/6, with mean net about −4k
  against −14k. "An army unseen after 5:00 is massing tanks" makes it fortify and train rocket
  infantry before the 6:40 wave.
- **The same rule loses turtle standoffs.** 5–7/8 against 8/8: no greed means no assets edge.
  - **The cause is timing:** fortify only until 7:00 (the waves came at 6:37–6:44), then boom
    in a standoff.
  - **Starting greedy matters:** a council that starts with "hold" lost standoffs 4/8.
- **One seed fooled us.** Run 59 alone said "better" (+0.11, CI +0.01 to +0.21); the fresh seed
  of run 60 did not. Confirm a win on a second seed before crowning it.
- **The economy starves before any decision matters.** Cash sits at 0 from 2:00, because the war
  factory comes before the second refinery (built at 3:58 in a lost Rush AI game).
  - **The fix:** `Knobs.early_refineries=1`, a second refinery before the war factory.
  - **It won on two seeds:** the greedy turtle went from 80% to 88% (run 61) and from 72% to 84%
    (run 63), with net +2.5k and +3.7k a game.
  - **It helps most against the eco exploiters** (eco-waves 4/8 to 8/8) and the Rush AI.
  - **The guard still stands:** its pillbox and infantry come first.
- **The best bot so far needs no API.** `ff-council-rules-eco` is the council's loops answered by
  code rules, plus the economy fix: 86% (run 63) and 16/16 against the exploiters. See
  docs/JEV_LOOPS.md.

## Against other contestants' bots (`@field`, docs/EXTERNAL_BOTS.md)

- **A rushing contestant breaks on v10 and destroys the live bot.**
  - `ext-ryan` (ryanxxhuang's rule bot): infantry rush at ~0:55, then waves, focus fire, retreats.
  - Against `ff-council-champ-v10`: 0/18, about −11k a game for the attacker.
  - Against `ff-default`: 4/4, every game a wipe-out by 5:25.
  - **Evidence:** runs 73-74 (experiment `field-e1`).
- **Our champion never finishes weak opponents.**
  - v10 drew 0-0 with OpenRA's Beginner and Easy AIs (1 game each, runs 69-70).
  - ext-ryan destroyed the Normal and Turtle AIs within 7 minutes.
  - A draw is replayed and scores nothing, so finishing is worth points (LAB_PLAN E3).

## How v10 was beaten (Claude advising ext-ryan, runs 75-77)

A rule-based contestant bot with Claude as its advisor beat `ff-council-champ-v10` 2 of 3 games.
Unadvised, the same bot was 0/18 against it. What v10 does not handle (candidates for v11):
- **A massed strike on its guard at 6:30-7:30.**
  - What happened: v10 keeps its army at its ore field and defends. A 28-60-unit blob (medium
    tanks with masses of riflemen) traded up there: +1,100 (game 1) and +3,050 (game 3).
  - Then: the attacker went home with the lead.
- **It does not come back from behind.**
  - Its attack rule needs `net <= -500`, under 3 minutes left and 25+ combat units.
  - Behind by 2,350 for 2.5 minutes (game 3), it stayed home and lost.
  - When it did go out (game 1), its heavy tanks were caught in the open by an all-in.
- **What it does well:**
  - its artillery and V2s pick off units parked within range (three medium tanks, game 1);
  - its economy wins a pure 0-0 asset race: 51.35k against 48.8k (game 2), even against a
    5-refinery opponent.
- **For the attacker:**
  - economy first beats the early rush: assets 30-35k at 6:00 against ext-ryan's ~16k at
    10:00 when it rushed;
  - one strike with everything, not waves;
  - no scouting into its defenses: a lost jeep was −500 that had to be won back.
- **Evidence:** games `0f6ed344`, `f5707d53`, `09b028ce` (`lab show`, notes, transcripts in
  `sim/lab/advise/`), against run 73 on the same seeds.

## Trying to fix v10 (experiment `v11`, runs 83-114): not yet

- **A scripted copy of Claude's plan beats v10 22-33% of the time.** `exploit-ryan-plan`
  (bots/ryan_plan.py): v10 scored 25/32, 25/32 and 50/64 against it (runs 84, 91, 113). Every
  earlier bot, ext-ryan included, scored 0 against v10. It is the first exploiter that works.
- **Why v10 loses those games: a smaller army at the moment of the strike.**
  - At 5:00-6:00 the scripted plan has about twice v10's army (37 against 18 units at 5:00 in
    `1b8a1c15`).
  - v10 sits on unspent ore (1,800 at 5:00): one barracks and one war factory can't spend its
    income.
  - Its "fortify" plan (enemy army unseen after 5:00) trains rocket soldiers into a mass of
    riflemen.
  - The strike then catches v10's army about 10 cells from its base, outside its towers
    (lost 17-19, killed 4-8).
- **Five candidate fixes, none better over whole games** (32-64 paired games each against the
  scripted plan):

  | Candidate | Change (defaults keep v10) | Points against the scripted plan | Note |
  |---|---|---|---|
  | `ff-council-v11` | mixed training when blind (`blind_production`), comeback with 10+ units (`comeback_army`, `commit_min_army`) | 25/32, same as v10 | |
  | `ff-council-v11b` | v11 + factories from 800 unspent (`surplus`), a second barracks (`extra_barracks`) | 20/32 (net −3.3k, CI excludes 0) | worse |
  | `ff-council-v11c` | v10 + meet attackers at our defenses when outnumbered 1.3x (`fallback`) | 25/32 | triggered in 6 of 32 games |
  | `ff-council-v11d` | v11 + fallback | 24/32 | |
  | `ff-council-v11-mixed` | mixed training when blind, alone | 50/64, same as v10 | fresh seeds |

- **Branches show where the changes act.** From 5:30 of v10's 7 losses (`lab branch`, 2 repeats):
  - mixed training wins 9/14 (net +4.1k), against v10's 12/28 (+0.4k);
  - the comeback rule alone loses ground (5/14, −0.6k): attacking out of a losing position
    mostly loses more;
  - over whole games the gain is given back elsewhere: a small change sends the whole game down
    another path. Small effects need hundreds of games, and branches, to see.
- **Next:** the gap is economic and early (army size at 5:00-6:00), not a rule about fights.
  Candidates:
  - turn unspent ore into army earlier without delaying the economy (v11b's factories came too
    early);
  - scout once to see a massing army;
  - more advised games against v10 to find what else a human-level plan exploits.

## What the exploiter search found (`lab exploit`)

- **Eco attackers beat our defenders.** An opponent with extra refineries and no defenses, attacking
  in constant small waves or in waves of 16 tanks, beat the v7 champion 6/8 and 5/8 (run 52), and
  wiped it out 4 times by about 8:20. They are now sparring partners (`@exploiters`).
- **Why:**
  - **The guard opening costs about 1,000 of army by 3:00.** It has 1.4 harvesters at 3:00, against
    2.2 for the standard opening (`lab curves`).
  - **Going greedy in the middle of the game starves production** right when the attack comes. v7
    had 11 units from 2:00 to 7:00.
  - **Greedy from the start is fine:** the biggest army at 6:00 (11.7k) and 8.6 harvesters.
- **Greed has a price against the Rush AI.** Its second wave (about 7:00) wipes out a greedy base
  that beat the first rush. Jev's read ("rusher": stay safe) avoided this; v9 makes it the rule.
- **More defenses: no effect.** A cap of 4 or 6 instead of 2 changed nothing (run 56, ±0.05 points a
  game), because defenses come after the war factory and two refineries.
- **A small difference needs many games.** Bots in the 75-80% range differ by a few percent, while
  one seed swings a matchup from 3/6 to 5/6. Paired seeds give an interval of ±0.1 points a game
  per 50 pairs, so a 5% difference needs about 400. Spend the games on big changes.

**Next:** pick the opening by opponent. A rush shows by ~2:30 (scouting) and in an opponent's
history; the guard costs economy against everyone else.
