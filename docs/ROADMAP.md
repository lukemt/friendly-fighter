# Roadmap: from a scripted bot to a tournament winner

> **A direction, not a plan of record.** This is how we picture the work today. Everything here
> is expected to change as experiments teach us more: revise, reorder or drop parts freely.

## Where we stand

- **Local games are fast and faithful.** One 10-minute game takes ~12 s, 12 games ~90 s, and
  they are timed like the platform. Seeded games and recorded traces replay exactly
  ([SIMULATOR.md](SIMULATOR.md), [SPEED_LOOP.md](SPEED_LOOP.md)).
- **The bot is weak against a competent opponent.** Our default bot won 26 of 184 games
  against OpenRA's Normal AI: 14-68 on trapped, 6-26 on sidestep, 6-64 on shadowfiend-ii.
  Every one of those games went to the time limit and was decided on the tiebreak.
- **Early lessons:**
  - Attacking into a defended base loses on the tiebreak: in self-play, the "turtle" beat
    our default bot 11 of 12.
  - Greed loses to a rush.
  - Small infantry waves feed the enemy kills.

## What winning means

Only wins score: a win is 1, a draw or loss 0. The format is Swiss rounds, then best-of-3
knockouts. In practice that means:

- **Trading well:** win the tiebreak (kills minus losses) by not throwing units away.
- **Finishing weak opponents:** an idle or slow opponent can be destroyed outright.
- **A solid floor:** the bot never plays worse than a strong scripted game, whatever else
  fails.
- **Adapting:** change plan within a game, and between the games of a best-of-3.

## The bot: one idea

Layers split by how fast each has to decide:

- **Reflexes (scripts), every view.** Economy, production, placement, focus fire, defending
  the base. They never wait on anything.
- **Tactical judgments (fast classifiers).** For example: do we win this fight, which target
  pays off, what kind of opponent is this.
- **Strategist (LLM).** Reads short summaries every 10-30 s and between games, and picks and
  tunes strategies from a playbook. Play never waits for it.

The layers work around a few shared stores:

| Store | Holds |
|---|---|
| **Knowledge base** | Strategies, counters, map and opponent notes, each with its evidence |
| **Game state** | The facts in the current view |
| **Analysis** | What the facts mean: score trend, strengths, threats, opponent style |
| **Current intent** | The active strategy, why it was chosen, and what would change it |

Today's `agent/redalert_bot.py` holds the reflexes. Its settings (Knobs) are a crude form of
intent, and `agent/commander.py` is a first attempt at the strategist.

## Many bots, one champion

No single bot gets strong by playing one opponent. The idea is a league:

- **The champion:** the bot we submit.
- **Challengers:** other designs, from scripted to LLM-led.
- **Exploiters:** bots built only to beat the champion.
- **Cheap copies:** token-efficient versions, so thousands of games stay affordable.
- **Fixed baselines:** OpenRA's AIs, the starter LLM bot and past champions. They catch
  regressions and keep the league from going in circles.

Ratings over all their games show who is strongest.

## How we improve

1. **Play.** The league runs games.
2. **Observe from outside.** Read losses, outliers and score swings.
3. **Hypothesise.** For example: "we lose units attacking into defences at minutes 4-6".
4. **Test.**
   - Settings sweeps.
   - Snapshot branches: replay a game to an interesting moment and try many tactics against
     several opponents from there.
5. **Change and keep only what wins.** The new version must beat the champion without
   losing ground against the baselines.
6. **Write down what we learned,** with the evidence.

## Building blocks

Built in the lab (2026-10-14, [LAB.md](LAB.md)):

- **Queryable game log.** `sim/lab/lab.db` holds:
  - who played (bot and version), seeds, map, result;
  - the score every 5 s, events, every batch with its reasoning and verdicts;
  - the trace.
  Platform games can be imported (`lab import`).
- **Bot registry.** `lab/bots.json`; a bot's version follows its settings and code.
- **League runner.** `lab league` / `lab gate` and Bradley-Terry ratings.
- **Snapshot branching.** `lab look` and `lab branch`, from each game's engine trace.
- **Observer reports.** `lab report`, `lab show`, and `lab dashboard`.
- **Playing ourselves.** `lab human`: the game pauses between moves.
- **LLM pacing in virtual time.** A bot's `think` can be `measured`: its real latency is
  charged as game time.

The knowledge base is started: [STRATEGY.md](STRATEGY.md). It holds what wins and loses against
each opponent, from the lab and from Claude playing OpenRA's AIs by hand, each point with its
evidence.

Still open:
- distilled cheap copies;
- an LLM orchestrator bot;
- classifiers trained from branch data.

## Experiments worth trying

- **Who is the field?** Most likely starter-style LLM bots, some scripted ones.
- **Settings sweep.** Posture, attack size, unit mix and target, across maps and opponents.
- **Where tiebreak points come from,** using the games we already recorded.
- **Exploiters** that punish weakness or only take good trades.
- **Branching** to compare tactics from the same moment.
- **Early opponent reads.** Rush, turtle, economy or passive, within 2-3 minutes.
- **Map knowledge.** Chokepoints, ore fields, staging spots.
- **Robustness.** Latency, refused orders, reconnects.
- **Claude vs the bot** via `scripts/ff_play.py`, to find weaknesses fast.

## Open questions and risks

- **The simulator is not the platform.** Check against live Testing matches from time to
  time.
- **Self-play can go in circles.** Keep fixed baselines and past champions in the mix.
- **One deterministic branch is one sample,** and short horizons punish attacks whose payoff
  comes later.
- **LLMs.** Cost, speed, and what the tournament rules allow.
- **Time.** How long until the tournament starts decides how much of this is worth building
  first.
