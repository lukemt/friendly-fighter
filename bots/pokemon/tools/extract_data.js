// Extract the parts of Pokémon Showdown's gen 9 data the bot needs into one compact JSON file
// (bots/pokemon/data/dex.json). The bot is pure Python and its release is a read-only git
// snapshot with no node_modules, so the data is committed.
//
//   NODE_PATH=/home/dev/showdown/node_modules node bots/pokemon/tools/extract_data.js
//
// (npm install pokemon-showdown@0.11.11 in /home/dev/showdown first; that version knows
// gen9vgc2025regi.)
'use strict';
const fs = require('fs');
const path = require('path');
const {Dex} = require('pokemon-showdown');

const dex = Dex.forGen(9);
const out = {source: `pokemon-showdown ${require('pokemon-showdown/package.json').version}`,
  species: {}, moves: {}, types: {}, items: {}, abilities: {}, natures: {}};

for (const s of dex.species.all()) {
  if (s.isNonstandard && s.isNonstandard !== 'Unobtainable') continue;
  out.species[s.id] = {
    name: s.name, types: s.types, bs: s.baseStats, abilities: Object.values(s.abilities),
    weight: s.weightkg,
  };
}

const keepFlags = ['contact', 'sound', 'punch', 'bite', 'pulse', 'slicing', 'protect', 'heal', 'wind', 'bullet'];
for (const m of dex.moves.all()) {
  if (m.isNonstandard && m.isNonstandard !== 'Unobtainable') continue;
  if (m.isZ || m.isMax) continue;
  const e = {
    name: m.name, type: m.type, cat: m.category, bp: m.basePower,
    acc: m.accuracy === true ? 0 : m.accuracy, pri: m.priority, target: m.target,
  };
  const flags = keepFlags.filter(f => m.flags[f]);
  if (flags.length) e.flags = flags;
  if (m.multihit) e.multihit = m.multihit;
  if (m.drain) e.drain = m.drain[0] / m.drain[1];
  if (m.recoil) e.recoil = m.recoil[0] / m.recoil[1];
  if (m.heal) e.heal = m.heal[0] / m.heal[1];
  if (m.willCrit) e.crit = 1;
  if (m.critRatio && m.critRatio > 1) e.critRatio = m.critRatio;
  if (m.boosts) e.boosts = m.boosts;
  if (m.self && m.self.boosts) e.selfBoosts = m.self.boosts;
  if (m.status) e.status = m.status;
  if (m.volatileStatus) e.volatile = m.volatileStatus;
  if (m.sideCondition) e.side = m.sideCondition;
  if (m.weather) e.weather = m.weather;
  if (m.terrain) e.terrain = m.terrain;
  if (m.pseudoWeather) e.pseudo = m.pseudoWeather;
  if (m.selfSwitch) e.selfSwitch = 1;
  if (m.ohko) e.ohko = 1;
  if (m.overrideOffensiveStat) e.offStat = m.overrideOffensiveStat;
  if (m.overrideDefensiveStat) e.defStat = m.overrideDefensiveStat;
  if (m.overrideOffensivePokemon) e.offMon = m.overrideOffensivePokemon;
  if (m.flags.charge) e.charge = 1;
  if (m.self && m.self.volatileStatus === 'mustrecharge') e.recharge = 1;
  if (m.selfdestruct) e.selfdestruct = 1;
  const sec = m.secondary || (m.secondaries && m.secondaries[0]);
  if (sec) {
    e.sec = {chance: sec.chance || 100};
    if (sec.status) e.sec.status = sec.status;
    if (sec.volatileStatus) e.sec.volatile = sec.volatileStatus;
    if (sec.boosts) e.sec.boosts = sec.boosts;
    if (sec.self && sec.self.boosts) e.sec.selfBoosts = sec.self.boosts;
  }
  if (m.damage) e.fixed = m.damage;
  out.moves[m.id] = e;
}

for (const t of dex.types.all()) {
  if (t.isNonstandard) continue;
  // damageTaken[attackingType]: 0 normal, 1 weak, 2 resist, 3 immune
  const taken = {};
  for (const [atk, v] of Object.entries(t.damageTaken)) {
    if (dex.types.get(atk).exists) taken[atk] = [1, 2, 0.5, 0][v];
  }
  out.types[t.name] = taken;
}

for (const i of dex.items.all()) {
  if (i.isNonstandard && i.isNonstandard !== 'Unobtainable') continue;
  out.items[i.id] = {name: i.name};
}
for (const a of dex.abilities.all()) {
  if (a.isNonstandard && a.isNonstandard !== 'Unobtainable') continue;
  out.abilities[a.id] = {name: a.name};
}
for (const n of dex.natures.all()) {
  out.natures[n.id] = {name: n.name, plus: n.plus || null, minus: n.minus || null};
}

const file = path.join(__dirname, '..', 'data', 'dex.json');
fs.mkdirSync(path.dirname(file), {recursive: true});
fs.writeFileSync(file, JSON.stringify(out));
console.log(`wrote ${file}: ${Object.keys(out.species).length} species, ${Object.keys(out.moves).length} moves, ` +
  `${fs.statSync(file).size} bytes`);
