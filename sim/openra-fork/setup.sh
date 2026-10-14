#!/usr/bin/env bash
# Set up the local Red Alert platform's engine in ~/openra-sim (override with FF_SIM_HOME):
# the platform's own OpenRA fork (yxc20089/OpenRA at the commit get_game_config reports),
# patched with ff-sim.patch (two agents per game, engine verdicts, exact-tick orders,
# lobby options), built headless with .NET 8, plus the freeware Red Alert content.
# Then:  .venv/bin/pip install grpcio  &&  python scripts/ffsim.py run
# Idempotent. Linux or macOS; needs git, curl, make, python3.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${FF_SIM_HOME:-$HOME/openra-sim}
COMMIT=9b271c1a5562a07deeb1c3f81d9a556cd563a676
mkdir -p "$ROOT"

if [ ! -x "$ROOT/dotnet8/dotnet" ]; then
  curl -sSL https://dot.net/v1/dotnet-install.sh -o "$ROOT/dotnet-install.sh"
  bash "$ROOT/dotnet-install.sh" --channel 8.0 --install-dir "$ROOT/dotnet8"
fi
# The stepped engines of virtual time run on .NET 10 (the same games, ~10 % faster ticks); real time on 8.
if [ ! -d "$ROOT/dotnet10/shared/Microsoft.AspNetCore.App" ]; then
  [ -f "$ROOT/dotnet-install.sh" ] || curl -sSL https://dot.net/v1/dotnet-install.sh -o "$ROOT/dotnet-install.sh"
  bash "$ROOT/dotnet-install.sh" --runtime dotnet --channel 10.0 --install-dir "$ROOT/dotnet10" --no-path
  bash "$ROOT/dotnet-install.sh" --runtime aspnetcore --channel 10.0 --install-dir "$ROOT/dotnet10" --no-path
fi

if [ ! -d "$ROOT/fork/.git" ]; then
  mkdir -p "$ROOT/fork" && cd "$ROOT/fork"
  git init -q && git remote add origin https://github.com/yxc20089/OpenRA.git
  git fetch -q --depth 1 origin "$COMMIT" && git checkout -q FETCH_HEAD
  git apply "$HERE/ff-sim.patch"
fi

export PATH=$ROOT/dotnet8:$PATH DOTNET_ROOT=$ROOT/dotnet8 DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
export MSBUILDDISABLENODEREUSE=1 DOTNET_CLI_USE_MSBUILD_SERVER=0 UseSharedCompilation=false
(cd "$ROOT/fork" && make TARGETPLATFORM=unix-generic all 2>&1 | grep -E " error |Build succeeded")

# Freeware Red Alert content (OpenRA's quick-install package, SHA1-checked).
CONTENT=$ROOT/src/Support/Content/ra/v2
if [ ! -f "$CONTENT/allies.mix" ]; then
  mkdir -p "$ROOT/content-dl"
  ZIP=$ROOT/content-dl/ra-quickinstall.zip
  for url in $(curl -sL https://www.openra.net/packages/ra-quickinstall-mirrors.txt); do
    curl -sfL --max-time 300 -o "$ZIP" "$url" && break
  done
  echo "44241f68e69db9511db82cf83c174737ccda300b  $ZIP" | (sha1sum -c - 2>/dev/null || shasum -c -)
  python3 -I - "$ROOT" <<'PY'
import os, re, sys, zipfile
root = sys.argv[1]
fork = os.path.join(root, "fork")
mapping, inside = {}, False
for line in open(os.path.join(fork, "mods/ra-content/installer/downloads.yaml")).read().split("\n"):
    if line.startswith("quickinstall:"):
        inside = True
        continue
    if inside and line and not line.startswith("\t"):
        break
    m = re.match(r"\t\t\^SupportDir\|(.+?): (.+)$", line)
    if inside and m:
        mapping[m.group(2).strip()] = m.group(1).strip()
z = zipfile.ZipFile(os.path.join(root, "content-dl/ra-quickinstall.zip"))
for inner, dest in mapping.items():
    out = os.path.join(root, "src", "Support", dest)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with z.open(inner) as fi, open(out, "wb") as fo:
        fo.write(fi.read())
print(f"extracted {len(mapping)} content files")
PY
fi
echo "Ready: python scripts/ffsim.py run --seat1 ai:normal"
