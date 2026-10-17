# bots/pokemon: the `pokemon_vgc_doubles_draft` bot

## Brief

friendly-fighter's agent couldn't get past Team Preview: `agent/agent.py` sent the bare
`select_lineup` template, the server refused it, and every Pokémon game was lost on the clock. This
package plays all three phases of the tournament's Pokémon game (draft, Team Preview, VGC doubles),
scripted and deterministic. It makes no model calls and answers in well under a second.

**Done when:** all three phases play, the tests pass in the container, and harness results against
both baselines are written up here. Then a release is cut and checked, and the user decides when the
runtime starts.

## How it plays

`agent/agent.py` routes every `pokemon_vgc_doubles_draft` decision to `bots.pokemon.create_agent()`.
That's one `PokemonAgent` per match: the worker process creates the agent once, so it carries
memory from the draft into the battle.

| Phase | What it does | Code |
|---|---|---|
| draft (15 s a pick) | Scores each offered card with `card_value`: its average duel against every card it could face (the opponent's roster counts double), credit for covering foes the team can't answer yet, doubles staples (Fake Out, Intimidate, redirection, Tailwind, Trick Room with slow partners, Spore, spread STAB, Wide Guard...) with diminishing returns, a penalty for stacking shared weaknesses, and a little base-stat total. Picks the best `ours + 0.45 x theirs`, so a card the opponent wants badly counts too (denial). | `team.py` `choose_draft` |
| team_preview (90 s) | Tries all 15 sets of 4 and all 6 lead pairs. Scores each against the opponent's six, weighting the four they're most likely to bring, plus coverage (the best answer to each foe), support value, and lead synergy (Fake Out + attacker, Trick Room + slow attackers, Tailwind, Intimidate, no shared weakness). | `team.py` `choose_lineup` |
| doubles turns (55 s / 420 s bank) | Reads the board (both sides' sets from draft memory, HP, status, boosts, weather, terrain, Trick Room, screens, Tailwind). For each slot it scores every offered move x target and every switch, then scores all pairs of slot choices together. | `battle.py` `TurnPlanner` |

**Turn scoring** is in units of HP fractions:
- Damage dealt to each foe: expected damage from a gen 9 damage estimate (`damage.py`) times
  accuracy, capped at the foe's HP and weighted by how dangerous the foe is.
- A bonus for each likely KO. KO odds come from the damage roll range; both slots' hits on one foe
  are combined over hit/miss outcomes, so focus fire and overkill are both judged right.
- Each action is scaled by the chance our Pokémon acts at all: a faster foe or a priority move
  KOs it first, or a foe's turn-1 Fake Out flinches it.
- Spread moves get the 0.75 modifier. Damage to our partner (Earthquake, Surf...) counts against
  the move, unless the partner is immune, protects or switches out.
- Damaging moves never target our own side.
- Fake Out only on a Pokémon's first turn, and not against Ghost types, Inner Focus, Covert
  Cloak, Armor Tail or Psychic Terrain. Two Fake Outs into one foe count once.
- Protect is worth most when we'd be KO'd before acting (it buys a turn for the partner).
  Back-to-back Protect is discounted to its 1/3 success chance.
- Helping Hand boosts the partner's hits in the pair score.
- Status and support moves are judged by condition: sleep and burn where they stick, Tailwind and
  Trick Room by the speed they'd swing, screens, redirection when the partner is threatened, setup
  when safe, recovery when low.
- A switch must beat staying in on damage taken, KO risk and offence, with a cost for switching.
  Forced switches pick the best matchup.

**Safety layer** (`agent.py` `check`/`fallback`): every answer is checked against the server's
own template before it is returned: the right type, a listed move and target, a listed switch, no
two slots switching to one Pokémon, no double pass. On any exception or failed check, it plays the
smoke agent's legal choice (`examples/smoke_agent.py`). If even that can't read the template, it
plays the simplest legal answer it can build. It never returns the template itself.

**Data.** `data/dex.json` is Showdown 0.11.11's gen 9 data: species, moves, the type chart, items,
abilities and natures. `tools/extract_data.js` makes it. It is committed because a release is a
read-only git snapshot with no `node_modules`; the bot itself is plain Python.

## Testing

```bash
docker exec -u dev -w /workspace friendly-fighter-pokemon-dev-workspace-1 .venv/bin/python -m pytest -q tests/test_pokemon_bot.py
```

`tests/test_pokemon_bot.py` covers:
- each phase on payloads shaped like the platform guide's;
- the stat and damage model;
- turn behaviour: Fake Out on turn 1 only, focus fire on a weakened foe, avoiding Earthquake into
  the partner unless it's immune, Protect when about to be KO'd, forced switches;
- the safety layer (planner crash, an answer the template doesn't offer, answers the server would
  refuse);
- the time budget;
- routing through `FriendlyFighter`, where Pokémon goes to this bot and not the LLM example.

**Local harness** (`harness/`): whole games with Showdown's own engine as referee.
- `harness/sim_server.js` runs `BattleStream`s for Python.
- `harness/harness.py`:
  - deals an 18-card pool, with distinct species and items, from `data/sim_cards.json`;
  - builds each observation in the live payload's own shapes (see "The live match" below):
    poke-env's condition maps (`{"GRASSY_TERRAIN": 0}`), statuses as `"BRN"`/`"FNT"`, the
    opponent's HP in percent with `"unknown_item"`;
  - runs the snake draft `A B B A A B B A A B B A` with the Item Clause;
  - runs Team Preview;
  - battles in `gen9vgc2025regi`, building each side's observation and `doubles_turn` template
    the way the platform guide describes them.
- Seats and the first drafter alternate.
- `data/sim_cards.json` holds 447 VGC-legal cards made by `harness/make_cards.js`: Showdown's
  random-doubles sets with competitive natures and EVs. The platform's real card list isn't
  public, so this pool stands in for it.

```bash
# Showdown for the harness (once, in the container):
mkdir -p ~/showdown && cd ~/showdown && npm init -y && npm install pokemon-showdown@0.11.11
.venv/bin/python -m bots.pokemon.harness.harness --a bot --opponent maxdamage --games 200 --workers 2 --seed 1000
.venv/bin/python -m bots.pokemon.harness.harness --a bot --opponent maxdamage --games 200 --workers 2 --seed 1000 --pool live
```

Baselines (`harness/baselines.py`):
- `random`: a random legal card, lineup and slot choice.
- `maxdamage`: drafts and brings the highest base-stat totals. Each turn it uses the move with the
  most base power x STAB x type effectiveness on the best foe, and switches only when forced.
- `smoke`: the first legal option.

`bot+bstdraft` is our battle play with max-damage's draft and lineup. It measures battle skill alone.

## Results

On the harness's stand-in pool (seeds 1000-1199, 200 games each, seats alternating):

| Bot | Opponent | Games | Wins | Losses | Win rate | Refused answers |
|---|---|---|---|---|---|---|
| bot | random | 100 | 100 | 0 | 100% | 0 |
| bot | maxdamage | 200 | 156 | 44 | 78% | 0 |
| bot+bstdraft (battle alone) | maxdamage | 200 | 130 | 70 | 65% | 0 |
| bot+bstdraft (battle alone) | random | 200 | 200 | 0 | 100% | 0 |

The max-damage row is from the build after the live match; the release `b4ec5f0` won 149 of the
same 200. The other rows are `b4ec5f0`'s.

On live-style pools (`--pool live`, seeds 1000-1199): 18 cards drawn from the 30 seen in live
matches, with items shared as on the platform.

| Bot | Opponent | Games | Wins | Losses | Win rate | Refused answers |
|---|---|---|---|---|---|---|
| bot | maxdamage | 200 | 169 | 31 | 84.5% | 0 |
| bot+bstdraft (battle alone) | maxdamage | 200 | 156 | 44 | 78% | 0 |

- The draft adds about 6 points over max-damage's base-stat draft.
- The bot's favourite picks: Great Tusk, Chi-Yu, Iron Hands, Basculegion, Dondozo, Gyarados and
  Incineroar.
- Its slowest decision took 0.9 s.
- On the first match's own 18 cards alone, it won 195 of 200, drafting Iron Hands, Incineroar,
  Basculegion and Iron Treads every time.

- **Speed:** a mean of about 50 ms a decision, with a maximum of 0.5 s on the stand-in pool and
  0.9 s on the live one (a full draft pick on a 2-CPU container running two games at once).
- **Battle length:** about 4 turns on the stand-in pool and 6 on the live one. Level-50 sets with
  full attack EVs hit hard.

## The live matches

Two live test matches were played on 2026-10-16, both on release `b4ec5f0`, both with every
answer accepted and 0 fallbacks:
- `df1f2177`: self-play. All three phases on both seats: 12 picks, 2 lineups, 7 turns. The
  player-1 seat won.
- `a7cbacde`: against another agent. We won in 6 turns.
- `harness/replay.py` replays a recording through the bot and prints each decision next to the
  live one (`--why` lists each slot's best options and their pair scores):

```bash
.venv/bin/python -m bots.pokemon.harness.replay <recordings>/<session_id>/seat0.jsonl.gz --why
```

Replaying the first match through `b4ec5f0` gave the same 32 decisions as live. The fixed build
replays the second match unchanged, 14 of 14. What the payload showed:
- The draft's `available_cards` carry full sets: item, ability, nature, EVs and moves. `rosters`
  is keyed by seat id, which is `context.agent_id`.
- At Team Preview:
  - `your_roster` has our full sets.
  - `opponent_roster` has species, types and base stats only: `"unknown_item"`, no ability, no
    moves, and HP 0.
  - The bot uses draft memory for both sides, so this costs nothing.
- In battle:
  - `team`/`opponent_team` are keyed `"p1: Iron Hands"`.
  - `current_hp_fraction` is 0..1. The opponent's `current_hp`/`max_hp` are in percent.
  - Statuses are poke-env's names (`"FNT"`, `"BRN"`).
  - Spread and self moves list target `[0]`. Ally moves list `[-1]` or `[-2]`, the fixed board
    positions.
- **Bug found and fixed:** `fields`, `weather` and the side conditions are poke-env's maps, with
  the turn a condition started (or its layers) as the value. Rillaboom's Grassy Surge arrives as
  `{"GRASSY_TERRAIN": 0}`, and the parser dropped falsy values, so `b4ec5f0` missed any condition
  that started on turn 0. With terrain read correctly, the player-2 seat's first turn changes from
  Fake Out + Earthquake (halved by Grassy Terrain) to Wood Hammer + Wave Crash into Iron Hands.
  `tests/data/pokemon_live.json` holds five of the match's states (ids replaced), and the tests
  play them.
- **The pool.** Each match draws 18 cards from a larger list. The two pools shared 6 species, and
  a species' card was the same set in both, so each species seems to have one fixed card.
  - The cards are standard VGC sets (Life Orb Timid Thundurus, Assault Vest Iron Hands...).
  - Items repeat inside a pool: 10 and 11 distinct items among 18 cards. The draft enforces the
    Item Clause by leaving clashing cards out of the legal picks.
  - All 30 cards seen so far are in `data/live_cards.json`.

## Open questions

- **The full card list.** 30 cards have been seen so far. Adding each new match's
  `available_cards` to `data/live_cards.json` makes `--pool live` closer to the platform.
- **Draft tuning.** The weights (staples, denial 0.45) are untuned. The draft doesn't yet count
  the Item Clause: taking a Sitrus Berry card can lock us out of the other two.
- **Memory across a worker restart.** If the match's worker restarts mid-battle, the draft memory
  is gone and sets are guessed (252/252 spreads, STAB placeholder moves).
- **Not modelled yet:**
  - opponent switching and Protect prediction beyond a flat 20%;
  - Ally Switch, Commander, Perish trapping, items like Booster Energy timing;
  - multi-turn plans (Trick Room turns left, Tailwind turns left).
- **Tuning.** Battle-only results against max-damage (65%) have room to improve: switching is
  conservative, and setup moves are valued roughly.
