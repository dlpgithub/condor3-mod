#!/usr/bin/env bash
# Builds the Rust landscape compilers and copies them into bin/ (macOS, Linux; on Windows use scripts\build_rust.bat,
# or this script from Git Bash / MSYS2).
#   scripts/build_rust.sh            both: bin/tiles2condor_v2 and bin/tiles2condor
#   scripts/build_rust.sh v2         only tiles2condor_v2 (the current one)
#   scripts/build_rust.sh v1         only tiles2condor (the old one)
#   scripts/build_rust.sh all debug  debug build (slow programs, faster to compile)
# Needs the Rust toolchain (https://rustup.rs). No other system packages: the GPU code (wgpu) loads Metal / Vulkan / DX12 at run time.
# On Linux the v2 compiler wants a Vulkan driver at run time (mesa-vulkan-drivers or the GPU vendor's), else use its --cpu option.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHICH="${1:-all}"; PROFILE="${2:-release}"
case "$WHICH" in all|v1|v2) ;; *) echo "usage: $0 [all|v1|v2] [release|debug]"; exit 2 ;; esac
case "$PROFILE" in release|debug) ;; *) echo "usage: $0 [all|v1|v2] [release|debug]"; exit 2 ;; esac

CARGO="$(command -v cargo || true)"
[ -z "$CARGO" ] && [ -x "$HOME/.cargo/bin/cargo" ] && CARGO="$HOME/.cargo/bin/cargo"
if [ -z "$CARGO" ]; then
  echo "cargo not found. Install Rust from https://rustup.rs:"
  echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  exit 1
fi
EXE=""; case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) EXE=".exe" ;; esac
FLAG=""; [ "$PROFILE" = release ] && FLAG="--release"

mkdir -p "$ROOT/bin"
build() {   # build <crate folder> <program name>
  echo "== $2 ($PROFILE) =="
  (cd "$ROOT/rust/$1" && "$CARGO" build $FLAG)
  cp "$ROOT/rust/$1/target/$PROFILE/$2$EXE" "$ROOT/bin/$2$EXE"
  echo "-> bin/$2$EXE"
}
"$CARGO" --version
[ "$WHICH" = v1 ] || build tiles2condor_v2 tiles2condor_v2
[ "$WHICH" = v2 ] || build tiles2condor tiles2condor
echo "done. Usage: bin/tiles2condor_v2$EXE <export_dir> [...] <config.json> <Landscapes_dir>/<Name>   (see rust/tiles2condor_v2/README.md)"
