#!/usr/bin/env bash
# One OpenRA AI-vs-AI match on a virtual display: aimatch.sh <map.oramap> <botA> <botB> <display> [log]
set -e
MAP=${1:-sidestep.oramap}; A=${2:-normal}; B=${3:-rush}; D=${4:-99}; LOG=${5:-$HOME/openra-sim/aimatch-$D.log}
pgrep -f "Xvfb :$D " >/dev/null || { Xvfb :$D -screen 0 1024x768x24 -nolisten tcp >/dev/null 2>&1 & sleep 1; }
export LP_NUM_THREADS=1 DISPLAY=:$D LIBGL_ALWAYS_SOFTWARE=1 PATH=$HOME/openra-sim/dotnet:$PATH DOTNET_ROOT=$HOME/openra-sim/dotnet
export OPENRA_AI_MATCH="$A,$B"
cd $HOME/openra-sim/src
exec ./launch-game.sh Game.Mod=ra Launch.Map="$MAP" Graphics.CapFramerate=True Graphics.MaxFramerate=2 \
  Sound.Mute=True Graphics.Mode=Windowed > "$LOG" 2>&1
