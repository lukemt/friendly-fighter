#!/usr/bin/env bash
# Build OpenRA without leaving MSBuild/compiler servers running.
set -e
export PATH=$HOME/openra-sim/dotnet:$PATH DOTNET_ROOT=$HOME/openra-sim/dotnet DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
export MSBUILDDISABLENODEREUSE=1 DOTNET_CLI_USE_MSBUILD_SERVER=0 UseSharedCompilation=false
cd $HOME/openra-sim/src && make TARGETPLATFORM=unix-generic all 2>&1 | grep -E " error |Build succeeded"
