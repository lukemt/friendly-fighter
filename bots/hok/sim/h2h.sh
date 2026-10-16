#!/bin/sh
# Variants of ff-hok head to head against the current bot (and upstream's rules) on the sim.
#   sh bots/hok/sim/h2h.sh <mode> <games> <seed> '<spec>' ['<spec>' ...]   (spec: ff-hok@pace:{knobs})
set -eu
mode=$1; games=$2; seed=$3; shift 3
for spec in "$@"; do
  echo "== $spec"
  .venv/bin/python -m bots.hok.sim.compare --a "$spec" --b "ff-hok,upstream" --games "$games" --seed "$seed" --mode "$mode" | tail -2
done
