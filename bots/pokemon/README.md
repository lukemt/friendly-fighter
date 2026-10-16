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
```

Baselines (`harness/baselines.py`):
- `random`: a random legal card, lineup and slot choice.
- `maxdamage`: drafts and brings the highest base-stat totals. Each turn it uses the move with the
  most base power x STAB x type effectiveness on the best foe, and switches only when forced.
- `smoke`: the first legal option.

`bot+bstdraft` is our battle play with max-damage's draft and lineup. It measures battle skill alone.

## Results

All results are on harness seeds 1000-1199: 200 games each, seats alternating.

| Bot | Opponent | Games | Wins | Losses | Win rate | Refused answers |
|---|---|---|---|---|---|---|
| bot | random | 100 | 100 | 0 | 100% | 0 |
| bot | maxdamage | 200 | 149 | 51 | 74.5% | 0 |
| bot+bstdraft (battle alone) | maxdamage | 200 | 130 | 70 | 65% | 0 |
| bot+bstdraft (battle alone) | random | 200 | 200 | 0 | 100% | 0 |

- **Speed:** a mean of about 50 ms a decision, with a maximum of 0.5 s (a full draft pick on a
  2-CPU container running two games at once).
- **Battle length:** about 4 turns. Level-50 random-doubles sets with full attack EVs hit hard.

## Open questions

- **The live payload.** The observation and template shapes come from the platform guide and the
  repository's fixtures; no live Pokémon game has been recorded yet. The bot reads every field
  defensively and falls back to legal play, but a test match (its recording in the runtime's
  `recordings/`) should be checked:
  - Do `your_roster`/`opponent_roster` carry moves and items?
  - Are `team`/`opponent_team` keyed as `"p1: Name"`?
  - Is `current_hp_fraction` 0..1?
  - Are weather and field names spelled as the code expects?
- **The real card pool.** The harness pool is a stand-in. Draft weights (staples, denial 0.45)
  should be re-tuned on real pools once games are recorded.
- **Memory across a worker restart.** If the match's worker restarts mid-battle, the draft memory
  is gone and sets are guessed (252/252 spreads, STAB placeholder moves).
- **Not modelled yet:**
  - opponent switching and Protect prediction beyond a flat 20%;
  - Ally Switch, Commander, Perish trapping, items like Booster Energy timing;
  - multi-turn plans (Trick Room turns left, Tailwind turns left).
- **Tuning.** Battle-only results against max-damage (65%) have room to improve: switching is
  conservative, and setup moves are valued roughly.
