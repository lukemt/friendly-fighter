# How the lab should evolve: bots, infrastructure, experiments

> Written 2026-10-15. A plan to work through and revise, not a record. The lab itself is described
> in [LAB.md](LAB.md); what we know about winning in [STRATEGY.md](STRATEGY.md); bots found online
> in [EXTERNAL_BOTS.md](EXTERNAL_BOTS.md); the long-term idea in [ROADMAP.md](ROADMAP.md).

## Where we stand

- **2,801 games** in `sim/lab/lab.db`, ~50 registered bots, 64 runs.
- **Champion:** `ff-council-champ-v10` (the rules council with early refineries, no API). Its
  unfrozen twin `ff-council-rules-eco` tops the ratings (1878 ±55, 92% over 128 games).
- **The live bot is `ff-council-champ-v10`** since 2026-10-15 23:50 (the user's go; `agent/champion.py`).
- **Real games are 20 minutes** (the platform's rules). Until run 114 every lab game was 10 minutes;
  the lab's default is 20 since 2026-10-16. Run 116 repeats run 64 at 20 minutes: the Rush AI wipes
  bases at 14-17 minutes, which 10-minute games never showed.

What `ff-council-rules-eco` scores, by opponent (all versions, run 63-64):

| Opponent | Games | W-D-L | Score | Net mean | Reading |
|---|---|---|---|---|---|
| ff-turtle, ff-guard-turtle, ff-default, counter, chaotic | 48 | 48-0-0 | 100% | | our own bots no longer teach us anything |
| exploit-eco-waves, exploit-eco-tanks | 32 | 32-0-0 | 100% | +10k, +4.5k | the exploiters are beaten |
| ai-normal | 16 | 13-1-2 | 84% | +3.0k | |
| ai-rush | 16 | 13-0-3 | 81% | +4.8k | wiped once |
| ai-turtle | 16 | 6-10-0 | **69%** | +1.6k | **10 draws at 0-0**: worth 0, replayed |

By map: shadowfiend-ii 88%, trapped 91%, sidestep 96%. By faction: France 80%, the others 91-100%.
**It wiped out an opponent in 0 of 128 games**: every win came on the tiebreak.

## What the numbers say

1. **The opponent pool is used up and was never the field.** Everything we built loses to v10
   100% of the time. The only platform games we have (4, imported) are self-play. The lab has
   never seen another contestant's bot. Contestant bots that play like the field now exist (see
   EXTERNAL_BOTS.md), so wiring them in is the most valuable step.
2. **We can't finish games.** No wipe-outs, and the Turtle AI holds us to 0-0 in most games. In
   the tournament a draw scores 0 and is replayed, and after three no-result games an admin
   decides. A bot that can break a turtle, or at least get one kill without losses, turns
   draws into points.
3. **What the decision layer adds is unmeasured.** The rules council equals the Jev council, and
   the Jev credits ran out. A Claude advisor gives an upper bound at no API cost.
4. **Games are cheap, but small differences need many.** At 75-90%, ±0.1 points a game takes
   ~50 paired seeds, and a 5% difference ~400. Branches at decision points, compared in pairs,
   measure decisions far more cheaply than whole games.

---

## Part 0: how experiments run (the funnel)

> Added 2026-10-16 with the user. The aim: as many ideas as we like, without paying for full runs.
> An idea starts with the cheapest test that could kill it, and moves up a stage only if it shows
> promise. Most ideas should die in stages 1-2; only a candidate champion pays for stage 4.

| Stage | What | Cost | Moves up when |
|---|---|---|---|
| 0. Idea card | one line: the situation, the change, how we would know (`lab experiment`) | nothing | it targets a weakness we have seen |
| 1. Moment probe | the variants branch from a set of saved moments for 3-5 minutes (`lab probe`) | a branch costs a game played to its moment (~10 s from 5:00, ~45 s from 17:00, until L14); minutes a probe | it beats the unchanged bot (the control) from the same moments |
| 2. Targeted screen | full games against the opponent that shows the weakness, paired seeds, stop when clear | 5-15 min | better than the champion there |
| 3. Fast suite | the 3 AIs and the 2 exploiters | ~10 min | no regressions |
| 4. Full gate | the complete compare (run 116's size) | ~1 h | champion, then the live bot (the user decides) |

**Saving games.**
- **Moments, not whole games.** A late-game idea is tested from minute 13, not from minute 0. An
  opening idea needs 10-minute games or less. Only stages 3-4 need full 20-minute games.
- **Reuse what was played (L11).** Lab games are deterministic: the same bot versions, map and
  seed play the same game, and a moment with the same bots plays out one way. The champion's games
  and the control branches never need replaying while their versions are unchanged.
- **Stop when clear (L12).** Paired games give small, stable differences: most comparisons are
  decided long before their last game.
- **Settings, not code.** Strategies as `Knobs` and interventions as small scripts (L13), so that a
  variant is a line of config and many can be screened on the same moments.
- **The live bot comes first.** Experiments run at low priority, with fewer workers while a live
  match is on.

**The ledger.** Each idea keeps its card, the stage it reached, the numbers and a verdict
(`lab experiment`, `lab note`), so dead ideas aren't retried and "promising in a probe" is never
mistaken for "proven".

**First use: the Rush AI's late push** (run 116: 15 wipes at 14-17 minutes). Moments at ~13:00 of
those games become `@late-rush`; 5-10 ideas are probed on them (defenses at the ore field, pull back
and mass at home, harvester escorts, a counter-attack into Rush's base during its push, an earlier
tech switch); the best 1-2 play 8 games against Rush, then the fast suite, then the gate.

---

## Part 1: lab infrastructure, in priority order

### L1. Contestant bots in the lab — built 2026-10-15 for starter-based bots

**Done for bots built on the AltruAgent starter** (`bots/external.py`, docs/LAB.md "Other
contestants' bots"; first: `ext-ryan`). Exploring changed the plan: their vendored runtime is the
same starter as ours (the same `runner.py`, `models.py`), so the lab plays them in-process with its
own `run_game`, deterministically, and branches and replays work.

- **Loading:** each seat gets its own copy of their code under an alias, with imports rewritten
  in memory.
- **Game clock:** their `time.monotonic()` reads the game clock, which a separate process would
  not fix either: their timers are wall-clock and lab games run ~50× faster.
- **Settings:** their environment is per seat.
- **No API:** `anthropic` is a stub.
- **Fetching:** `lab external fetch` clones the pinned commit into gitignored `sim/external/`.

**Still open:** bots with their own play loop (Khuongtdm) need a wrapper around their policy
function. A separate-process runtime is the last resort, and it needs real time or a clock shim too.

### L2. Register what costs nothing

- Done 2026-10-15: `ai-beginner`, `ai-easy`, `ai-medium`, and the suites `@field` (grows with each
  contestant bot) and `@ais-all`.
- Still to add: `@sparring` (henrysue's `rusher` and `yuriko`
  archetypes), `@gate` = `@standard` + `@exploiters` + `@field`.

### L3. Claude as the council's advisor (`oracle="claude"`)

**The desk exists** (`lab/advise.py`, docs/LAB.md "Claude as a bot's advisor"). It already advises
`ext-ryan-claude` through that bot's own advisor layers. The council's oracle becomes another
client of `Desk.ask(system, payload, schema)`. The council's questions are already JSON schemas
(Jev's typed questions), so `lab adv` serves both.


- **Setting:** a third oracle beside `jev` and `rules` in `bots/jev_council.py`.
- **When it calls:** the game pauses (virtual time, like `lab human`) and prints each loop's
  briefing, state and questions exactly as Jev gets them. `lab adv answer '{...}'` answers, and
  the game plays on.
- **Pacing:** Claude is called only on events the council already detects (a raid, a new enemy
  type, enemy buildings seen, an attack's result, a score swing) or about every 45 s. The rules
  answer the calls in between.
- **Logging:** each answer is saved as a labelled case in `lab/jev_cases.json` format. Each
  disagreement with the rules is saved as a branch point (L5).
- **Registered as** `ff-council-claude`.

### L4. A wider advisor interface

Ours chooses only a plan, a production mix and a posture. henrysue's advisor also sets
route, expansion, raid targets, turret side, the waiting army's stance and the guard. Add
outputs to the council, each one with rules-based defaults so the `rules` oracle still plays:
- `attack_target` (cell or building);
- `raid_target` (harvesters / a field / power);
- `expand` (yes/no, where);
- `defense_side`;
- `finish` (go for the remaining buildings).

This turns the "current intent" store of ROADMAP.md into code.

### L5. Measure decisions at the points where advisors disagree

- **What it does:** a command that takes the moments where two oracles disagreed (Claude vs
  rules, Jev vs rules) and branches both answers from that snapshot with `lab branch` against
  the same opponent. It scores net at +60/+120 s and at the end, over several seeds.
- **Output:** a per-decision verdict instead of a whole-game result, and a stream of labelled
  cases for improving `Rules`.
- **Builds on** `lab coach` (which forces postures) and `lab branch`.

### L6. Score like the tournament

- **Points:** a draw is replayed, so the value of a matchup is P(win) / (1 − P(draw)), capped
  by the 3-replay rule. Show that next to the score, and add a "wiped them" rate to the
  headline tables.
- **Simulated tournament:** a `lab tourney` command that runs Swiss rounds plus best-of-3
  knockouts from the head-to-head matrix and reports how often each bot wins the event.
  This is what we optimise for.

### L7. Learn the field from what is published

- **Contestant logs:** Khuongtdm's repo has 5 platform match logs (`ra_logs/*.jsonl`), and
  PakledAgent and henrysue describe opponents they met.
- **Archetype bots:** turn these into scripted bots ("yuriko": six riflemen at 0:32, war factory
  2:39, pillboxes 2:40/3:35, nonstop tanks from 4:05, push with a fixed tank count) built
  from our own `RedAlertBot` with knobs.
- **Opponent catalogue:** a short one in STRATEGY.md: what each known contestant opens with,
  when it attacks, and how it trades.

### L8. Robustness and live parity, before switching the live bot

- **Latency:** v10 at measured and doubled latency (`think`), and with refused orders injected.
- **Real time:** a real-time (not virtual) batch to confirm results match.
- **Platform:** one Testing match (self-play) with v10 on the platform, imported with
  `lab import` and compared with the simulator.

### L9. Housekeeping

- **Ratings:** keep the ad-hoc `knobs:...` entries from `lab exploit`/`compare` out of the main
  ratings, or group them, so the table reads as bots.
- **Dashboard:** add an "external" section and a per-opponent heat map of the champion.
- **External bots:** freeze them too (`lab bots freeze` records the commit).

### L10. Moment sets and probes (the funnel's stage 1) — built 2026-10-16

- `lab moments find NAME --kind wiped|swing|building-lost|at ...` collects moments of recorded
  games into a named set (the seat to branch, the time, why); `lab moments add/show/list/drop`.
- `lab probe NAME --variants A,B --stop-after 300` branches every moment of the set with each
  variant and the control (the bot that played there), and reports per variant the score change
  since the moment, the paired difference to the control with an interval, how many moments it
  did better in, and how often the seat was wiped. `lab probe NAME --report` reprints it.
- First real set: `@late-rush`, 13 moments (run 116: the Rush AI wiping a base after 10:00, 2 min
  before). The test probe (run 119, 2 moments) ran end to end.
- **Measured cost:** a branch first replays the game to the moment, and that is mostly engine
  ticks (17 min: ~26 s idle, ~21 s of it ticks; 44 s with 4 games running; 5 min: ~10 s). So a late
  branch costs about as much as a game played to that point: probes save by playing only the
  situations an idea is for and stopping early, and they are precise (paired at the same moments),
  but not cheap per branch.
- **Built on 2026-10-16 too:** `--halving` (successive halving, L12 for probes), `--stop-quiet S`
  (a branch ends once nothing died for S seconds), reuse of branches played the same way (L11 for
  probes), and merged replay steps (-13%; views checked at every step with orders).
- **Reuse verified:** 4 branches played again with `--no-reuse` (run 124) came out identical to
  the originals (score, end, timeline).
- **First halving probe** (`@late-rush`, 5 bots, runs 120-123): 50 branches in ~6.5 minutes instead
  of 78; the finding: 2 minutes before a wipe is too late (docs/STRATEGY.md). Ties are common
  (decided positions play identically), so halving on "unclear" differences drops at random: rank
  on enough moments before trusting a drop.
- **Measured and dropped:** replaying with OpenRA's AI asleep (as a save game loads,
  `ModularBot.cs`): the AI thinks ~1.2 of ~21 s in a 17-minute replay (`RL_BOT_TIMING=1`), ~6%.

### L11. Reuse games already played — built for probe branches 2026-10-16; whole games next

Before a run, skip every job whose game is already in the database with the same bot versions,
map, seed, length (and, for a branch, origin, tick and stop): the result is the one recorded.
Reports then draw on the old games. Needs one check first: that the same job does replay the same
game across lanes and engine builds (an engine change resets the cache).

### L12. Stop when clear — successive halving built for probes 2026-10-16; compare/gate next

`compare`/`gate`/`probe` schedule in rounds of paired games and stop a candidate once its
interval against the reference clears a margin either way (or a budget runs out).

### L13. Scripted interventions and carried memory in branches

- A variant can be "the bot plus an instruction from this moment" (build 2 pillboxes at the ore
  field, all-in at 15:00), so an idea is tested before it is built into a bot.
- A branched bot can start with the memory it had at that moment (rebuilt by replaying its own
  states), instead of a fresh one, so late-game probes don't play a bot that forgot the game.

### L14. Engine snapshots (makes late branches cheap)

Replay a moment once and branch every variant from the engine's saved world, instead of replaying
the game for each branch. OpenRA has no world copy; options are a save/restore of the world state in
the fork, or checkpointing the engine process (CRIU). Worth it once probes are the main workload:
a 13-moment, 5-variant late probe replays ~17 minutes 78 times today.

---

## Part 2: bots to add

| Group | Bot | Source | Why | Effort |
|---|---|---|---|---|
| Field proxies | `ext-ryan` | ryanxxhuang/altruagent-redalert, advisor off | rule-based waves, kiting, focus fire, retreat at 0.4×: close to a good contestant | L1 |
| | `ext-henry` | henrysue (`agent.dispatch`), `REDALERT_ADVISOR=off` | "hyper-aggressive", tuned for the tiebreak: tests our defence against an agent that attacks well | L1 |
| | `ext-henry-rusher`, `ext-henry-yuriko` | henrysue's `sparring.py` archetypes | copies of real tournament opponents | L1 + a setting |
| | `ext-pakled` | SirLinhart/PakledAgent, models off | the biggest scripted bot found: expansion, raids, siege, rush counters | L1 |
| | `ext-khuong`, `ext-khuong-aggro` | Khuongtdm, default and an aggressive `params.json` | a parameterised policy: easy variants | L1 + wrapper |
| | `ext-riffo` | lawrencedz/Riffo-Agent, fallback only | an LLM-style bot without its LLM: weak but typical | L1 |
| Engine AIs | `ai-beginner`, `ai-easy`, `ai-medium` | our fork | weak baselines: do we win big? | L2 |
| | FransBots (six) | ModDB | stronger in-engine AIs | port + corpus check |
| Our challengers | `ff-council-claude` | L3 | the advisor's upper bound | L3 |
| | `ff-council-finisher` | v10 + a late "finish" rule | turns turtle draws into wins | small |
| | `ff-council-read` | v10 + opening chosen by the opponent read at ~2:30 | STRATEGY.md's open "next" | small |
| | `ff-council-wide` | v10 + L4 outputs answered by rules | does the wider interface help even without a model? | L4 |
| Exploiters | `exploit-v10-*` | `lab exploit --target ff-council-champ-v10` | the old exploiters are beaten; find new holes | a run |
| Reference | OpenRA-RL `scripted_bot` | OpenRA-RL examples | the engine authors' reference play | shim |

Retire from the regular gates (keep for regression checks): `ff-rush`, `ff-eco`, `ff-harass`,
`ff-tanks`, `ff-patient`, the `ff-jev-force-*` coaches, and the Jev bots while there are no
credits.

---

## Part 3: experiments, in priority order

Each experiment: the question, the design, the size, and what decides it. Default design:
paired seeds, maps rotated, seats swapped, a second seed before anything is crowned (STRATEGY.md:
"one seed fooled us").

| # | Question | Design | Size | Decides |
|---|---|---|---|---|
| E1 | **How does v10 do against the field proxies?** | `lab league ff-council-champ-v10 @field` (+ `ff-default` for reference) | 16 games per pair | the new baseline; any proxy under 70% becomes the next target |
| E2 | **Where does v10 break?** | `lab exploit --target ff-council-champ-v10` over the knob space, then confirm the best 3 on a fresh seed | ~200 games | new exploiters for `@exploiters` |
| E3 | **Can we turn Turtle AI draws into wins?** | branch all 10 draws at 6:00/7:30/9:00 with variants: siege push with V2/artillery behind a screen, harvester raid, AA + bait, "one kill and go home" | 10 games × 3 moments × 4 variants × 2 seeds | the best variant becomes `ff-council-finisher`; gate it so it doesn't lose ground elsewhere |
| E4 | **Does the opening by opponent pay?** | `ff-council-read` (guard only if a rush shows by 2:30, else greedy) vs v10 on `@gate` | 2 seeds × 50 pairs | crown if better on both seeds |
| E5 | **How much can an advisor add?** (Claude as the ceiling). *First look, runs 75-77: Claude advising `ext-ryan` went 2/3 against v10, against 0/3 unadvised on the same seeds (docs/STRATEGY.md).* | `ff-council-claude` plays 3 games each vs ai-rush, ai-turtle and the weakest field proxy; then L5 branches at every disagreement | ~9 games + ~100 branches | if Claude's calls win the branches: encode the patterns into `Rules`; if not: the advisor layer is not our bottleneck |
| E6 | **Do wider advisor outputs help?** | `ff-council-wide` (rules) vs v10, then with Claude answering | as E4 | keep L4 if either wins |
| E7 | **Why France and shadowfiend-ii?** | `lab stats --by faction/map` on the losses and draws, `lab watch` the worst 5, `lab curves` by faction | analysis + targeted branches | a fix or a note in STRATEGY.md |
| E8 | **How much greed against agents?** | the assets tiebreak decides agent mirrors: `greed` 0/1/2/3 × `early_refineries` 1/2 vs `@field` | grid, 8 games per cell | settings for the field |
| E9 | **Is v10 robust?** (L8) | latency ×1/×2/×4, refused orders, a real-time batch, one platform Testing match | ~60 games | needed before the live switch |
| E10 | **Switch the live bot?** | gate v10 vs `ff-default` on `@gate` | already clear on `@standard`; confirm on `@field` | user decides |

Not worth the games now (STRATEGY.md): more defenses (no effect, run 56), scouting with infantry
(costs standoffs), Jev experiments until there are credits.

---

## Order of work

0. **2026-10-16 (the funnel, Part 0):** L10 (moments + probe, built), then L11 (reuse), L12 (stop
   when clear), L13 (interventions, memory), L14 (engine snapshots). First probe: `@late-rush`.
1. **2026-10-15:**
   - L2 (built-in AIs, suites).
   - L1 for `ext-ryan`, the drop-in one.
   - E1 on what's wired, and E2 running in the background.
2. **Next:**
   - L1 for henrysue (+ sparring), PakledAgent and Khuongtdm.
   - E1 complete.
   - E3 (draws into wins).
3. **Then:** L3 + L5 (Claude advisor and disagreement branches) and E5.
4. **Then:** E4, L4/E6, E8 as the field results point.
5. **Before going live:** E9 and E10. The live switch is the user's decision.

Open questions for the user:
- When does the tournament start?
- Is updating the live bot after the deadline allowed? This is still unconfirmed; check the
  Official Rules.
- Should the repo stay public, now that contestants can read our strategy?
