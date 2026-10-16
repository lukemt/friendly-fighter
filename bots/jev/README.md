# The jev family

> A family of bots built in its own session (AGENTS.md). Keep this file current: it is how the
> coordinator and the other sessions learn what this family is and what it has shown.

## Brief

*From the coordinator, 2026-10-16. Read AGENTS.md first: the rules and the funnel.*

**Goal.** Find out where Jev (TypeSafe System One: typed answers to questions about a state) makes
our bot better, and build a Jev bot that **beats the champion** (`ff-council-champ-v10`) on the
board. Explore freely: kinds of questions, where and how often to ask, how many decision loops,
and different architectures.

**What we know** (docs/STRATEGY.md "Decisions by Jev" and "Jev as several loops", docs/JEV_LOOPS.md):
- Jev bots so far are the scripted bot (`agent/redalert_bot.py`, the hands) steered by Jev
  answers, with rails in code: `bots/jev_bot.py`, plus the council `bots/jev_council.py`
  (three loops: strategist, quartermaster, field commander, each with its own briefing and pace).
- The council with Jev equalled v7 (runs 59-60). The council with **code** answering the same
  questions (`oracle="rules"`) is as good, and is the champion v10: **Jev hasn't yet shown that
  it adds anything over rules.** That's the open question.
- The cloud ran out of credits (run 60). **Use the local server:** this worktree's `.env` has
  `JEV_URL=http://100.93.0.5:8090/v1/systemone` (model `jevk5-9b-v0.3.3`, no key). Calls take
  **~1.1-1.5 s** (the first ~7 s), against ~0.3 s in the cloud: the answers may differ from the
  cloud model's too.
- The board (`lab board`): v10 73%; weak against `ai-normal` 3/8 and `ai-rush` 4/8. Those are the
  places a better decision could pay.

**Time is the constraint.** On the platform the game runs in real time (25 ticks/s) and never
waits: a 1.2 s answer is 30 ticks of game the bot plays on old decisions.
- **Register Jev bots with `--think measured`**, so the lab charges the real time a decision takes
  to the game clock. With the default fixed think time (5 ms) a Jev call costs no game time in the
  lab, and the results would flatter slow bots. Check this works as intended before trusting any
  number (e.g. game log latencies vs game time).
- Ask only where a decision matters and code can't decide it well. Never block the bot's hands on
  an answer: the scripted bot keeps playing, Jev adjusts. Ask in the background, cache answers,
  skip unchanged states (v6 did), batch questions into one call.

**Ideas to explore** (not a checklist):
- **Where to ask:** the posture at a fight (attack, hold, fall back), the response to Rush's wave
  around 12:00 (`@rush-wave`), opening and economy choices, the opponent's type, when to finish a
  weak opponent (we never destroy a base).
- **Loops:** 1, 2, 3 or more decision loops; their pace (every N s, or on events only); one call
  with many questions against several small ones; loops in parallel.
- **Architectures:**
  - rules first, with Jev only when the rules are unsure;
  - Jev as a critic or veto of the rules' choice;
  - Jev scoring a few candidate plans that code generates (`score` questions);
  - a slow strategist plus a fast tactician;
  - Jev deciding only between games: the opponent read for a best-of-3.
- **Measure decisions, not only games.** Branch at the moments where Jev and the rules disagree,
  play both choices (`lab coach`, `lab probe`), and see which was right. `lab jev eval` scores Jev
  on labelled cases (lab/jev_cases.json).

**Start from.**
1. `lab jev` and docs/JEV_LOOPS.md: how the existing bots ask.
2. Check the local server from the lab: one game of `ff-council-jev` (or your copy of it) with
   `--think measured`, then `lab jev last` to see the calls and latencies.
3. Your bots live in `bots/jev/`. Build on `bots/jev_bot.py` and `bots/jev_council.py` by
   importing or copying them (don't change them: rule 8). The control for each idea is the same
   bot with code answering (`oracle="rules"`) or with Jev off.

**Method** (the funnel, AGENTS.md):
- idea card → moment probe (`@rush-wave`, or sets of your own named `jev-...`) → screen vs the
  weak spots (`ai-normal`, `ai-rush`) → fast suite → the board.
- A Jev bot is only better if it beats **both** v10 and its own rules control on the same seeds.

**Done when** either:
- a frozen Jev bot on the board beats v10, with its paired interval above 0, and beats its rules
  control; or
- a written answer here on where Jev helps and where it doesn't, with numbers (that's worth
  having too).

**Limits:**
- The local server is shared by every game you run at once: with 5 slots, five games ask together
  and answers get slower. Measure that, and use fewer workers if it distorts the results.
- No shared code changes without rule 8.
- One big run at a time.

## The idea

> **Parked (2026-10-16, the coordinator with the user; lab note 54).** The platform has no support-power
> order, so no tournament opponent can nuke, parabomb or spy-plane us; only the OpenRA AIs in the lab do.
> "Bank the lead" defends against a lab artifact, so it stops here. What stays useful is the *analysis*:
> **late wipes in the AI games are noise for judging a bot**, and the facts about the engine below.
> The code (`bots/jev/laststand.py`, off by default) and the sets `@jev-ls-wipes`, `@jev-ls-holds` stay.

*Added 2026-10-16 by the jev session after the user's go to "make the best champion possible".*

**v10 mostly loses by a late wipe while it is ahead on the score, and the wipe is one second long.**
- Of v10's 20-minute games against the AIs, the losses that end by `victory` of the AI all come
  after 12:00, and in 12 of 18 the score was *positive* when the first building fell (+9,400,
  +13,000, +17,150, +19,400 against the Normal AI; +24,050 against the Turtle AI; one game +45,100).
- The fall is not a siege. `lab watch 38de5f1a --seat 1 --every 0:01 --from 17:44 --to 17:47`: at
  17:45 the base stands (18 buildings, net +43,300); at 17:46 17 buildings are gone and nothing
  hostile is in sight (net +28,800). In run 156 (`367635db`, 18:52 to 18:56) the army went from 60
  units to 8 and the buildings from 17 to 4 in four seconds, net +16,650 to -17,550.
- **What does it** (engine, `mods/ra/rules/ai.yaml` `SupportPowerBotModule` and the Soviet airfield
  and missile silo in `structures.yaml`; `badr.bomber` is in sight at 18:54 in `367635db`): the
  Soviet AIs fire their support powers at the densest structures. The nuke (missile silo, charge 9
  minutes) goes for any region with structures worth 3,000 or more, parabombs (Ukraine's airfield,
  every 5 minutes) for any structure, the spy plane (every 2.5 minutes) too. Every AI can build the
  silo (its prerequisite is any tech center), so the big drops hit Soviet and Allied opponents alike.
  The coordinator wrote it up in `docs/STRATEGY.md` (and `lab superweapons` flags the games); the
  codex family found the bombers from the other side (AA, evacuation, repair: `bots/codex/README.md`).

**What the engine rules allow** (all measured, runs 148 and 149, game `2e110913`):
- A **sold building is no loss.** `deaths_cost` only counts what is destroyed. A sale pays 50% of the
  cost times the building's health and spawns infantry worth up to 40% more (`SpawnActorsOnSell`):
  selling 16 buildings gave 6,500 cash and 75 units at no loss.
- A player is **defeated only when no building and no MCV is left.** Units don't count: a game with
  an MCV alone and 109 units went on to 10:00 (`2e110913`); with no MCV and 0 buildings it ended at
  once (`38df204f`, the deployed MCV sold as a yard).
- The **AI's squads aim at the nearest actor of ours, fog or not** (`FindClosestEnemy` over
  `World.Actors`), so a hiding place buys only the time the AI needs to walk there.

**So: bank the lead.** A Soviet AI cannot nuke what is not there: with all buildings sold there is no
structure value for its decisions, no loss is booked, and a spare MCV keeps us in the game. The
bots below test it, in parts, on top of a copy of v10 (`bots/jev/v10/`, `jev-base`):
1. **Insurance:** one MCV (2,000, 40 s) from a given time, sent to a hiding place chosen from the
   map picture (far by path from both bases), running from enemies that come near.
2. **Overrun sale:** the whole base is sold when the enemy at home is worth 3,000+ and 1.3 times ours
   and buildings are falling, while the score is not lost.
3. **Cash-out:** at a set time, ahead by 2,000+, the whole base is sold; the army and the
   harvesters go to the MCV.

These are decisions of *when*, so they are also what Jev could be asked. The rules version comes
first; Jev is tried against it afterwards (paid games, a handful: AGENTS.md rule 11).

## New direction (2026-10-16, after the parking): fights and composition, against agents

Nothing here needs a nuke or a support power. Every setting is opt-in in this family's copy of v10
(`bots/jev/v10/`, no shared code), off by default; `jev-base` plays like v10 (parity checked in the
probes: same branches, same scores).

1. **Where the army waits** (`stage_cells`, `regroup`; experiment `jev-stage`). v10 stages new units a
   fifth of the way to the enemy base: 10-11 cells on trapped and sidestep, 17-18 on shadowfiend-ii,
   while its defenses stand 6 cells out (`_place_spot`). In the fight of `1141b291` (12:30, trapped) the
   46-unit army stood in a long column at 8-17 cells from the yard, beyond the tower at 8 cells, and
   lost 41 units in 36 s. Hypothesis: waiting 5-8 cells out puts the fight under the towers.
2. **What the infantry is** (`blind_production="rifles"`, `rifle_rule`; experiment `jev-comp`).
   PakledAgent's matchup model (docs/research/red-alert-unit-matchups.md in
   github.com/SirLinhart/PakledAgent, read only) gives the rifleman 1,056 effective DPS per $100
   against infantry and 5,000 HP per $100; the rocket soldier 78 against infantry and 833 against
   heavy armour. v10 trains only rocket soldiers once it has seen any vehicle, or when blind after
   5:00 (`Rules.production`), so most of its infantry is the worst unit against an infantry swarm
   (its losses in the wipes above: `e3 x32, e1 x9`). `exploit-ryan-plan` fields 88 units by 10:00, mostly
   riflemen.
3. **How the army fires** (`focus`). The same model: "targeting is worth as much as composition".
   Defending, each kind of unit gets an `attack` order on the nearest enemy of the kind it hurts most.
4. **Measured on the way:** v10's `choose_action` costs 0.53 ms mean per view (p95 0.8 ms, max 13 ms,
   3,500 calls of a 20-minute game, run 168), so the platform's view interval (~0.24 s) is not
   threatened by the champion's own code, and the lab's fixed 5 ms think is 10 times conservative.

## Bots

| Bot | What it is | Status |
|---|---|---|
| `jev-council-m` | `ff-jev-council` (`bots.jev_council:create_agent`) with `--think measured`, for the Jev check | played 1 game (run 151) |
| `jev-base` | a copy of frozen v10 (`bots/jev/v10/`), the base of the family and the control | parity with v10 checked: identical in all 15 branches of `@normal-fight`, twice (runs 170, 180) |
| `jev-selltest` | mechanics test: second MCV at 6:00, then sells every building | runs 147-149 |
| `jev-ls1` | `jev-base` + insurance MCV from 10:30 + overrun sale | probe run 156 (smoke) |
| `jev-ls2`, `jev-ls2h` | `jev-base` + insurance from 10:30 + cash-out at 18:00 (`h`: the army goes to the MCV) | `jev-ls2h` probe run 161 |
| `jev-ls3` | insurance from 12:00 + overrun sale + cash-out at 16:00, the army hides | probe run 163 |
| `jev-ls4s`, `jev-ls4m`, `jev-ls4t` | no fleeing; cash-out at 17:00; army and harvesters split / with the MCV / stay | run 166, stopped after 5 of 33 games (below) |
| `jev-lsj` code: `laststand_jev.py` | Jev decides when to bank (asks every 15 s from 14:00) | written and unit-tested, never played; **parked** |
| `jev-timed` | v10 timing every `choose_action` (wall and CPU) | run 168: 0.53 ms mean per view |
| `jev-st5`, `jev-st8` | `jev-base`, the army waits 5 / 8 cells from home (`stage_cells`) and idle units regroup there | probe run 170; `jev-st5` in full games: run 181; frozen as `jev-st5-v1` |
| `jev-a1`, `jev-a2` | `jev-st5` + the riflemen rule (`a1`); + focus fire (`a2`) | registered |
| `jev-rifb` | `jev-base`, riflemen only while the enemy army is unseen after 5:00 (`blind_production="rifles"`) | registered |
| `jev-rifr` | `jev-rifb` + riflemen at 60%+ infantry among the enemy seen, rockets at 35%- (`rifle_rule`) | registered; the rules side of the Jev test |
| `jev-focus` | `jev-base` + focus fire when defending (`focus`) | registered |
| `jev-sjs`, `jev-sjj` | `jev-rifr` with the quartermaster question also asked of Jev: `s` shadow (rules decide, Jev logged), `j` Jev decides | `bots/jev/splitjev.py`, unit-tested, not played |

## Results

**The Jev check (the brief's first task).**
- **Local server (`JEV_URL=http://100.93.0.5:8090`): down.** Run 138 (`jev-council-m` against
  `ai-normal`, 1 game, seed 7): every one of the 247 requests timed out (latency median 6,094 ms =
  the client's 6 s timeout); the game played on frozen decisions and counts for nothing. By hand,
  `GET /` answered 404 in 0.14 s, but a `POST /v1/systemone` with a one-word state hung for 60 s,
  with the local model name and with `jev-1.13.0`.
- **Paid Jev (TypeSafe), 1 paid game: works.** Run 151 (`jev-council-m`, `ai-normal`, 10 minutes: a
  connectivity check, not a result): 52 requests, 53,855 input tokens ($0.0023), all answered,
  latency median 231 ms (p95 295 ms), a win at +1,200. `--think measured` is charged to the game
  clock: the seat made 59 batches in 10 minutes, against 101 for v10 at a fixed 5 ms (285 games).
  The measured think includes the bot's own Python time, so it is not only Jev's latency.
  Paid games used so far: 1 (run 151).

**The mechanics** (`jev-selltest`, 10-minute games against `ai-normal`, seed 77, trapped):
- Run 148 (`38df204f`): the inner bot deployed the new MCV as a yard after the sale, and the test
  sold that too: no building, no MCV, 109 units: defeat at 7:14.
- Run 149 (`2e110913`): no redeploying. At 7:14 sold 16 buildings: `deaths_cost` 0 before and after,
  cash 6,500, units 34 to 109, the MCV alone left; the game went on to 10:00 (draw against the AI).

**Where the army waits: probe on `@normal-fight`** (experiment `jev-stage`; 15 moments, 30 s before v10's first
4,000+ score fall against the Normal AI, 8:50-15:20; 180 s or 45 s quiet; run 170; the report drops
the rows because the family code changed after the run started, so the table is read with
`current=False`, see `lab/moments.py:outcomes`):

| bot | branches | score change since the moment | vs control (paired, 95% CI) | better in | W-D-L at the stop |
|---|---|---|---|---|---|
| control (v10) | 15 | -4,240 | | | 6-0-9 |
| `jev-base` (parity) | 15 | -4,240 | +0 (+0..+0) | 0/15 | 6-0-9 |
| `jev-st5` (5 cells) | 15 | +3,167 | **+7,407 (-1,654..+16,467)** | 11/15 | **14-0-1** |
| `jev-st8` (8 cells) | 15 | +583 | +4,823 (-2,528..+12,175) | 11/15 | 10-1-4 |

- **`jev-base` is v10**: the same score in all 15 branches (the copy and the new knobs at their defaults change
  nothing).
- **Waiting under the towers turns the fights**: the interval touches 0, but the sign test (11/15) and the
  stop result (14 of 15 at net >= 0, against 6) agree. Caveats: a branch, not a whole game; the regroup
  sends the army back at the start of the branch, which a game with the setting from the start does not need.
  Next: whole 20-minute games (run 178: `ai-normal` and `exploit-ryan-plan`, 16 games each).
- **But the probe overstates every change, and whole games show nothing.**
  - *Placebo* (run 185, same set and rule): `jev-plc` = `jev-base` with `regroup` only (idle units walk to v10's own
    staging point, which should change nothing) gains **+3,383** vs the control (-2,024..+8,791), better in
    10/15, W-D-L 12-0-3 against 6-0-9. The set is chosen by v10's bad fights, so the control replays a selected-bad
    outcome and any perturbation regresses toward the mean. `jev-st5` is about +4,000 over the placebo: not significant.
    **Every probe on selected moments needs a placebo variant, and the comparison is to it** (lab note 63).
  - *Whole games* (run 183, `jev-st5-v1`, 20 min, no superweapons, seed 5101, 16 games each vs `ai-normal` and
    `exploit-ryan-plan`): v10 10/16 (+6,828) and 14/16 (+17,556); `jev-st5-v1` 9/16 (+6,416) and 15/16 (+14,562);
    together 24/32 each, **+0.00 points a game (CI -0.20..+0.20), net -1,703 (CI -11,008..+7,602)**: no difference.
    Per pair the results flip both ways (chaos). The first big army loss (>= 6,000 in 30 s) comes at 13.6 min on
    average for v10 and 12.3 for `jev-st5-v1` (15 and 13 games of 16): staging near the towers does not move it.
  - *Why the late games are lost* (no superweapons now, so these are conventional): v10 leads or is level to
    ~15 min with 55 units (`30573cce`: e3 x24, 2tnk x17, e1 x12), the Allied Normal AI's wave (12 e3, 12 e1,
    a Tanya, a tank, a helicopter in sight) kills 54 units for 28 in 44 s at the staging point, and the base
    falls 16:19-17:35. Against the Allied Normal AIs **Tanya (`e7`) is seen in 25 of 25 v10 games, first at
    ~13.5 min, and v10 loses 13 of them** (Soviet Normal AIs have none). She does 35,714 DPS against infantry and
    nothing against vehicles (PakledAgent's table), and v10's army is 60% infantry.
- **Focus fire when defending** (`jev-focus`, run 180, same set and rule; `jev-base` again identical to v10 in all 15
  after the module was renamed to `hands.py` for freezing): +1,660 vs control (-4,351..+7,671), better in 6/15,
  W-D-L 7-0-8 against 6-0-9. Nothing; parked.
- **Distinct from rush-wave's idea 1** (anti-infantry units from 5:00, no effect against the Rush AI,
  runs 137/139): the riflemen test below is against other opponents (`exploit-ryan-plan`, `ext-ryan`, the
  eco exploiters) and changes what the *infantry* is, not an anti-infantry mix.

**Bank the lead, first probes** (moment sets `@jev-ls-wipes`: 11 of v10's late-wipe games at 10:00, to the
end of the game, and `@jev-ls-holds`: 24 games v10 did not lose by a wipe; experiment `jev-laststand`):

| run | bot | moments | what | result |
|---|---|---|---|---|
| 156 | `jev-ls1` (insurance 10:30, overrun sale) | 2 | smoke | no sale fired; the spare MCV alone kept the game alive to 20:00 (both lost on the score) |
| 161 | `jev-ls2h` (cash-out 18:00, army hides) | 4 | first cash-out | 1 win, 3 losses; found a bug: a probe branch starts at 10:00, so `spawn_home` (set only before 0:05) was never learned and nothing hid; the nuke of `38de5f1a` came at 17:46, before the 18:00 cash-out |
| 163 | `jev-ls3` (insurance 12:00, cash-out 16:00, army and harvesters to the MCV's corner) | 11 | the same, fixed | score change -500 against -13,123 for v10 (+12,623, CI -5,049..+30,295, better in 7/11) but **1 win in 11 against 3: ten wiped (v10: six)** |

- **Why the spare MCV died** (run 163, `events` of the branches): the AI found it 1.7-2.8 minutes after
  the cash-out (`28690cba` sold 16:00, MCV lost 17:41; `166949ad` 18:33; `258f03bd` 18:50). The AI's squads
  pick the nearest actor of ours *whatever it sees*: `HiddenUnderFog` is an `IDefaultVisibility`, and the
  AI's `IsNotHiddenUnit` only looks at `IVisibilityModifier`s (cloaks), so fog hides nothing from it.
  Two avoidable causes on top: the flee rule sent MCVs across the map through the enemy's side
  (`5f085d67`, `213f88ad`: lost at (61,28) and (36,52) on the way), and the army and the harvesters
  went to the MCV's corner (`9177b81e`: the MCV died in one burst with nine harvesters at 19:18).
  Fixed in `bots/jev/laststand.py`: no fleeing; each spare MCV has a place of its own; the army and the
  harvesters may go to other places (`stand_army`: stay / mcv / split).
- **A cash-out at 16:00 was early for a 1.7-2.8 minute hunt, late for some nukes.** First mass losses of
  v10's games against the AIs: 34 of its 117 decided 20-minute games (24 of the 34 lost) lose 6+ buildings
  within 5-16 s, first at 14.3 min; cumulative: by 17:00 4 games, 17:30 8, 18:00 11, 18:30 14, 19:00 24, 19:30 27.
- **Run 166** (`jev-ls4s/m/t`, no fleeing, cash-out 17:00): **stopped by hand after 5 of 33 games** when the work was
  parked. The first moment (`eb7dec67`, wiped at 19:44 in the original game): all three variants win at 20:00
  (+7,500 split, +15,450 with the MCV, +11,100 stay; v10's control is wiped). The second (`b58db8cf`): `ls4s` and
  `ls4m` lose at 18:39 (the MCV found, as in run 163). Nothing more can be said.
- The overrun sale (`sell_mode="overrun"`) never fired in run 163: the cash-out came first or its test
  (enemy value at home 3,000+ and 1.3 times ours, buildings falling) did not hold.

## Core changes


(Every change to shared code, rule 8: the file, what it adds, that it is off by default, its test.)

## Open questions and next steps
