#!/usr/bin/env bash
# Builds d3dx.dll (+ pfxtest.exe, monitor) inside the dev container image "c3dev".
#   scripts/build_docker.sh            Release
#   scripts/build_docker.sh Debug      Debug (runtime logging to the monitor)
set -euo pipefail
cd "$(dirname "$0")/.."
TYPE="${1:-Release}"
docker image inspect c3dev >/dev/null 2>&1 || docker build -t c3dev .devcontainer
docker run --rm -v "$PWD":/workspace c3dev bash -lc "cd /workspace && bash scripts/build_dobby.sh && cmake -B build -DCMAKE_TOOLCHAIN_FILE=cmake/mingw-toolchain.cmake -DCMAKE_BUILD_TYPE=$TYPE && cmake --build build -j4"
