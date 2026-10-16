# bots/hok: friendly-fighter's Honor of Kings 5v5 bot

The platform's `honor_of_kings_5v5`: a real-time 5v5 MOBA where one agent commands all five heroes
of a camp (seats 0-4 or 5-9). Each hero gets one action per 0.2 s step; a **macro** (`macro_advance`,
`macro_retreat`, `macro_attack`, `macro_skill1..3`) becomes that hero's **standing order** and keeps
playing until it gets another. The crystal wins; at the step limit (3000 steps by default) or time
limit, the tiebreak is damage to enemy towers and crystal, then hero kills. Contract:
`https://api.altruagent-game.com/skill/hok`, upstream's `GAMES.md` section and `tests/test_hok.py`.

Built before any live payload existed (online test matches had closed), from the published
contract and upstream's fixtures, checked on a mock platform and an abstract sim. Release 1
(`195e798`) then played the first live Testing matches the same evening and won its first game
(crystal destroyed); its recording corrected the map, the hold button and the healing model
(see "What the first live games showed"). Release 2 is built on that; release 3 turns off the heal
skill, which the release-2 games showed doing nothing.

## The idea

A scripted team controller in the spirit of Red Alert's `agent/redalert_bot.py`: it answers every
view fast from fixed rules and never waits on a model. The macros already aim (at the *nearest*
target) and walk the hero, so the policy decides each hero's **intent** and leaves the aiming to the
server:

1. **Escape when low and threatened, then go back to work.** Below 25% HP with an enemy hero in
   range (or taking an enemy tower's fire), a hero walks back until it has been out of danger for
   3 s. It does not walk home to heal: live, home heals ~0.25% a second, so that took minutes,
   while a death costs a 5-25 s revive at full HP.
   **Evade** for 2 s when a hero below 60% would die within 2 s at its current damage rate near an
   enemy hero or tower (the dive and burst detector, scale-free, from the HP history).
2. **Fight together, not one by one.** A hero engages visible enemy heroes only when our HP-weighted
   strength around it is at least theirs (enemies fogged in the last 4 s count half). Outnumbered
   heroes back off, unless they stand under our own tower or crystal.
3. **Skills on heroes first.** A skill whose label names an enemy hero (or whose distance matches a
   visible enemy hero's, when the label is opaque) is cast first, the ultimate included; then the
   basic attack. Skills go on minion waves only while no enemy hero is near.
4. **No dives.** Never chase an enemy hero under its tower without the team; never stand in an
   enemy tower's range with fewer than 2 healthy heroes there (unless the tower is under 15%, or
   the enemy team is mostly dead), and wait at most 15 s for them.
5. **Defend** a structure of ours that enemy heroes are hitting: every hero with 45%+ HP within
   40k goes back and fights there (minions alone are left to our minions).
6. **Push together.** A hero far ahead of the team's median waits (the do-nothing button, live
   `button_1` "NoOp") for up to 15 s; otherwise farm what is in range, then advance. After a won
   fight (3+ enemy heroes dead, from our kill count and enemies vanishing at low HP) nobody waits.
7. **Play for the tiebreak** in the last 60 s: ahead on structure damage (by half a tower), pull
   back and protect it; level or behind, all in (retreat only below 12%, no waiting).

## Files

| File | What |
|---|---|
| `view.py` | Reads a state into a `TeamView`: our heroes, enemies (fog), structures, offers per seat, labels (`nearest: <what>, <d> away`), limits. Never raises on missing or mistyped fields. |
| `policy.py` | `TeamPolicy` and its `Knobs`: one `Choice` (macro, reason, priority, alternatives) per hero. Memory: HP history, retreat state, structure damage (a structure that leaves the list at ≤25% HP counts as destroyed). |
| `agent.py` | `HokAgent`: the runner's hooks, real time vs lockstep, sending only changes, refusals, pacing, logging, the safety layer. |
| `sim/engine.py` | An abstract 5v5 MOBA (not the real game) for relative comparisons; `map="live"` (default) is the one-lane map seen live, with its buttons and labels. |
| `sim/match.py`, `sim/compare.py` | Two agents through the platform's interface on the sim (live latency 0.2 s per order); the comparison CLI. |
| `sim/baselines.py` | `button0` (the placeholder), `advance`, `upstream` (the starter's rules), `ff-hok[@pace][:{knobs}]`. |
| `sim/trace.py`, `sim/sweep.sh`, `sim/h2h.sh` | One game with hero positions and where the time went; knob sweeps; variants head to head. |
| `harness/mock_driver.py` | Our agent through the real runner (`run_game`) against a mock platform, with chaos. |
| `harness/replay.py` | A live recording: schema report, game summary, replay through the bot. |
| `tests/data/hok_live_states.json` | Two states from the first live game (ids scrubbed), as fixtures. |

## The runtime contract, as implemented

- **One hero per `choose_action` call**; the runner calls again at once for the next one.
- **Real time** (`pacing.mode == "realtime"`): the team is planned on every state, and only
  *changes* are sent: a hero whose standing order already matches gets nothing; an order sent within
  1.5 s isn't resent while the server's report catches up. The most urgent change goes first
  (retreat, then skills on heroes, then attacks, then holds and advances). Nothing to change:
  `WAIT`, and idle looks are paced to one per 0.3 s (`FF_HOK_PACE_S`), instead of one per step.
- **Lockstep**: every pending hero is answered at once with its planned macro, even when it's the
  standing order; a dead hero (no macros) gets the do-nothing button (found by its label: live
  `button_1` "NoOp"), or any listed button with its first legal values. A seat is never answered
  twice in one step.
- **Raw buttons**: only the NoOp (to hold), found by its label and sent only when listed. The
  HealSkill (`button_11`) is behind `heal_skill` (off: live it didn't heal); when on, its fields are
  the middle `legal_offset_*` values and `legal_target` 0 when listed. A raw action clears the
  standing order, so the hero's macro is sent again on the next step.
- **Safety layer**: every answer is checked against the state's legal actions before it leaves: a
  macro listed for that pending seat (`{"macro", "seat"}` only), a listed `action_id`, or a raw
  button whose every field comes from its `legal_*` list. An exception or an unlisted answer falls
  back to a listed macro of the first pending hero (attack, else advance, else its first entry), is
  logged and counted. `on_action_result` never raises.
- **Refusals**: a refused order rests 1 s (real time) or for the step (lockstep); the hero's next
  choice is used. If `{"macro", "seat"}` dicts are refused 3 times with none ever accepted, the agent
  switches to sending the listed `action_id` (the guide's other documented form).
- **Phases** `waiting_for_players` / `queued` / `starting`: nothing is listed, so `WAIT`.
- **Fog** (`visible: false`, no position), **dead heroes** (`hp` 0 / `revive_time`), **missing
  fields**: handled in `view.py`; a hero with no view at all gets attack/advance.

### Core changes (outside `bots/hok`)

- `agent/agent.py`: HoK is checked **first** in `_choose` (by `game_type`, or by the state's shape
  when the game type is missing), so a `game_type None` real-time HoK state can never reach the Red
  Alert bot. Red Alert's own path is unchanged (`context.game_type == "red_alert"` skips the check).
  `on_action_result` goes to the HoK bot when it plays. The recorder names the bot `ff-hok`.
- `altruagent/runner.py`: `_TEAM_GAMES = {"honor_of_kings_5v5"}`. A team game's **lockstep** states
  get the real-time handling too: `WAIT` waits (the server plays standing orders after its timeout)
  instead of raising `DecisionError`, and `INVALID_ACTION` / `RUNTIME_TEMPORARILY_UNAVAILABLE` are
  reported and re-read instead of ending the match. Without it, one refused hero order in lockstep
  would kill the worker, and the supervisor's cooldown would leave the team without new orders for a
  minute. Every other game behaves exactly as before (tested).
- Upstream's HoK update (altruagent-starter 824e8fe): `examples/llm/hok.py`, `tests/test_hok.py`,
  the `GAMES.md` section, `examples/llm_agent.py` wiring. Cherry-picked unchanged.

## Borrowed from Red Alert

- `agent/redalert_bot.py`'s shape: a scripted controller that answers every view at once with
  standing behaviour, remembers what it sent so it doesn't spam duplicates while the server's view
  lags, and cools down refused orders instead of resending them every tick.
- The tiebreak play: Red Alert stops attacking when ahead late; here, ahead on structure damage in
  the last minute, the team protects instead of pushing.
- STRATEGY.md's "attacking into defenses loses" and "a unit that dies costs its full price": the
  no-dive and evade rules.
- The lab's discipline for comparisons: paired seeds, both sides, a second seed range before calling
  anything better ("one seed fooled us"), and the same scripted opponents every time.
- `agent/commander.py`'s pattern for the optional planner (below): slow, in a background thread,
  only setting knobs.

## Results

### Tests (in the container)

- `tests/test_hok_bot.py`: 61 tests: upstream's fixtures and variants (fog, dead heroes, missing and
  mistyped fields, garbage, lockstep and real time, unlisted macros, refusals, the step and time
  limits, routing, the runner's team-game handling), two scrubbed **live** states
  (`tests/data/hok_live_states.json`), the mock driver and the sim. `tests/test_hok.py` (upstream's,
  16) passes unchanged. Full suite: 1098 passed at release 1, 1108 passed at release 3 (4 skipped).

### Mock driver (the real runner against a mock platform)

`python -m bots.hok.harness.mock_driver --steps 3000 --opponent upstream`: both camps x both pacings
x chaos 0 and 0.05 (fields dropped or mistyped, labels without distances, `legal_actions` left out,
`INVALID_ACTION` races, `STALE_STATE`, `RUNTIME_TEMPORARILY_UNAVAILABLE`, a match that starts queued).

| Code | Matches | Steps | Crashes | Illegal answers | Decision time |
|---|---|---|---|---|---|
| Release 1 (`195e798`) | 8 | 24,000 | 0 | 0 | 1.0 ms average, 22.8 ms max |
| Release 2 (`05e5c77`) | 8 | 22,915 | 0 | 0 | 2.6 ms average, 199 ms max (2 CPUs shared with a sim run) |
| Release 3 (1500-step matches, live latency 0.2 s) | 8 | 12,000 | 0 | 0 | 1.25 ms average, 187 ms max |

Plus the **live game replayed** through release 2 (`harness/replay.py`): 2,202 real states, 0 illegal
answers, 0 exceptions.

### Sim (relative only: an abstract model, not the real game)

Score = A's points per game (win 1, draw 0.5); every seed played from both sides; 95% CI over seed
pairs. Opponents: `upstream` (the starter's rules), `button0` (the placeholder), `advance`.

| What | Map | Opponent, pacing | Games | W-D-L | Score |
|---|---|---|---|---|---|
| Release 1 policy, seeds 2026-2029 | 3-lane guess | upstream, real time | 8 | 5-0-3 | 0.62 |
| ...with the tower-wait deadlock fixed, seeds 2026-2031 | 3-lane | upstream, real time / lockstep | 12 / 12 | 8-0-4 / 12-0-0 | 0.67 / 1.00 |
| Sweep winner (min_pushers 2, no tower step-out, return at 60%), seeds 2026-2035 | 3-lane | upstream, real time | 20 | 19-0-1 | 0.95 |
| Release 2, seeds 2026-2035 | **live** | upstream, real time | 20 | 20-0-0 (10 crystal kills) | 1.00 |
| ...same, but walking home to heal (release 1's retreat style) | live | upstream, real time | 20 | 7-0-13 | 0.35 |
| Release 2, **fresh seeds 5000-5009** | live | upstream, real time / lockstep | 20 / 20 | 20-0-0 / 20-0-0 | 1.00 / 1.00 |
| Release 2, fresh seeds 5000-5009 | live | button0, real time / lockstep | 20 / 20 | 20-0-0 / 20-0-0 (all crystal kills) | 1.00 / 1.00 |

Head to head against release 2 itself (live map, live latency 0.2 s, both sides; a variant must
win a second seed range before it ships):

| Variant | Seeds 2026-2035, real time | Seeds 5000-5009, real time / lockstep | Verdict |
|---|---|---|---|
| Skills on minion waves (`skills_on_minions`) | 14-0-6 (0.70, CI 0.54-0.86) | 12-0-8 (0.60, CI 0.47-0.73) / 10-0-10 | not confirmed, off |
| Skills on towers (`skills_on_towers`) | 12-0-8 (0.60, CI 0.40-0.80) | - | off |
| Both | 12-0-8 (0.60) | - | off |
| Faster looks (pace 0.15 s or 0) | 8-0-12 (0.40, CI 0.27-0.53) | - | keep 0.3 s |

Read with care: the starter's rules are a weak opponent in this sim (they walk into our team one by
one), so "beats upstream" is a floor, not a strength estimate. Self-play head-to-heads between
variants are the finer measure (`sim/h2h.sh`).

### Live

| Game | Release | Opponent | Result |
|---|---|---|---|
| `128fe39b` | 1 | self-play | no result: `SESSION_NOT_FOUND` before the start (platform) |
| `e5f159ed` | 1 | warren-test | **won, crystal destroyed** (31 kills, 10 deaths); most of the middle spent walking home (the hold bug and heal-at-home) |
| `1a24fdbb` | 1 | Quicksilver | no result: `engine_failed` (platform) |
| (start) | 1 | Quicksilver | no result: `start_failed` (control-plane timeouts) |
| `12bdf5a6` | 2 | self-play | one seat won, one lost (crystal destroyed) |
| `ba540e6e` | 2 | Quicksilver | **won, crystal destroyed** (outer tower ~173 s, inner ~220 s, crystal after ~300 s) |
| (adf698cb seat) | 2 | Steve Nash Equilibrium | **won, crystal destroyed** |

Against other teams: 3 wins in 3 games with a result, all by crystal kill.

## What the first live games showed (2026-10-16, release 195e798)

The runtime went live on release 1 and played Testing matches the same evening. The recording of
`e5f159ed` (vs warren-test, **won: crystal destroyed**; real time, 3000 steps, 1800 s limit) settled
most of the guesses (`harness/replay.py --schema-only`):

| Question | Live answer | Consequence |
|---|---|---|
| The map | **One lane.** Per camp: outer tower 4000 HP at ±11.5k, inner tower 6000 at ±21.5k, crystal 8000 at ±34.3k, `ACTOR_TOWER_SPRING` at ±45k, plus one `ACTOR_NONE` | The sim's map was rebuilt to match (`SimConfig.map="live"`) |
| Healing at home | **~0.25% of max HP a second**, no fast spring heal: heroes sent home at 30% sat there for minutes (30% → 85% took ~3 min) | No more walking home to heal (`heal_at_home=False`): a low hero escapes only while threatened |
| Revives | 0-23 s (`revive_time` in seconds, `hp: 0` and the position kept), back at full HP | Dying costs less time than healing; deaths still feed the enemy |
| The do-nothing button | **`s<n>:button_1` "NoOp"**; there is no `button_0` | Release 1's holds fell through to retreat (most of 7,758 retreat orders). Fixed: the hold button is found by its label |
| Labels | Attack: `Basic attack (nearest: enemy hero 133, 6120 away)`; skills: `Cast skill 1 toward enemy hero 133 (6120 away)` (also `toward enemy tower` / `enemy crystal`); advance: `Move toward the nearest enemy tower (N away)`; retreat: `Move back toward your own base (N away)` | Both shapes parsed; skills hit structures too |
| Raw buttons | 1 NoOp, 2 Move (`legal_move`: 24 directions), 3 NormalAttack, 4-6 skills, 8 ChosenSkill, 9 Recall, 10 EquipmentSkill, 11 HealSkill, 12 FriendSkill; aimed ones carry `legal_offset_x/z` (11 values) and `legal_target` | Only NoOp is used |
| HealSkill (release 2 used it below 40% HP) | **No heal seen**: of ~40 accepted uses in `ba540e6e`/`12bdf5a6`, HP 1 s later was flat or lower, and most of those heroes were dead 5 s later; it stays listed (no visible cooldown); its `legal_target` changes step to step | Off in release 3 (`heal_skill=False`) |
| `play_action`'s answer | Carries `state` in real time too (no `accepted` key) | The runner uses it directly; no extra read |
| `simultaneous` | Also `you_submitted`, `last_applied_actions` (every hero's applied action, the enemy's included: macros run as `Move`), `last_fallback_players`, `step_deadline_in_s` | Not used yet; `last_applied_actions` could show the enemy's intent |
| Refusals | 6 in the whole game, all races: `'macro_skill1' is not available for seat 1 now` | Handled: rested and replaced |
| Size | ~31 KB a state, ~3.6 KB gzipped; 2,202 states in 10 min (8 MB recording) | Fine |

Still unknown: lockstep games (none yet), whether `last_applied_actions` timing helps, what
ChosenSkill/FriendSkill/EquipmentSkill do, the heal skill's effect.

## After each live game

1. Copy the recording into `recordings/live/` here (gitignored; never change the originals in
   `~/srv/friendly-fighter-hok/recordings`), then
   `python -m bots.hok.harness.replay recordings/live/<session>/seat<N>.jsonl.gz`.
   The `wire-<pid>.jsonl.gz` next to it is every MCP call with its latency.
2. Fix `view.py` for any field that differs, set the distance knobs from the label distances seen at
   first contact, and add the real payload as a test fixture.
3. Check the runtime's log for `[hok] refused`, `[hok] illegal`, `[hok] exception` lines, and the
   status lines (structure damage dealt/taken, k/d) every 30 s of game time.

## The optional slow planner: a proposal, not built

Red Alert's `agent/commander.py` pattern would carry over: a background thread that every ~15 s
reads a compact view (score estimate, heroes' HP/levels, structures, enemies seen, the policy's
posture) and sets `Knobs` (posture, `min_pushers`, retreat lines, `hold`), never an order. Play never
waits for it.

**Not built, on purpose:**
- The decisions that win fights here are fast and local (retreat at 30%, skill when the target is
  in range, step out of tower aggro), and the scripted policy already makes them every 0.3 s. A
  model replying in seconds can't do better on those.
- The slow decisions a planner could take (posture, how many heroes to push a tower with, when to
  protect a lead) are exactly the knobs the sim sweeps tune, and the endgame posture is already
  scripted from the score.
- Without one live payload we don't know what a model would be reasoning about. The first
  recordings should show whether there is a systematic gap (e.g. losing every teamfight to one
  opponent style). Then the cheap version is a switch between two or three sim-validated knob
  presets, which can be scripted (the score trend) before any model is asked.

**Cost if built** (no model calls were made; this needs the user's OK): one call every 15 s with
~1,500 input tokens (the view, like upstream's `build_view`) and ~100 output tokens (a knob JSON).

| Match | Calls | Input tokens | Output tokens |
|---|---|---|---|
| Real time (3000 steps x 0.2 s = 10 min) | ~40 | ~60k | ~4k |
| Lockstep (ends at the time limit, ~30 min) | ~120 | ~180k | ~12k |

For a model priced at $X per million input tokens and $Y per million output tokens, a real-time
match costs about 0.06·X + 0.004·Y dollars (lockstep three times that). At an illustrative $1 / $5
tier, that is ~$0.08 per real-time match, ~$0.24 per lockstep match. Through the user's ChatGPT
quota (the Codex desk, like Red Alert's `bots/codex_advisor.py`) it costs no credits but needs the
user's OK for anything beyond small runs.
