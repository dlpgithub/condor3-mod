#!/usr/bin/env bash
# Runs the post-processing chain headless under CrossOver and writes a BMP.
#   scripts/pfxtest.sh <capture.bin> <out.bmp> [samples] [hlsl]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
abs() { echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; }
CAP="$(abs "$1")"; OUT="$(abs "$2")"; S="${3:-4}"; HLSL="$(abs "${4:-$ROOT/postfx/postfx.hlsl}")"
GAME="${CONDOR_DIR:-$HOME/Condor3}"
W=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine
H="$HOME/.condor3_fx_harness"; mkdir -p "$H"
cp "$ROOT/build/pfxtest.exe" "$GAME/D3DCompiler_43.dll" "$H/"
cd "$H" && "$W" --bottle Condor3 "$H/pfxtest.exe" "$CAP" "$HLSL" "$OUT" "$S" 2>&1 | grep -E "^(capture|PfxCore|wrote|FAIL)" || true
cat "$H/pfxtest.log"
