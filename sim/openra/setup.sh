#!/usr/bin/env bash
# Set up a local OpenRA (Red Alert) for friendly-fighter's simulator in ~/openra-sim:
# .NET 6 SDK, OpenRA release-20250330 built from source with aimatch.patch, and the
# freeware Red Alert content (OpenRA's own quick-install package, SHA1-checked).
# Debian/Ubuntu: needs sudo for the native libraries. Idempotent.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$HOME/openra-sim
mkdir -p "$ROOT"

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  liblua5.1-0 libsdl2-2.0-0 libopenal1 libfreetype6 'libicu7*' xvfb xauth libgl1-mesa-dri libgl1 libegl1 make unzip curl

if [ ! -x "$ROOT/dotnet/dotnet" ]; then
  curl -sSL https://dot.net/v1/dotnet-install.sh -o "$ROOT/dotnet-install.sh"
  bash "$ROOT/dotnet-install.sh" --channel 6.0 --install-dir "$ROOT/dotnet"
fi

if [ ! -d "$ROOT/src" ]; then
  git clone -q --depth 1 --branch release-20250330 https://github.com/OpenRA/OpenRA.git "$ROOT/src"
  (cd "$ROOT/src" && git apply "$HERE/aimatch.patch")
fi
cp "$HERE/build.sh" "$HERE/aimatch.sh" "$ROOT/" && chmod +x "$ROOT"/*.sh
"$ROOT/build.sh"

CONTENT=$ROOT/src/Support/Content/ra/v2
if [ ! -f "$CONTENT/allies.mix" ]; then
  mkdir -p "$ROOT/content-dl"
  ZIP=$ROOT/content-dl/ra-quickinstall.zip
  for url in $(curl -sL https://www.openra.net/packages/ra-quickinstall-mirrors.txt); do
    curl -sfL --max-time 300 -o "$ZIP" "$url" && break
  done
  echo "44241f68e69db9511db82cf83c174737ccda300b  $ZIP" | sha1sum -c -
  python3 -I - "$ROOT" <<'PY'
import os, re, sys, zipfile
root = sys.argv[1]
src = os.path.join(root, "src")
mapping, inside = {}, False
for line in open(os.path.join(src, "mods/ra-content/installer/downloads.yaml")).read().split("\n"):
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
    out = os.path.join(src, "Support", dest)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with z.open(inner) as fi, open(out, "wb") as fo:
        fo.write(fi.read())
print(f"extracted {len(mapping)} content files")
PY
fi
echo "Ready. Try: $ROOT/aimatch.sh sidestep.oramap normal rush 99"
