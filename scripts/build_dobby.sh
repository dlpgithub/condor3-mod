#!/usr/bin/env bash
# Builds Dobby once (i686 MinGW, same toolchain file as the main project)
# and stores libdobby.a in deps/dobby-prebuilt/.
#   scripts/build_dobby.sh           -> rebuild only if fork/flags/compiler changed
#   scripts/build_dobby.sh --force   -> rebuild unconditionally
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/external/dobby-fork"
OUT="$ROOT/deps/dobby-prebuilt"
BUILD="$ROOT/deps/.dobby-build"
BUILD_TYPE="${BUILD_TYPE:-Release}"
TOOLCHAIN="${CMAKE_TOOLCHAIN_FILE:-$ROOT/cmake/mingw-toolchain.cmake}"
CXX_BIN="i686-w64-mingw32-g++"

# Must match add_compile_definitions() in the main CMakeLists.txt
EXTRA_FLAGS="-D_WIN32_WINNT=0x0600 -DWINVER=0x0600 ${DOBBY_EXTRA_FLAGS:-}"

[[ -d "$SRC" && -n "$(ls -A "$SRC")" ]] || {
  echo "Dobby fork missing. Run: git submodule update --init --recursive" >&2; exit 1; }

fingerprint() {
  {
    (cd "$SRC" && find . -type f \( -name '*.c' -o -name '*.cc' -o -name '*.cpp' \
        -o -name '*.h' -o -name '*.hpp' -o -name '*.S' -o -name '*.asm' \
        -o -name 'CMakeLists.txt' -o -name '*.cmake' \) -print0 \
        | sort -z | xargs -0 sha256sum)
    sha256sum "$TOOLCHAIN"
    echo "$BUILD_TYPE|$EXTRA_FLAGS"
    "$CXX_BIN" --version | head -n1
  } | sha256sum | cut -d' ' -f1
}

FP="$(fingerprint)"
if [[ "${1:-}" != "--force" && -f "$OUT/libdobby.a" && -f "$OUT/stamp" \
      && "$(cat "$OUT/stamp")" == "$FP" ]]; then
  echo "Dobby prebuilt is up to date: $OUT/libdobby.a"
  exit 0
fi

rm -rf "$BUILD"
cmake -S "$SRC" -B "$BUILD" -G "Unix Makefiles" \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DCMAKE_C_FLAGS="$EXTRA_FLAGS" \
  -DCMAKE_CXX_FLAGS="$EXTRA_FLAGS" \
  -DDOBBY_BUILD_SHARED_LIBRARY=OFF
cmake --build "$BUILD" --target dobby_static -j"$(nproc)"

echo "Static libs produced:"; find "$BUILD" -name '*.a'
LIB="$(find "$BUILD" -name 'libdobby*.a' | head -n1)"
[[ -n "$LIB" ]] || { echo "libdobby*.a not found in $BUILD" >&2; exit 1; }

mkdir -p "$OUT"
cp "$LIB" "$OUT/libdobby.a"
echo "$FP" > "$OUT/stamp"
echo "Done: $OUT/libdobby.a"