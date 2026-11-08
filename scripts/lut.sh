#!/usr/bin/env bash
# Swap the colour grading LUT while the game runs (the mod polls <game>/postfx/lut.cube).
#   scripts/lut.sh list          candidate LUTs in luts_candidates/
#   scripts/lut.sh NAME [str]    use luts_candidates/NAME/NAME.cube (optionally set lut_strength, default 1.0)
#   scripts/lut.sh off           remove the LUT and set lut_strength 0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; GAME="${CONDOR_DIR:-$HOME/Condor3}"
case "${1:-}" in
  list) ls "$ROOT/luts_candidates" | grep -v zip ;;
  off) rm -f "$GAME/postfx/lut.cube"; "$ROOT/scripts/tune.sh" lut_strength 0 >/dev/null; echo "LUT off" ;;
  "") echo "usage: $0 list | NAME [strength] | off"; exit 2 ;;
  *) f="$ROOT/luts_candidates/$1/$1.cube"; [ -f "$f" ] || f="$1"
     cp "$f" "$GAME/postfx/lut.cube" && "$ROOT/scripts/tune.sh" lut_strength "${2:-1.0}" >/dev/null; echo "LUT: $1 strength ${2:-1.0}" ;;
esac
