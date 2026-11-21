#!/usr/bin/env bash
# Builds d3dx.dll (+ pfxtest.exe, monitor) and the 64-bit autogen helper build/c3helper.exe (git submodule external/condor3-helper)
# inside the dev container image "c3dev".
#   scripts/build_docker.sh            Release
#   scripts/build_docker.sh Debug      Debug (runtime logging to the monitor)
set -euo pipefail
cd "$(dirname "$0")/.."
TYPE="${1:-Release}"
docker image inspect c3dev >/dev/null 2>&1 || docker build -t c3dev .devcontainer
[ -f external/condor3-helper/CMakeLists.txt ] || git submodule update --init external/condor3-helper
docker run --rm -v "$PWD":/workspace c3dev bash -lc "cd /workspace && bash scripts/build_dobby.sh && cmake -B build -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-toolchain.cmake -DCMAKE_BUILD_TYPE=$TYPE && cmake --build build -j4 \
  && cmake -S external/condor3-helper -B build/helper -DCMAKE_TOOLCHAIN_FILE=/workspace/external/condor3-helper/cmake/mingw64-toolchain.cmake -DCMAKE_BUILD_TYPE=Release >/dev/null \
  && cmake --build build/helper -j4 && cp build/helper/c3helper.exe build/c3helper.exe"
