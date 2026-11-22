#!/usr/bin/env bash
# Packs the built files into a release zip: dist/condor3-mod-<date>-<commit>-<platform>.zip   (macOS, Linux, Windows via WSL)
#
# Build first (this script only collects, it does not build):
#   scripts/build_docker.sh                   -> build/d3dx.dll, build/c3helper.exe
#   python3 shaders/build_shaders.py build    -> shaders/build/Effects/fx/*.fx
#   scripts/build_rust.sh v2                  -> bin/tiles2condor_v2       (on Windows: scripts\build_rust.bat v2 -> .exe)
# The landscape compiler is a native program: the zip gets the one in bin/ and is named after its platform
# (windows / macos-x86_64 / macos-arm64 / linux-x86_64 ...). Everything else is the same for every platform.
#
#   scripts/package_release.sh              build the zip
#   scripts/package_release.sh --dir-only   only the folder dist/condor3-mod-..., no zip
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DIRONLY=0; [ "${1:-}" = "--dir-only" ] && DIRONLY=1

# ---- what must be there -------------------------------------------------------------------------------------------------------
missing=0
need() { [ -e "$1" ] || { echo "missing: $1   -> $2"; missing=1; }; }
need build/d3dx.dll        "scripts/build_docker.sh"
need build/c3helper.exe    "scripts/build_docker.sh"
ls shaders/build/Effects/fx/*.fx >/dev/null 2>&1 || { echo "missing: shaders/build/Effects/fx/*.fx   -> python3 shaders/build_shaders.py build"; missing=1; }
COMPILER=""
for f in bin/tiles2condor_v2.exe bin/tiles2condor_v2; do [ -f "$f" ] && { COMPILER="$f"; break; }; done
[ -n "$COMPILER" ] || { echo "missing: bin/tiles2condor_v2   -> scripts/build_rust.sh v2   (Windows: scripts\\build_rust.bat v2)"; missing=1; }
[ $missing = 0 ] || { echo "Build the missing parts first, then run this again."; exit 1; }

# ---- warn when a build is older than its sources --------------------------------------------------------------------------------
newest() {   # newest source file below the given folders
  local n="" f
  while IFS= read -r -d '' f; do { [ -z "$n" ] || [ "$f" -nt "$n" ]; } && n="$f"; done < <(find "$@" -type f \( -name '*.c' -o -name '*.h' \
      -o -name '*.rs' -o -name '*.wgsl' -o -name '*.fx' -o -name '*.hlsl' -o -name '*.py' -o -name '*.json' \) -print0 2>/dev/null)
  echo "$n"
}
older() {   # older <built file> <source dirs...>
  local b="$1"; shift; local n; n="$(newest "$@")"
  [ -n "$n" ] && [ "$n" -nt "$b" ] && echo "warning: $b is older than $n - rebuild?"
  return 0
}
older build/d3dx.dll src external/condor3-helper/include
older build/c3helper.exe external/condor3-helper/src external/condor3-helper/include
older "$COMPILER" rust/tiles2condor_v2/src
older "$(ls -t shaders/build/Effects/fx/*.fx | head -1)" shaders/orig shaders/hlsl shaders/options.json shaders/patches.py

# ---- name: date, commit, platform of the compiler ------------------------------------------------------------------------------
if command -v file >/dev/null; then INFO="$(file -b "$COMPILER")"
else case "$COMPILER" in *.exe) INFO="PE32+ (MS Windows)" ;; *) INFO="$(uname -s) $(uname -m)" ;; esac; fi
case "$INFO" in
  *PE32+*|*PE32*|*"MS Windows"*) PLAT=windows ;;
  *Mach-O*arm64*)  PLAT=macos-arm64 ;;
  *Mach-O*x86_64*) PLAT=macos-x86_64 ;;
  *Mach-O*|Darwin*) PLAT=macos ;;
  *ELF*aarch64*|*ELF*ARM*) PLAT=linux-arm64 ;;
  *ELF*|Linux*)    PLAT=linux-x86_64 ;;
  *)               PLAT=unknown ;;
esac
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
git diff --quiet HEAD -- 2>/dev/null || COMMIT="$COMMIT-modified"
HELPER="$(git -C external/condor3-helper rev-parse --short HEAD 2>/dev/null || echo ?)"
NAME="condor3-mod-$(date +%Y%m%d)-$COMMIT-$PLAT"
OUT="$ROOT/dist/$NAME"
rm -rf "$OUT" "$OUT.zip"; mkdir -p "$OUT"

# ---- collect (same layout as the repository, so the install scripts work unchanged) ----------------------------------------
put() { mkdir -p "$OUT/$(dirname "$2")"; cp "$1" "$OUT/$2"; }
put README.md                           README.md
put README_POSTFX.md                    README_POSTFX.md
put build/d3dx.dll                       build/d3dx.dll
put build/c3helper.exe                   build/c3helper.exe
put postfx/postfx.hlsl                   postfx/postfx.hlsl
put postfx/default_lut/lut.cube          postfx/default_lut/lut.cube
put postfx/default_lut/ATTRIBUTION.txt   postfx/default_lut/ATTRIBUTION.txt
[ -f postfx/user/lut.cube ] && put postfx/user/lut.cube postfx/user/lut.cube
[ -f postfx/user/tune.txt ] && put postfx/user/tune.txt postfx/user/tune.txt
for f in shaders/build/Effects/fx/*.fx; do put "$f" "$f"; done
put scripts/postfx.bat                   scripts/postfx.bat
put scripts/postfx.sh                    scripts/postfx.sh
put scripts/tune.sh                      scripts/tune.sh
put "$COMPILER"                          "$COMPILER"
for f in examples/*.json; do put "$f" "$f"; done
for f in assets/runway/*; do put "$f" "$f"; done
put docs/TILES2CONDOR.md                 docs/TILES2CONDOR.md
put external/dobby-fork/LICENSE          licenses/Dobby-LICENSE.txt
chmod +x "$OUT/scripts/"*.sh "$OUT/$COMPILER"
# Windows batch files need CRLF line endings
for f in "$OUT"/scripts/*.bat; do perl -pi -e 's/\r?\n/\r\n/' "$f"; done
cat > "$OUT/VERSION.txt" <<EOF
$NAME
built from condor3-mod $COMMIT, condor3-helper $HELPER, on $(date '+%Y-%m-%d %H:%M')
landscape compiler: $COMPILER ($INFO)
for Condor 3.1.0
EOF

# ---- zip ---------------------------------------------------------------------------------------------------------------------
if [ $DIRONLY = 1 ]; then echo "folder: dist/$NAME"; exit 0; fi
cd "$ROOT/dist"
if command -v zip >/dev/null; then zip -qr "$NAME.zip" "$NAME"
else python3 - "$NAME" <<'PY'
import os, sys, zipfile
name = sys.argv[1]
with zipfile.ZipFile(name + ".zip", "w", zipfile.ZIP_DEFLATED) as z:
    for d, _, files in os.walk(name):
        for f in sorted(files): z.write(os.path.join(d, f))   # keeps the unix permissions (executable scripts)
PY
fi
rm -rf "$NAME"
echo "dist/$NAME.zip ($(du -h "$NAME.zip" | cut -f1))"
