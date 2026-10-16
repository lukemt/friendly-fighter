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

(What these bots do differently, and why it could beat the champion or a part of the field.)

## Bots

| Bot | What it is | Status |
|---|---|---|
| `jev-council-m` | `ff-jev-council` (`bots.jev_council:create_agent`) with `--think measured` | registered; local-server check failed (below) |

## Results

(Each claim with its evidence: run ids, `lab probe` / `lab compare` output, games to look at.)

**2026-10-16, local Jev check: FAILED, the server does not answer.** Run 138
(`lab compare jev-council-m --baselines ai-normal --games 1 --seed 7`, game a03112ee, 19:37 game
time, lost, 758 s wall): every one of the 247 requests got no answer (latency median 6094 ms, p95
6145 ms = the client's 6 s timeout, `TIMEOUT_SECONDS` in `bots/jev_bot.py`), so the bot played on
frozen decisions and the game counts for nothing. Checked by hand afterwards: `GET
http://100.93.0.5:8090/` answers 404 in 0.14 s (host up), but `POST /v1/systemone` with a one-word
state hangs for 60 s with no reply, for model `jevk5-9b-v0.3.3` and for `jev-1.13.0`. So the model
behind the server is not loaded or is stuck, or the server is busy. Not measured yet: whether
`--think measured` charges latency to the game clock (needs a working server). Also to check once
it works: the council pins `MODEL = "jev-1.13.0"`, but the local model is `jevk5-9b-v0.3.3`, and
the client's 6 s timeout is above the ~1.2 s expected but would drop the first ~7 s call.

## Core changes

(Every change to shared code, rule 8: the file, what it adds, that it is off by default, its test.)

## Open questions and next steps
