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
| 1a. Manual play | try the tactic by hand from the set's moments (`lab human start --moment`), paired with the control in the probe report | almost no CPU (the game waits) | it beats the control by hand: write it as a setting and probe it |
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

### L11. Reuse games already played — built for probe branches and the board 2026-10-16 (whole games verified deterministic, runs 126-127); compare/gate next

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

### L15. The board — built 2026-10-16

`lab board` (lab/board.py, lab/board.json): every family's frozen entries against the champion on
one fixed schedule (6 baselines × 8 games, 20 min), played from each family's worktree into the
shared lab, whole games reused when played the same way (verified, runs 126-127), standings paired
with the reference, snapshots over time. The coordinator runs it (AGENTS.md, duty 9).

### L16. Decide with fewer games (the user's question, 2026-10-16: "test hypotheses and find the next champion in the fastest, most resource-efficient way")

**Where the machine goes** (measured 2026-10-16, experiment `lab-speed`):
- **The slots are the limit.** Since 06:00 all 5 are busy (4.9 of 5 an hour), the CPUs too (load
  9.3 on 6). More workers would not help: only fewer games per decision, and cheaper games.
- **A 20-minute game takes ~60 s** of a slot (15x real time); with ext-henry ~206 s (its Python;
  ext-henry-rusher 130 s, yuriko 143 s). A branch: ~34 s.
- **A third of the 20-minute games played in a day were the champion's** (567 of 1,647), mostly
  on seeds no other run used: each family's own `--seed`, and compare's seeds depended on the
  baseline's place in `--baselines`.
- **The noise:** v10 and a candidate on the same game differ in points with sd 0.52 (994 pairs);
  73% of pairs are equal. A 95% interval of +-0.1 points a game needs ~100 pairs, +-0.05 ~400.
  The board (48) sees +-0.15. The improvements we find are 0.05-0.2 (rush-wave-k-v1 +0.07,
  jev-st5-v1 +0.10, jev-rifr-v1 +0.19).
- **Games that tell nothing:** against ext-henry-rusher, ext-henry-yuriko, chaotic, counter (100%
  of pairs equal), ff-turtle (94%), ext-ryan (83%) every good bot wins.
- **Net (kills minus losses) is a slightly sharper measure than points**: its t-value was higher
  for 5 of 7 candidates (median x1.17, ~1.4x fewer games for the same certainty), and it predicts
  the points on held-out games as well (r 0.92 vs 0.93). Points stay the decision (the tournament
  counts wins); net can screen.

**Built (2026-10-16):**
1. **The same seeds everywhere** (`league.game_seed`): the champion's game against an opponent on a
   seed is played once, then reused by every compare, gate and the board. Screen on seed 2026 (the
   board's): the reference's games are already there, so a compare plays only the candidate's. With
   one candidate that halves the games.
2. **`lab compare --sprt`** (lab/sprt.py): the sequential test chess engines are tested with
   (Fishtest, OpenBench). Simulated on our pairs: a +0.1 candidate called better 78% of the time
   (a fixed 56-pair compare: 31%), a no-gain one 4%; clearly worse ones stop after 20-50 pairs.
   `--games` is the cap.
3. **`@screen`** (the opponents where bots differ) and **`@smoke`** (2 games each where they
   don't: catches breakage).

**The funnel with these** (stages 2-3 of Part 0): `lab compare ff-council-champ-v10,X --baselines
@screen --seed 2026 --games 40 --sprt`, plus `--baselines @smoke --games 2`. A BETTER goes to the gate on
a fresh seed (the holdout: many families screening on one seed will find its lucky games, and
a fresh seed doesn't share them).

**Next, by expected gain:**
- **`--sprt` for gates** (the h2h and the baselines as one test; non-regression bounds
  `-0.05,0` for a change aimed at one opponent: "no worse elsewhere", Fishtest's second test).
- **Engine snapshots** (L14): a late-game idea (the Tanya, the last stand, salvage) branches at
  15:00 from saved states instead of replaying 15 minutes (~2/3 of a branch's time).
- **Profile ext-henry's 206 s** (our wrapper or theirs: `bots/external.py` copies, logs, clocks).
  It is the opponent we most need to play.
- **Tune numbers with SPSA** (Fishtest's tuner): for a knob bot, nudge all settings at once by
  random +-steps, 2 games per step, move towards the better side. Many settings in the games one
  hand-made variant takes now.
- **A screen on net**: `--sprt` on net (in thousands) for stage 2, points for the gate.

---

## Part 1b: other architectures (the user's idea, 2026-10-16)

Every bot of ours so far is one architecture: the scripted bot (`agent/redalert_bot.py`, `Knobs`)
under the council's three loops (strategist, quartermaster, field commander), answered by code
rules (v10) or Jev. The families' leads are v10 plus one setting. What else has won elsewhere:
StarCraft bots (UAlbertaBot, Steamhammer, CherryPi) are **modular** (separate managers for
economy, production, army, scouting) and pick from a **portfolio of strategies** by reading the
opponent, some with an **opponent model** kept across games. In microRTS, scripted bots won for
years; deep reinforcement learning won only lately, with far more games than this machine plays
(~2,000 a day). The strongest contestant here, ext-henry, is modular (brain, commander,
executor, squads, an economy schedule, per-opponent profiles in `opponents.py`).

| # | Architecture | Why here (evidence) | Cost | First test |
|---|---|---|---|---|
| A1 | **A portfolio and a selector:** several whole strategies (v10; anti-rush with `defend_radius` 22; macro with henry-style refineries; a timing attack), and a selector that reads the opponent from what was scouted by 3:00-6:00 and switches | Different leads win against different opponents (rush-wave-k-v1 Rush 5/8 vs v10 3/8; jev-a1m8 7/7 vs ext-henry, v10 4/8), and v10 commits to one plan: after one raid it stays on hold for the game (the jev family, run 195) | low-medium: the strategies are settings of the same core | each strategy alone vs `@screen` (which wins where), then the selector vs v10 and vs each strategy alone, `--sprt` |
| A2 | **An opponent model across matches:** the platform names the opponent at the start (`OfficialAssignment.opponents`; imported games show "Yuriko Red", "Cryosis"). Remember what each did (rush time, army mix, expansions) and open with its counter | Tournament opponents repeat; henry's bot already keeps profiles | low, on top of A1 | lab: a "memory" from earlier lab games against the same bot, vs no memory |
| A3 | **Modular managers** (henry's and UAlbertaBot's shape): an economy manager with a refinery and harvester schedule, production, an army manager with squads (main army, home guard, raiders), scouting; each with its own rules | ext-henry beats v10 5-1 by out-building it (assets 85k vs 57k at 17:54) and its last stand; v10's army is one group that scatters (the regroup lead, run 187) | high: a new bot, days | its first manager alone (the economy) inside v10, then more |
| A4 | **A learned evaluation:** a model trained on the lab's ~5,700 games (timeline every 5 s: armies, economy, kills, losses, both seats) that predicts the result from the state at any time | Lets a bot choose between options by the predicted result (attack or hold, expand or not), and lets the lab end games already decided (a 20-minute game is ~60 s of a slot) | low-medium: gradient boosting on what the database has | how well minute 10-12 predicts the end (held-out games), then one decision (posture) by the model, probed |
| A5 | **Decisions learned from branches** (search offline, rules online): at sampled moments play each option for 60-180 s (the probes do this by hand), label the best, train a small classifier for that decision | The platform gives no forward model live, but the lab's engine is deterministic and branches are cheap (~34 s) | medium, and CPU: ~N options x branches per label | one decision (attack now / wait) at unselected moments |
| A6 | Deep RL, one network for everything | microRTS: won only with very many games | not here | no |

**A1, first look** (2026-10-16, from games already played, paired with v10): the best existing bot per opponent gains +0.36 (jev-rifr-v1 vs ai-normal), +0.29 (jev-rifr-v1 vs ai-rush), +0.50 (jev-a1m8 vs ext-henry), nothing elsewhere; an always-right selector's ceiling is ~+0.19 points a game on @screen (optimistic: the best of ~10 on small samples). The winners are one lineage, so a combined bot may take most of it; how jev-a1m8 does against the AIs decides how much room a selector has.

**A4, first look** (2026-10-16, experiment `arch-value-model`; logistic regression in numpy on
the differences between the seats: net, army value, assets, harvesters, buildings, combat units).
Only ~630 whole 20-minute games have both seats' timeline (the lab records it for agent seats,
not for OpenRA AIs), 189 held out:

| state at | accuracy | sign of net alone | 97%+ sure | of those right |
|---|---|---|---|---|
| 6:00 | 86.8% | 83.7% | 24% | 100% |
| 10:00 | 91.5% | 86.0% | 53% | 97.0% |
| 12:00 | 94.7% | 86.0% | 63% | 97.5% |
| 15:00 | 95.8% | 87.8% | 66% | 98.4% |

Army value and assets count beside the score. For the lab, ending a game the model is 97% sure of
at 12:00 would save about a quarter of a whole game's time; but the 2-3% it calls wrong are the
comebacks (ext-henry's 18:00 last stand), which late-game ideas are about. So: opt-in for screens
of early ideas only, never for gates. Next: record AI seats' timeline too, more games, then one
bot decision by it.

**Order:** A1 with A2 first (cheapest, uses what we have, proven in StarCraft); A4 alongside (a
tool for the selector and for the lab's speed); A3 if A1 shows that the economy and army
structure, not the plan, is what loses to ext-henry; A5 for the decisions the others leave open.
Each is a family's brief (AGENTS.md: two families never chase the same idea). The machine's 5
slots are the limit: a fourth family shares them with the other three (first come, first served).

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
| E11 | **Does a controlled Tanya pay?** (the user's idea, 2026-10-16; experiment `tanya-sniper`, details below) | a Tanya setting on v10's rules when Allied: build her, keep her behind the army, shoot infantry from range, pull her back from vehicles and turrets | 1a by hand vs ai-rush and ext-henry; 1 on unselected moments (`--kind at`, Allied seats only); 2-3 whole games on 2 seeds | keep it if net gains on Allied seats with no loss elsewhere |
| E10 | **Switch the live bot?** | gate v10 vs `ff-default` on `@gate` | already clear on `@standard`; confirm on `@field` | user decides |

### Jev experiments, with the budget (2026-10-16)

The user allows paid Jev at $1 an hour (AGENTS.md rule 11): ~$0.005 a Jev seat in a 20-minute
game, so the money no longer limits these. What does: one paid run per family at a time, and
`think: measured` (Jev's ~230 ms a call is charged to the game clock, as on the platform). In
order:

| # | Question | Design | Cost |
|---|---|---|---|
| J1 | **Does Jev answering beat the rules on the same loops?** The question v10 left open (Jev council = v7 = the rules council, runs 59-60, before the economy fix). | `ff-council-champ-v10-jev` (v10's frozen code, `oracle: jev`) vs `ff-council-champ-v10`: `lab compare ff-council-champ-v10,ff-council-champ-v10-jev --baselines @ais,@exploiters,ext-ryan,ext-henry --games 8`, 2 seeds | 64 Jev seats a seed, ~$0.35 |
| J2 | **One decision at a time:** where does Jev beat a rule? | the jev family's split bots: `jev-sjj` (Jev decides the quartermaster) vs `jev-sjs` (shadow: the rules decide, Jev's answer logged) vs `jev-rifr`; the shadow logs give the disagreements | as J1 |
| J3 | **The decisions the rules get wrong:** boom or hold after a raid (v10 locks greed 0 after one raid: the jev family, run 195), and hold or fight when outnumbered in the last 3 minutes (ext-henry's last stand at 18:00) | a Jev question for each, against the rule and the jev family's coded fixes (`greed_hold`, `macro`); probe at unselected moments, then whole games vs ext-henry and @ais | a probe ~$0.05; whole games as J1 |
| J4 | **At the disagreements:** branch every point where Jev's shadow answer differs from the rule (L5), play both | `jev-sjs` logs, `lab branch` | ~100 short branches, <$0.10 |

J1 decides whether the live bot should ask Jev at all: if v10-jev wins on 2 seeds (and `lab jev`
shows answers in every game, none frozen), it goes to a gate, and the switch is the user's call.

### E11 in detail: a controlled Tanya

**Idea (the user's):** Tanya under deliberate control instead of not at all. Our bots have never
built her: v10 is Allied in ~half its lab games (england/france/germany 489 of 986 seats) but
never builds a tech center (the `atek` events of v10's games are all the enemy's, spotted).

**What she can and can't do here** (`~/openra-sim/fork/mods/ra/rules/infantry.yaml` `E7`, weapons
`smallcaliber.yaml` `Colt45`, the API's attack in `OpenRA.Mods.Common/Traits/Player/ActionHandler.cs`):
- **Allies only.** Needs a barracks and a tech center (`atek`, 1500, after war factory and radar
  dome). She costs 1800, one at a time (`BuildLimit: 1`), 100 HP.
- **No C4 through the API.** Demolishing a building is the engine's `C4` order; our `attack` sends
  `Attack`, which needs a weapon that can hit the target, and her pistol can't hit buildings
  (`ValidTargets: Ground, Infantry, Barrel`), so it is refused. Like capture and support powers,
  the platform offers no way to it. The OpenRA AIs' Tanya can C4 our buildings in the lab; no
  tournament opponent's can.
- **A sniper against infantry:** one shot kills any infantry (damage 10000 vs riflemen 5000 HP,
  rocket soldiers 4500), every 7 ticks (~3.5 kills a second), range 7 cells (riflemen and rocket
  soldiers shoot from less). Speed 68 against 54 for other infantry: she can back off from
  infantry. She does nothing against vehicles or buildings and dies to tanks, dogs and turrets.
- She attacks infantry in range by herself (auto-target); control adds where she stands and
  when she runs.

**Why it could matter:** the attacks that break us are infantry-heavy. ext-henry's last stand
at 18:00 is mostly riflemen and rocket soldiers (its losses per game, run 195: 65 riflemen, 28
rocket soldiers); the Rush AI's waves are ~30 riflemen and rocket soldiers each. 20 infantry
killed is ~2,000-6,000 of tiebreak for an 1,800 unit.

**Design (a setting on v10's rules, off by default, Allied seats only):**
1. Tech: a tech center once the economy allows (after the 2nd refinery, war factory and dome).
2. Tanya: build her once, rebuild when she dies (cap the rebuilds: 1800 each).
3. Position: 3-5 cells behind our army's front, or at home with the defenders when an attack is
   on its way (the last stand, a Rush wave).
4. Targeting: the nearest enemy infantry inside 7 cells; never walk into vehicles, dogs or
   turrets: when one is within ~5 cells, move her back towards our army or turrets.
5. Measure: her kills and losses by value (`lab games`/`curves`), and the seat's net against the
   same seeds without her.

**How to test (the funnel):** 1a by hand first, cheap: `lab human start --moment ID` on an Allied
seat against ai-rush and ext-henry (macro autopilot, Tanya by hand) to see whether kiting her is
possible at the API's order rate. Then a probe on unselected moments (`--kind at`, Allied seats,
ai-rush, ai-normal, ext-henry; the placebo bot alongside), then whole games on 2 seeds.
**Open questions:** whether the tech center (1500) costs us more than she gains before 10:00;
whether a seat's decision rate is fast enough to keep her out of reach of jeeps and dogs.

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
