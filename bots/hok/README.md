# bots/hok: friendly-fighter's Honor of Kings 5v5 bot

The platform's `honor_of_kings_5v5`: a real-time 5v5 MOBA where one agent commands all five heroes
of a camp (seats 0-4 or 5-9). Each hero gets one action per 0.2 s step; a **macro** (`macro_advance`,
`macro_retreat`, `macro_attack`, `macro_skill1..3`) becomes that hero's **standing order** and keeps
playing until it gets another. The crystal wins; at the step limit (3000 steps by default) or time
limit, the tiebreak is damage to enemy towers and crystal, then hero kills. Contract:
`https://api.altruagent-game.com/skill/hok`, upstream's `GAMES.md` section and `tests/test_hok.py`.

**No live payload has been seen yet.** Online test matches had closed before this bot was built.
Everything below is built from the published contract and upstream's fixtures, and checked on a
mock platform and an abstract sim. The first live game is the first real test (see
"Known unknowns" and "After the first live game").

## The idea

A scripted team controller in the spirit of Red Alert's `agent/redalert_bot.py`: it answers every
view fast from fixed rules and never waits on a model. The macros already aim (at the *nearest*
target) and walk the hero, so the policy decides each hero's **intent** and leaves the aiming to the
server:

1. **Retreat on low HP and come back healed**, with hysteresis: out below 30% HP, back only at 85%.
   **Evade** for 2 s when a hero below 60% would die within 2 s at its current damage rate near an
   enemy hero or tower (the dive and burst detector: scale-free, from the HP history).
2. **Fight together, not one by one.** A hero engages visible enemy heroes only when our HP-weighted
   strength around it is at least theirs (enemies fogged in the last 4 s count half). Outnumbered
   heroes back off, unless they stand under our own tower or crystal.
3. **Skills on heroes first.** A skill whose label names an enemy hero (or whose distance matches a
   visible enemy hero's, when the label is opaque) is cast first, the ultimate included; then the
   basic attack. Skills go on minion waves only while no enemy hero is near.
4. **No dives.** Never chase an enemy hero under its tower without the team; never stand in an
   enemy tower's range with fewer than 3 healthy heroes there (unless the tower is under 15%).
5. **Defend** a structure of ours that is losing HP: healthy heroes within reach go back and fight.
6. **Push together.** A hero far ahead of the team's median waits (the do-nothing button, the only
   raw action used: a hold); otherwise farm what is in range, then advance.
7. **Play for the tiebreak** in the last 60 s: ahead on structure damage (by half a tower), pull
   back and protect it; level or behind, all in (retreat only below 12%, no waiting).

## Files

| File | What |
|---|---|
| `view.py` | Reads a state into a `TeamView`: our heroes, enemies (fog), structures, offers per seat, labels (`nearest: <what>, <d> away`), limits. Never raises on missing or mistyped fields. |
| `policy.py` | `TeamPolicy` and its `Knobs`: one `Choice` (macro, reason, priority, alternatives) per hero. Memory: HP history, retreat state, structure damage (a structure that leaves the list at ≤25% HP counts as destroyed). |
| `agent.py` | `HokAgent`: the runner's hooks, real time vs lockstep, sending only changes, refusals, pacing, logging, the safety layer. |
| `sim/engine.py` | An abstract 5v5 MOBA (not the real game) for relative comparisons. |
| `sim/match.py`, `sim/compare.py` | Two agents through the platform's interface on the sim; the comparison CLI. |
| `sim/baselines.py` | `button0` (the placeholder), `advance`, `upstream` (the starter's rules), `ff-hok[:{knobs}]`. |
| `harness/mock_driver.py` | Our agent through the real runner (`run_game`) against a mock platform, with chaos. |
| `harness/replay.py` | A live recording: schema report, game summary, replay through the bot. |

## The runtime contract, as implemented

- **One hero per `choose_action` call**; the runner calls again at once for the next one.
- **Real time** (`pacing.mode == "realtime"`): the team is planned on every state, and only
  *changes* are sent: a hero whose standing order already matches gets nothing; an order sent within
  1.5 s isn't resent while the server's report catches up. The most urgent change goes first
  (retreat, then skills on heroes, then attacks, then holds and advances). Nothing to change:
  `WAIT`, and idle looks are paced to one per 0.3 s (`FF_HOK_PACE_S`), instead of one per step.
- **Lockstep**: every pending hero is answered at once with its planned macro, even when it's the
  standing order; a dead hero (no macros) gets the do-nothing button, or any listed button with its
  first legal values. A seat is never answered twice in one step.
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

## Known unknowns (what only a live game will show)

Each is a guess the bot is built to survive; `harness/replay.py`'s schema report answers most of
them from the first recording.

| Unknown | What the bot assumes | If it's wrong |
|---|---|---|
| Distance scale | Upstream's 9000 is "in range"; towers reach ~9000 | Engage/tower rules fire too early or late; all distances are `Knobs` |
| Label format | `"... (nearest: enemy hero 169, 4123 away)"` (upstream's GAMES.md) | Target kind falls back to matching the label distance against visible heroes and towers; no distance at all: macros are treated as out of range, so heroes advance instead of attacking |
| A standing skill order on cooldown | May idle the hero (the harsh reading) | The bot switches back to attack as soon as the skill leaves the offer, so either way costs a step or two |
| `macro_advance` | Walks to the nearest enemy structure; may stop under it without attacking | The tower rules and the evade rule keep heroes from standing there alone |
| `your_pending_seats` in real time | Every living hero, every step | If only heroes without standing orders are pending, changes still go out as soon as a hero is listed |
| `standing_orders` format and lag | `{"<seat>": "macro_…"}`, current | Also reads `s3:macro_x` and `{"macro": ...}`; an order sent < 1.5 s ago isn't resent; with no report at all it trusts what was accepted |
| The do-nothing button (`s<n>:button_0`) | Idles the hero and clears its order | Used only to hold position; `Knobs.hold = false` turns it off |
| Destroyed structures | Leave the list, or stay at 0 HP | Counted as destroyed when they leave the list at ≤ 25% HP |
| Fog and per-seat views | Team vision: the same enemies in every summary | The union of the five views is used |
| `revive_time` units | Only `> 0` with `hp` 0 matters | — |
| Read/send rate limits | ~3 idle reads/s plus one read per sent order | `FF_HOK_PACE_S` slows it down |
| Lockstep length | 5 calls per step: 3000 steps would take ~25-50 min, so the time limit ends it | The endgame clock uses `min(seconds_left, steps_left x 0.2 s)` |
| What counts for the tiebreak | All damage to enemy towers and crystal (minions' too) | The score estimate is HP lost by visible structures, which is what any rule would count |

## After the first live game

1. `python -m bots.hok.harness.replay ~/srv/friendly-fighter-hok/recordings/<session>/seat<N>.jsonl.gz`
   (in this workspace: copy the file in first). Read the schema report against the table above.
2. Fix `view.py` for any field that differs, set the distance knobs from the label distances seen at
   first contact, and add the real payload as a test fixture.
3. Check the runtime's log for `[hok] refused`, `[hok] illegal`, `[hok] exception` lines, and the
   status lines (structure damage dealt/taken, k/d) every 30 s of game time.
