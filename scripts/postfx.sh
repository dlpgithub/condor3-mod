#!/usr/bin/env bash
# Installs / removes the post-processing build in the Condor 3 folder.
#   scripts/postfx.sh install     backs up the current d3dx.dll, installs build/d3dx.dll + postfx/postfx.hlsl
#   scripts/postfx.sh uninstall   restores the backed-up d3dx.dll and removes <game>/postfx
#   scripts/postfx.sh sync        copies only postfx.hlsl (the running game hot-reloads it)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GAME="${CONDOR_DIR:-$HOME/Condor3}"
BAK="$GAME/d3dx.dll.before_postfx"
case "${1:-}" in
  install)
    [ -f "$ROOT/build/d3dx.dll" ] || { echo "build first: scripts/build_docker.sh [Debug]"; exit 1; }
    [ -f "$BAK" ] || cp "$GAME/d3dx.dll" "$BAK"
    cp "$ROOT/build/d3dx.dll" "$GAME/d3dx.dll"
    mkdir -p "$GAME/postfx"; cp "$ROOT/postfx/postfx.hlsl" "$GAME/postfx/"
    [ -f "$GAME/postfx/lut.cube" ] || cp "$ROOT/postfx/default_lut/lut.cube" "$GAME/postfx/lut.cube"
    echo "Installed. Start Condor normally.  F9 = effect on/off, F8 = reload shader, F10 = capture frame."
    echo "Log: $GAME/postfx/postfx.log   Remove again: scripts/postfx.sh uninstall" ;;
  uninstall)
    [ -f "$BAK" ] && mv "$BAK" "$GAME/d3dx.dll"
    rm -rf "$GAME/postfx"
    echo "Restored the previous d3dx.dll, removed $GAME/postfx" ;;
  sync)
    cp "$ROOT/postfx/postfx.hlsl" "$GAME/postfx/" && echo "postfx.hlsl copied" ;;
  *) echo "usage: $0 install|uninstall|sync"; exit 2 ;;
esac
