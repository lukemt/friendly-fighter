# Several Jev decision loops in one bot

How the Jev bots ask TypeSafe's Jev (System One) for decisions. There are two designs:
- **one call** that asks everything (`bots/jev_bot.py`, the v7 champion);
- **a council** of loops, each with its own briefing, view, pace and questions
  (`bots/jev_council.py`).

This page covers what the TypeSafe docs say about combining loops, how the council applies it, and
what it has shown so far.

## What the docs say (docs.typesafe.ai, read 2026-10-14)

- **One request per state.** Questions that judge the same state go in one request:
  - Jev reads the state once and bills it once;
  - each question costs only its own tokens;
  - all questions are answered in parallel, so adding one barely changes the latency.
- **Answers are independent.** No question sees another's answer, and the question ids are never
  shown to the model, so each instruction has to say everything.
- **A second request only for a real dependency.** Make one only when the first answer is needed
  to build the next request: to fetch evidence, to choose what the state contains, or to set the
  next options. Otherwise ask everything together and let code ignore what it doesn't need
  ("speculative fan-out").
- **Point questions at parts of the state.** Write the path in backticks, such as "Using
  `briefing`, ...". That is how a rulebook can sit in the state once instead of inside every
  question.
- **Gate on confidence, per action.** A risky action needs a clearer answer than a harmless one
  ("confidence-gated routing").
- **Keep inferred state apart from observed facts.** Code keeps goals and observations, fresh
  judgments steer the next bounded step, and a result is checked for freshness before it is
  applied ("respond to changing state").
- **Memory.** Save what matters and add it to later states ("simple memory system" cookbook).
- **Options built by code.** Code may offer only the options that can be right now (as in
  "hierarchical classification").
- **Models.**
  - `jev-latest` is still `jev-1.13.0`.
  - `jev-1.14.0` came out on 2026-10-14 as `jev-preview`. On our 33 labelled cases it was no
    better: 29/33 with the plain questions against 28–29 for 1.13, and 31/33 with literal2
    against 32–33.
  - An alias can move without notice, so the council pins `jev-1.13.0`.
- **Images and Box questions** are "dreamer-ready", not production. A minimap image is a possible
  later experiment.

## The council (`bots/jev_council.py`)

The one-call bot sent about 8,000 characters of questions with every call. Its 13-line playbook
was copied into each of the 3–5 questions, and each call cost about 2,700 tokens. The council
splits the work three ways:

| loop | when | sees | asks |
|---|---|---|---|
| strategist | every 30 s, and at once on a new event | the whole game so far, plus a memory of events written by code (raids and their trades, the first sight of each enemy unit type, attack results) | opponent, plan (hold / boom / fortify / attack), "will an attack come within 2 minutes?" (Noul), the opening before 4:00 |
| quartermaster | every 20 s, and at once on a new enemy unit type | both armies, the economy, the plan | what to train (infantry-only is offered only before 4:00) |
| field commander | only when there is something to command: a fight away from home, an army out, or the attack plan; every 3 s in a fight | the fight in words, the score, the plan | defend / gather / commit / retreat |

- **Briefings sit once in each state.** Each loop's rules are in its own `briefing`, and the
  questions point at it. Requests cost about 1,370 tokens (strategist), 680 (quartermaster) and
  870 (field commander).
- **The plan becomes knobs.** `boom` sets greed 2. `fortify` sets greed 0 and up to 4 defenses.
- **Confidence gates the plan.** A new plan needs probability 0.55, and 0.6 for `attack` or
  `boom`. An expected attack (the Noul at 0.5 or more) vetoes `boom`.
- **The one real dependency.** When the strategist's answer changes the plan so that the field
  commander is needed, a second request follows in the same tick, built with the new plan.
- **The rails of `bots/jev_bot.py` still stand** behind every posture.
- **`lab jev eval --council`** asks the labelled cases the way the council does: the strategist
  first, then the other loops with its plan.
  - It scores 34/35, against 33/35 for literal2 in one call.
  - Its one miss is a deliberate trade-off: fortify, where the label said greedy, at 7:10.

**Tokens.** About 45–57k a game, against about 100k for v7, so 45–55% fewer. The strategist makes
about 22 requests a game and the quartermaster about 31. The field commander rarely runs, because
the strategist almost never plans an attack.

## Results (runs 59–60, paired seeds, against @standard + @exploiters + chaotic)

| | run 59 | run 60 | notes |
|---|---|---|---|
| ff-jev-champ-v7 | 70% | 79% | |
| ff-jev-council (starts with hold) | 67% | – | lost turtle standoffs 4/8: no greed, so even assets |
| ff-jev-council-boom (starts greedy, kept until 4:00) | **81%** | 75% | pooled 78% against 75%: within noise |
| ff-jev-v9 (one call, greedy start) | – | 80% | |

- **Run 59 alone said "better"** (+0.11 points a game, 95% CI +0.01 to +0.21). The fresh seed
  did not confirm it (−0.04, CI −0.16 to +0.08). One seed is not enough.
- **Repeated in both runs:**
  - **Against exploit-eco-tanks the council is much better:** 5/8 and 4/6, against 1/8 and 2/6
    for v7, with mean net about −4k against −14k. Its quartermaster trains rocket infantry before
    the tanks come, and "fortify" stops the greed.
  - **In turtle standoffs it is worse:** 5–7/8, against 8/8 for v7. "Fortify after 5:00 when
    the enemy army is unseen" gives up the assets tiebreak.
- **The fix being tested (council v2):**
  - fortify only between 5:00 and 7:00 (eco-tank waves came at 6:37–6:44);
  - boom after 7:00 when no enemy has come all game;
  - "mixed" is the default production before 5:00.
- **Not a Jev problem: the economy is starved.** In a lost Rush AI game, cash was 0 from 2:00 to
  the end, because the scripted build order puts the war factory before the second refinery
  (built at 3:58). That is the opt-in knob `Knobs.early_refineries`, tested on the scripted bots
  (experiment `early-refineries`).

## Operations

- **Credits.** The TypeSafe account ran out of credits during run 60, after about $5.16 in all
  (1,125 game seats, about $0.005 a game). Jev then answers `402 billing_error`.
  - From game 163 on, the Jev bots played on frozen decisions. `jevlog.run_health` names those
    games, and the analysis above drops them.
  - `lab play/compare/gate` now try Jev once before starting and refuse with the reason. After a
    run they warn, and leave a note, when Jev bots went without answers.
- **Latency.** The loops that fall due together go out in parallel (one thread each), so a tick
  still costs one round trip, about 250 ms.
