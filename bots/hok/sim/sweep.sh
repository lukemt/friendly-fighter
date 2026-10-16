#!/bin/sh
# Knob variants of ff-hok against one opponent on the sim: one compare per variant.
#   sh bots/hok/sim/sweep.sh <opponent> <mode> <games> <seed> '<knobs json>' ['<knobs json>' ...]
set -eu
opp=$1; mode=$2; games=$3; seed=$4; shift 4
for knobs in "$@"; do
  echo "== ff-hok:$knobs"
  .venv/bin/python -m bots.hok.sim.compare --a "ff-hok:$knobs" --b "$opp" --games "$games" --seed "$seed" --mode "$mode" | tail -1
done
