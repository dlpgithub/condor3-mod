#!/usr/bin/env bash
# Live tuning of the post-processing while the game runs (the mod polls <game>/postfx/tune.txt).
#   scripts/tune.sh name value [name value ...]   set values (kept in tune.txt)
#   scripts/tune.sh reset                         remove all overrides (back to the defaults in postfx.hlsl)
#   scripts/tune.sh show                          print tune.txt
GAME="${CONDOR_DIR:-$HOME/Condor3}"; F="$GAME/postfx/tune.txt"
touch "$F"
case "${1:-}" in
  reset) : > "$F"; echo "tune.txt cleared" ;;
  show) cat "$F" ;;
  "") echo "usage: $0 name value ... | reset | show"; exit 2 ;;
  *) while [ $# -ge 2 ]; do
       grep -v -i -E "^$1[[:space:]]" "$F" > "$F.tmp"; echo "$1 $2" >> "$F.tmp"; mv "$F.tmp" "$F"; shift 2
     done; echo "ok:"; cat "$F" ;;
esac
