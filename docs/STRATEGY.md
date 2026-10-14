# What wins: the knowledge base

What we know about winning Red Alert games here, each point with its evidence: lab runs, games
and branches you can look up (`lab show <game>`, `lab experiment show <name>`, `lab runs`). It
grows with every experiment; a point without evidence doesn't go in. Started 2026-10-14 from
the first lab leagues and gates, and from Claude playing OpenRA's three AIs by hand
(`lab stats human-claude --by opponent`).

## The score decides almost everything

- **The tiebreak decides.** Nearly every game ends at the 10-minute limit. The tiebreak is
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

## OpenRA's AIs (as seen in the lab)

| AI | What it does | What beats it | Evidence |
|---|---|---|---|
| Rush | Attacks at 3:14-4:10 with ~20 infantry, then waves (APCs, flamers, grenadiers) at ~5:30-6:10 and ~8:30. When the first rush lands, every standard opening is wiped out. | A defense right after the barracks; ~10 infantry on `defend` next to it by 3:00; fight inside the base with everything when a wave breaks in (sell what you don't need, buy infantry). | Claude's Rush game: +6500 (`bd4da0a5`). `ff-guard` 3/6 against it, `ff-default` 0/6 (gate run 25). |
| Normal | Fortifies its ore field (tesla, flame towers, SAM, pillboxes around 24-35,36-47 from the corner at 26,32). Keeps a reaction army of ~25-35 infantry with rocket soldiers at home. Rarely attacks bases (in ~25% of games, after 8:30). Allied Normal AIs shell from out of sight with artillery. | Defending until it comes (the turtle wins 4.5-6/6). Or: one big blob of 25-30, committed all at once at their base edge around 8:00: its army is finite, and once it is spent the base edge is open. | Gate run 29: `ff-guard-turtle` 6/6. Claude's Normal game 4: +2000 (`624f5e33`); at 8:40 it killed 20 in 8 s after being down 1350. |
| Turtle | Builds pillboxes, turrets and teslas around its ore field, and sometimes a forward base. Lays mines (seen in 13 of 28 earlier games). Has MH60 helicopters (5 games) and Yak planes (6), and a fast reaction group (jeeps, light tanks, APC). | Defending: rocket infantry or AA for its aircraft, and no losses. Its raiders and planes come to you late. | Claude's Turtle game 3: +1350, zero losses (`lab games --vs ai-turtle`). Turtle games 1-2 lost by attacking (mines, helicopter, reaction group). |

`lab h history` shows these per opponent and map during a game (where it built, what units it
used).

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
  factory comes before the second refinery (built at 3:58 in a lost Rush AI game). See
  `Knobs.early_refineries`.

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
