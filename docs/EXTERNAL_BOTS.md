# External bots: what exists online to play against

A survey made on 2026-10-15 of OpenRA / Red Alert bots and AltruAgent tournament agents that
could be downloaded and connected to our simulator and lab (docs/SIMULATOR.md, docs/LAB.md).
Each entry is a lead to explore in detail, one at a time; tick it off in the checklist at the end
when done. **Wired in so far (2026-10-15):** #3 as `ext-ryan` (and `ext-ryan-claude`, Claude as its
advisor) and #4 (`ai-beginner`, `ai-easy`, `ai-medium`). **2026-10-16:** #1 as `ext-henry` (advisor, analyst
and squad leads off) with its sparring archetypes `ext-henry-rusher` and `ext-henry-yuriko`
(`RA_SPARRING`, active through its practice-disguise switch: `RA_SPARRING_AGAINST=lab`), and #2 as
`ext-pakled` (`subdir: src`; its local model is unreachable here, so its script plays). Suite
`@field2` = ext-ryan plus these four. Smoke run 184 (20 min, trapped, seed 5150): v10 won all four
(+10,200, +10,100, +21,600, +5,000); every bot fought (ext-henry: 77 units lost, 182 killed).

## Summary

| # | Name | What it is | Plays Red Alert without an API key? | Connects how | Priority |
|---|---|---|---|---|---|
| 1 | henrysue/ucla-steve-nash-equilibrium | Contestant: large modular RA bot, rules on every view, Claude advisor optional | yes (`REDALERT_ADVISOR=off` or no key) | `module` bot, `agent.dispatch:create_agent` | high |
| 2 | SirLinhart/PakledAgent | Contestant: very large scripted RA core + optional local JevK5-9B model; plus research data | yes (script plays when no model answers) | `module` bot, `agent.agent:create_agent` under `src/` (starter layout) | high |
| 3 | ryanxxhuang/altruagent-redalert | Contestant: rule-based RA bot (97 KB), optional 3-layer Claude advisor | yes (advisor off without `ANTHROPIC_API_KEY`) | `module` bot, `agent.agent:create_agent` | high |
| 4 | Built-in `beginner` / `easy` / `medium` AIs | OpenRA AIs already in our engine fork, not registered | yes | `ai` bot, `type: beginner/easy/medium` | high (trivial) |
| 5 | Khuongtdm/altruagent-entry | Contestant: parameterised rule policy (v3), optional LLM "coach" | yes | thin wrapper around `redalert_action(state)` | medium |
| 6 | lawrencedz/Riffo-Agent | Contestant: starter's `RedAlertPlayer` + heuristics + GPT-4o mini | partly (local fallbacks) | `module` bot, needs the starter's `examples/` | low/medium |
| 7 | OpenRA-RL `examples/` | Reference scripted bot and MCP bot from the engine's authors | yes | adapter from `openra_env` client to our runtime | low |
| 8 | FransBots | Six stronger in-engine OpenRA AIs (community mod) | yes | new `ModularBot` entries in the engine's `ra` rules | medium, needs porting |
| 9 | WilliamChenTW1209/altruagent-agent | Starter's LLM example plus a reflex autopilot | no (model makes army decisions) | like `starter-llm` | low |

Already registered and not repeated here: OpenRA's `normal`, `rush`, `turtle`, `naval` AIs and
the starter's LLM player (`starter-llm`).

## How to connect each kind

- **Contestant agents built on the AltruAgent starter** (built: `bots/external.py`, docs/LAB.md
  "Other contestants' bots"). Register a `module` bot with the factory
  `bots.external:create_agent` and `kwargs` `{repo, url, commit, name}` (+ `env`, `stubs`), then
  run `lab external fetch`. What happens for each seat:
  - **Loading:** their code loads in-process under an alias, with their imports rewritten in
    memory; it is never copied into ours.
  - **Clock:** their wall-clock timers run on the game clock.
  - **Settings:** their environment is the seat's own.
  - **No API:** `anthropic` is a stub, so no API is reachable.
  - **Advisor:** if they have a model advisor, `advisor: "desk"` puts Claude in the model's place
    (`lab advise`).
- **Our engine's own AIs**: registered (`ai-beginner`, `ai-easy`, `ai-medium`; suite `@ais-all`).
- **In-engine mods (FransBots)**: add their `ModularBot@...` blocks and any rule files to the
  fork's `mods/ra/rules/ai.yaml`, rebuild, check with `scripts/ffbench.py check` that the
  standard corpus still replays identically, then register as `ai` bots.
- **OpenRA-RL client bots**: they speak OpenRA-RL's Python client (`openra_env`, OpenEnv HTTP
  server on :8000). Needs a small shim that turns their `OpenRAAction` into our runtime's order
  batches, or a lane that serves the OpenEnv API.

Licences: only henrysue's repo has a licence (MIT). The others have none. Running them locally
as sparring partners is fine; do not copy their code into ours.

---

## 1. henrysue/ucla-steve-nash-equilibrium

- **Link:** https://github.com/henrysue/ucla-steve-nash-equilibrium (MIT, created 2026-10-08,
  last push 2026-10-14)
- **What:** a Pokémon specialist (`agent/vgc/`) plus a full Red Alert agent in
  `agent/redalert/` (~35 modules, ~900 KB, 400 KB of RA tests). Entry point for all games:
  `agent.dispatch:create_agent` (routes `red_alert` to `agent/redalert/agent.py`). Note
  `agent/agent.py` alone is the Pokémon strategist.
- **Design (from `agent/redalert/PLAN.md`):** "hyper-aggressive", tuned for the 20-min tiebreak
  (`kills_cost - deaths_cost`, then assets). Rules play every view (`agent.py` → `commander.py`
  → `executor.py`); `brain.py` sets a mode. A Claude advisor every ~6 s (off with
  `REDALERT_ADVISOR=off` or no key) reads the scouting report and sets posture, route,
  expansion, raid targets, turret side. Post-game Claude Opus analysis writes opponent profiles
  (tournament games only; `REDALERT_ANALYST=off`). Optional Haiku squad leads.
- **Modules worth reading:** `buildplan.py`, `expand.py`, `raid.py`, `scouting.py`, `snipe.py`,
  `strength.py`, `terrain.py`, `oregrowth.py`, `config.py` (all numbers).
- **Sparring archetypes (`sparring.py`, test only):** `rusher` and `yuriko`, scripted copies of
  real tournament opponents built from replays ("Yuriko" won vs "abu" by ~6:45 on Trapped and
  Sidestep, Oct 13). Their build orders and timings are written out in the docstring: useful
  intel about what the field plays, and a ready-made rush opponent.
- **To explore:** run it with the advisor off vs v10; also force each sparring archetype.

## 2. SirLinhart/PakledAgent

- **Link:** https://github.com/SirLinhart/PakledAgent (no licence, created 2026-10-09, last push
  2026-10-14, ~3 MB)
- **What:** a multi-game agent; the starter runtime vendored in `src/altruagent/`, the agent in
  `src/ucla_agent/`. The Red Alert core is `src/ucla_agent/games/redalert/` (~110 modules, by far
  the largest RA bot found: `upkeep.py` 174 KB, `expansion.py` 120 KB, `combat.py` 80 KB,
  `placement.py`, `siege.py`, `strike.py`, `raids.py`, many scouting modules, `rush_counter.py`,
  `rush_watch.py`, `nuke.py`, `remote_air.py`). Tuning in `config/redalert/tuning.toml` (157 KB).
- **Models:** a local JevK5-9B model (`jev9b` role, run by an included model server) picks the
  plan and commands one unit group within the script's checks; a hosted Jev service is optional
  (`JEV_API_KEY`, `config/agent.toml`). The script plays its own choice whenever no answer comes
  in time, so it runs with no model at all.
- **Research data (valuable on its own, under `docs/research/`):**
  - `red-alert-unit-matchups.md` + CSVs: DPS per armour class, per-$100 value, 1v1 duels, group
    fights (N = 1/5/10/20, focus vs spread fire), cost-equal fights at $2,000 and $6,000, defence
    value, standoff/kiting (1.2 MB CSV). Computed from the rule files.
  - `red-alert-rush-vs-defense.md`: rush validity and the cost of defence, with the engine rules
    behind them (build time = cost × 0.024 s, low power ×3, sell refund, what must die).
  - `local-openra-feasibility.md`: they also ran OpenRA-RL's engine locally over gRPC (Rush,
    Normal, Turtle AIs; two external agents in one game).
  - `docs/reference/altruagent/redalert.md`: their copy of the platform's RA reference.
- **To explore:** compare their matchup tables with docs/STRATEGY.md; run their bot with models
  off vs v10.

## 3. ryanxxhuang/altruagent-redalert

- **Link:** https://github.com/ryanxxhuang/altruagent-redalert (no licence, created and last
  pushed 2026-10-10)
- **What:** `agent/redalert_bot.py` (97 KB), "a fast, rule-based Red Alert player" about 5
  decisions a second: build order, power, repairs; tanks first with anti-armour infantry; a
  staging point, defend the base, attack in waves, pull back when a wave is losing; kiting,
  focus fire, emergency retreat below 0.4×. `agent/advisor.py`: three optional Claude layers
  (strategy / tactics / micro) on background threads, on only when `ANTHROPIC_API_KEY` is in
  `.env`. `agent/playbook.md` is their strategy guide (economy benchmarks: powr 0:10, proc 0:50,
  barracks 1:05, 2nd proc 1:45, weap 2:35, fix 3:05, first heavy tank ~3:40).
- **Entry point:** `agent.agent:create_agent` (starter layout).
- **Wired in (2026-10-15):** `ext-ryan`, pinned to `8047e75da137`, advisor off. Its per-game dump
  is stubbed out. `ext-ryan-claude` is the same bot with its advisor answered by Claude at the
  desk: strategy every 60 s, tactics every 20 s, micro off.
- **What it plays:**
  - its default opening is an infantry rush at ~0:55, then waves;
  - it destroys weak or slow opponents outright (the Normal AI at 7:15 in its first game, the
    Turtle AI at 6:51, `ff-default` at 3:58-5:25);
  - its timers run in game seconds (30 s status reports land on t=30.0, 60.0, ...);
  - the same seed twice gives the same game, and `lab replay` passes.
- **First results:** see "Results" below.

## 4. Built-in OpenRA AIs we have not registered

Our fork's `mods/ra/rules/ai.yaml` has `ModularBot@BeginnerAI` (`beginner`), `@EasyAI`
(`easy`) and `@MediumAI` (`medium`) besides normal/rush/turtle/naval. Weaker baselines for
checking that we win convincingly (and by how much: points matter, not only wins). The OpenRA-RL
platform itself uses the Beginner AI as its baseline.

## 5. Khuongtdm/altruagent-entry

- **Link:** https://github.com/Khuongtdm/altruagent-entry (no licence, created 2026-10-11, last
  push 2026-10-12)
- **What:** its own play loop (`agent.py`, based on the platform's sample, not the starter) plus
  `redalert_policy.py` "v3: economy first, defended, staged, and built for coach-driven tuning":
  spends cash + ore, fixed build targets, a time-based minimum army ("defense floor"), rally at a
  staging point, defend, attack at `combat_target` units, keeps fighting without a construction
  yard. Every parameter is in `PARAM_SPECS` and can be overridden from `params.json` (ranges in
  `PARAMS.md`); an optional LLM strategist every ~25 s (`LLM_BASE_URL`/`LLM_MODEL`).
- **Includes:** 5 platform match logs in `ra_logs/` (JSONL) and `summarize_logs.py`.
- **Connects:** wrap `redalert_action(state)` / `log_result(state, result)` in a
  `create_agent()` object. Its parameter file makes it easy to field several variants
  (e.g. a defensive and an aggressive setting).

## 6. lawrencedz/Riffo-Agent

- **Link:** https://github.com/lawrencedz/Riffo-Agent (no licence, created 2026-10-13)
- **What:** one 104 KB `agent/agent.py`, "Riffo v5.2": the starter's `RedAlertPlayer` with
  `FastRedAlert`/`HybridRedAlert` (deterministic micro + utility heuristics), GPT-4o mini
  compares options with an 8 s limit; local fallbacks when the model fails.
- **Connects:** `module` bot; needs the starter's `examples/` package. Without a key it plays
  only its fallback, which may not be how it plays on the platform.

## 7. OpenRA-RL examples

- **Link:** https://github.com/yxc20089/OpenRA-RL/tree/main/examples (the project behind the
  platform's engine, see docs/BACKEND_ANALYSIS.md)
- **What:** `scripted_bot.py` (34 KB, a state machine: deploy, power, barracks, refinery, war
  factory, train infantry + APC, attack-move, sustain with repair/sell), `mcp_bot.py` (28 KB),
  `llm_agent.py`. Probably weak, but the engine authors' own reference play.
- **Connects:** needs a shim (see "How to connect").
- About 30 forks exist; the active ones (kunrenzhilu, dafahaha, lordtiva, cafel176, alibad,
  pushed Aug–Sep 2026) were not inspected for bots yet. AIPensieve/MemorySys-in-OpenRA-RL- is an
  RL agent with cross-episode memory.

## 8. FransBots

- **Links:** https://www.moddb.com/mods/fransbots, forum thread
  https://forum.openra.net/viewtopic.php?p=313707 (version 20231220)
- **What:** six AI opponents for Red Alert that, per the author, beat the default AI without
  cheats: optimised base building, production and economy, expansion, engineers, thieves,
  medics, Tanya, smarter nukes. Installed by replacing 19 files in 3 folders of `mods/ra`.
- **Unknowns:** ModDB and the OpenRA forum block scripted access, so whether it is YAML only or
  also code is not confirmed. It targets a 2023 release; our fork is a newer development build,
  so the files likely need porting. The user may need to download the zip by hand.
- Related older community bot: epAI (https://forum.openra.net/viewtopic.php?p=281611), not
  inspected.

## 9. WilliamChenTW1209/altruagent-agent

- **Link:** https://github.com/WilliamChenTW1209/altruagent-agent (last push 2026-10-10)
- **What:** `agent/agent.py` is still the placeholder; the RA work is in the starter's LLM
  example plus `examples/llm/redalert_autopilot.py` (reflexes: deploy, place buildings, power,
  economic core, 3 harvesters, defend). Army decisions are the model's, so without a key it
  builds a base and does not fight: close to our `builder` baseline.

## Results (experiment `field-e1`)

`ext-ryan` (rules only, advisor off), 2026-10-15. Run 73: 16 games against v10, plus 2 seeded checks (runs 67-68).
Run 74: a 24-game league with the three AIs and `ff-default`.

| Opponent | Games | ext-ryan W-L | Net mean (ext-ryan) | Wipe-outs |
|---|---|---|---|---|
| `ff-council-champ-v10` (champion) | 18 | 0-18 | −11.3k | none |
| `ff-default` (**the live bot**) | 4 | 4-0 | +10.1k | ext-ryan destroyed it in all 4 (3:58–5:25) |
| ai-normal | 4 | 2-2 | −5.5k | 1 by ext-ryan |
| ai-turtle | 3 | 2-1 | +3.8k | 1 by ext-ryan |
| ai-rush | 3 | 1-2 | −3.0k | 1 each way |

- **v10 holds.** ext-ryan's 0:55 infantry rush and its waves break on v10's guard and defend
  posture: about −11k a game for the attacker.
- **The live bot does not hold.** The same rush destroys `ff-default` outright in 4 of 4 games. A
  field with even one rusher like this makes the live bot's floor a loss. That is a strong
  reason to switch the live bot to v10 (the user's decision; LAB_PLAN E10).
- **Ratings:** ext-ryan rates at about the AIs' level (1580 ±98 against Normal 1592, Rush 1565,
  Turtle 1478), far above `ff-default` (1258).
- **Small samples** (3-4 games against each AI). The v10 result is clear; the rest is a first look.

### Claude as ext-ryan's advisor against v10 (experiment `advisor-e5`, runs 75-77)

Three full games of `ext-ryan-claude` against `ff-council-champ-v10`, on the seeds where plain
`ext-ryan` had lost (run 73). Seat 0, about 40 answers a game: strategy every 60 s, tactics every
20 s, micro off. The transcripts are in `sim/lab/advise/<game>.jsonl`, with notes on each game.

| Map (seed) | ext-ryan alone | ext-ryan + Claude | Assets (ours / v10) | How |
|---|---|---|---|---|
| trapped (10010) | loss −15,000 | **win +5,550** | 29.6k / 26.0k | economy; a strike at 6:43 (+1,100); an all-in at 8:44 caught v10's heavy tanks in the open |
| sidestep (10014) | loss −15,650 | loss 0-0 (assets) | 48.8k / 51.35k | pure economy turtle; v10 never left home and out-spent us late |
| shadowfiend-ii (10018) | loss −12,000 | **win +2,350** | 40.7k / 36.05k | economy to 5:43, then a 45-60-unit strike on v10's base edge (+3,050), back home |

- **The same bot, advised, went from 0/3 to 2/3 against the champion.** What changed is only
  the plan.
  - Plain ext-ryan rushes at 0:55 and throws ~65 units into v10's defenses.
  - Advised, it builds economy first (2 refineries before the war factory, 4-5 refineries,
    7-9 harvesters), keeps the army home, strikes once with a mass, and protects the lead.
- **It is one advisor's 3 games** (no latency: the game waited for each answer). The size of
  the swing matters more than the exact rate.

---

## Rejected (do not download)

- **Unwelcome-assault9086/openra-ai-bot-2026** (+ `unwelcome-assault9086.github.io`): new
  account, no code, "unbeatable AI" with a Windows download through a github.io page. A typical
  malware-lure pattern.
- **Vintorez91/Spearhead-AI**: account 4 days old, no source, a Windows installer zip (`.cmd`,
  asks for administrator rights) that drops a compiled bot DLL into OpenRA release-20250330. A
  DLL built for that release would not load in our fork anyway. Its README's feature list
  (scouting with remembered contacts, MCV expansion, engineer captures, APC raids) is a source
  of ideas only.
- **sharedcare/OpenRA-Bot**: an RL project with its own pythonnet bridge; trained only on build
  orders (no combat). Not a sparring partner.

## The AltruAgent search (all of GitHub, repositories, newest first)

Run on 2026-10-15 through the GitHub search API (unauthenticated, so repository search only;
code search needs a login). Queries: `altruagent`, `altruagent in:readme`,
`altruagent in:description`, `altru-agent`, `"altru agent"`, `altruagent-game`,
`ucla red alert agent`, `ucla ai agent gaming tournament`, plus the forks of the official
starter. 30 repositories came back; the unrelated ones ("altruism" simulations, onboarding
agents) are left out below.

| Last push | Repository | Games | Red Alert content |
|---|---|---|---|
| 2026-10-15 | jas8n/pokemon-vgc-agent | Pokémon | none |
| 2026-10-15 | holmjames/ace-trainer-claude (starter fork) | Pokémon | none |
| 2026-10-14 | henrysue/ucla-steve-nash-equilibrium | Pokémon, Red Alert | **#1 above** |
| 2026-10-14 | lukemt/friendly-fighter | Red Alert | ours (see note below) |
| 2026-10-14 | lawrencedz/Riffo-Agent | all three | **#6 above** |
| 2026-10-14 | Yxyxyxyxyxy111/ucla-agent-jet2holiday | Werewolf | none |
| 2026-10-14 | thuanbui743-dotcom/Yanmega | Pokémon (TypeScript) | none |
| 2026-10-14 | SirLinhart/PakledAgent | all three | **#2 above** |
| 2026-10-14 | siIverfish/altruagent | ? | empty repository |
| 2026-10-14 | aliceblee/altruagent-starter (fork) | Werewolf | none |
| 2026-10-13 | iamitiwari/altruagent-game (fork) | Pokémon | none |
| 2026-10-12 | Khuongtdm/altruagent-entry | Red Alert (+ placeholders) | **#5 above** |
| 2026-10-11 | Khuongtdm/pokemon-agent | Pokémon | none |
| 2026-10-11 | abubakarsiddik31/quicksilver | Pokémon, Werewolf | stubs only |
| 2026-10-10 | WilliamChenTW1209/altruagent-agent | via LLM example | **#9 above** |
| 2026-10-10 | ryanxxhuang/altruagent-redalert | Red Alert | **#3 above** |
| 2026-10-08 | jerryhuang-math/hok-agent | Honor of Kings | empty repository |
| 2026-10-08 | omarcano23/altruagent-omarcanoc23 | Pokémon | none (starter's RA example only) |
| 2026-10-08 | traffichaos01/altruagent-rhartshorne-2026 (fork) | placeholder | none |
| 2026-10-08 | koichiio/altruagent-starter (fork) | placeholder | none |
| 2026-10-07 | wu-warren/altruagent-starter (fork) | placeholder | none |
| 2026-10-07 | UCLA-Trustworthy-AI-Lab/altruagent-starter | official starter | `examples/llm/redalert.py` (= `starter-llm`) |
| 2026-08-21 | sarthakmohindru04/Autonomous-Reasoning-Agent | Prisoner's Dilemma | none (not this tournament) |

**Note: our own repository is public.** lukemt/friendly-fighter shows up in this search, and
the API reports it as public: the bot, docs/STRATEGY.md and the lab are readable by every
other contestant. `.env` is gitignored, so the keys are not in it.

**Limits of the search:** repository search only finds names, descriptions and READMEs;
contestants who never wrote "AltruAgent" there, private repositories, and code search hits are
missing. With a GitHub token, code search for `altruagent`, `red_alert` + `choose_action`, or
`api.altruagent-game.com` would find more.

## Exploration checklist

- [x] 4. register `ai-beginner`, `ai-easy`, `ai-medium`
- [x] 3. ryanxxhuang: clone to `sim/external/`, register, smoke game, v10 vs it (run 73); advisor desk
- [x] 3b. advised games vs v10 (runs 75-77: 2 wins, 1 loss on assets, against 0/3 unadvised)
- [ ] 3c. advised games vs the Rush AI and the Turtle AI
- [x] 1. henrysue: same, advisor off; also its `rusher` / `yuriko` sparring modes (run 184)
- [x] 2. PakledAgent: same, models off (run 184); [ ] read the matchup research against docs/STRATEGY.md (the jev family uses it)
- [ ] 5. Khuongtdm: wrapper; default and one aggressive `params.json`
- [ ] 6. Riffo: decide whether a fallback-only run is meaningful
- [ ] 8. FransBots: get the zip, check the file types, port to the fork, corpus check
- [ ] 7. OpenRA-RL scripted bot: shim
- [ ] GitHub code search with a token
