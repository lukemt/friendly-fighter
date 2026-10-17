// Build a pool of draft cards for the local harness (bots/pokemon/data/sim_cards.json), shaped like
// the platform's cards: {card_id, species, item, ability, nature, level 50, evs, ivs, moves[4]}.
//
// The platform's real card list isn't public, so this stands in for it: Showdown's gen 9 random
// doubles sets, given competitive natures/EVs, kept only where gen9vgc2025regi's validator accepts
// the set (legal species, moves and items), one card per species.
//
//   NODE_PATH=/home/dev/showdown/node_modules node bots/pokemon/harness/make_cards.js [N_TEAMS]
'use strict';
const fs = require('fs');
const path = require('path');
const {Dex, Teams, TeamValidator} = require('pokemon-showdown');

const nTeams = Number(process.argv[2] || 400);
const validator = TeamValidator.get('gen9vgc2025regi');
const dex = Dex.forFormat('gen9vgc2025regi');
const cards = {};

function competitive(set) {
  const sp = dex.species.get(set.species);
  let phys = 0, spec = 0;
  for (const m of set.moves) {
    const mv = dex.moves.get(m);
    if (mv.category === 'Physical') phys++;
    else if (mv.category === 'Special') spec++;
  }
  const physical = phys >= spec;
  const fast = sp.baseStats.spe >= 80 && !set.moves.some(m => dex.moves.get(m).id === 'trickroom');
  const evs = {hp: 4, atk: 0, def: 0, spa: 0, spd: 0, spe: 0};
  if (phys + spec === 0 || (!fast && (phys + spec) <= 1)) {
    evs.hp = 252; evs.def = 128; evs.spd = 128;
  } else {
    evs[physical ? 'atk' : 'spa'] = 252;
    if (fast) evs.spe = 252; else evs.hp = 252;
  }
  let nature;
  if (fast) nature = physical ? 'Jolly' : 'Timid';
  else nature = physical ? 'Adamant' : 'Modest';
  if (set.moves.some(m => dex.moves.get(m).id === 'trickroom')) nature = physical ? 'Brave' : 'Quiet';
  const ivs = {};
  if (nature === 'Brave' || nature === 'Quiet') ivs.spe = 0;
  return {...set, level: 50, nature, evs, ivs};
}

for (let i = 0; i < nTeams; i++) {
  const team = Teams.generate('gen9randomdoublesbattle');  // unseeded: the file is generated once and committed
  for (const raw of team) {
    const id = dex.species.get(raw.species).id;
    if (cards[id]) continue;
    const set = competitive({species: raw.species, item: raw.item, ability: raw.ability,
      moves: raw.moves.map(m => dex.moves.get(m).name)});
    if (set.moves.length !== 4) continue;
    const problems = validator.validateSet({...set, name: set.species, teraType: undefined}, {});
    if (problems && problems.length) continue;
    const evs = {};
    for (const [k, v] of Object.entries(set.evs)) if (v) evs[k] = v;
    cards[id] = {card_id: `vgc-${dex.species.get(raw.species).id}`, species: dex.species.get(raw.species).name,
      item: set.item, ability: set.ability, nature: set.nature, level: 50, evs, ivs: set.ivs, moves: set.moves};
  }
}

const list = Object.values(cards).sort((a, b) => a.card_id.localeCompare(b.card_id));
const file = path.join(__dirname, '..', 'data', 'sim_cards.json');
fs.writeFileSync(file, JSON.stringify(list, null, 0));
console.log(`wrote ${list.length} cards to ${file}`);
