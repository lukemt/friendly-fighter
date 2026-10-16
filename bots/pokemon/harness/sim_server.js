// Showdown battles for the Python harness (harness.py), over stdin/stdout JSON lines.
//
//   in:  {"id": "b1", "cmd": "start", "format": "gen9vgc2025regi", "seed": [1,2,3,4],
//         "p1": {"name": "A", "team": [sets]}, "p2": {...}}
//        {"id": "b1", "cmd": "write", "data": ">p1 move 1 2, switch 3"}
//        {"id": "b1", "cmd": "close"}
//   out: {"id": "b1", "chunk": "<one BattleStream output chunk>"}
//
//   NODE_PATH=/home/dev/showdown/node_modules node bots/pokemon/harness/sim_server.js
'use strict';
const readline = require('readline');
const {BattleStream, Teams} = require('pokemon-showdown');

const battles = new Map();
const send = obj => process.stdout.write(JSON.stringify(obj) + '\n');

async function pump(id, stream) {
  for await (const chunk of stream) send({id, chunk});
  send({id, closed: true});
}

const rl = readline.createInterface({input: process.stdin});
rl.on('line', line => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch (e) { send({error: `bad json: ${e}`}); return; }
  const {id, cmd} = msg;
  try {
    if (cmd === 'start') {
      const stream = new BattleStream();
      battles.set(id, stream);
      pump(id, stream).catch(e => send({id, error: String(e && e.stack || e)}));
      const spec = {formatid: msg.format || 'gen9vgc2025regi'};
      if (msg.seed) spec.seed = msg.seed;
      stream.write(`>start ${JSON.stringify(spec)}`);
      stream.write(`>player p1 ${JSON.stringify({name: msg.p1.name, team: Teams.pack(msg.p1.team)})}`);
      stream.write(`>player p2 ${JSON.stringify({name: msg.p2.name, team: Teams.pack(msg.p2.team)})}`);
    } else if (cmd === 'write') {
      battles.get(id).write(msg.data);
    } else if (cmd === 'close') {
      const s = battles.get(id);
      if (s) { s.writeEnd(); battles.delete(id); }
    }
  } catch (e) {
    send({id, error: String(e && e.stack || e)});
  }
});
rl.on('close', () => process.exit(0));
