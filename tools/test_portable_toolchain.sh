#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build extract-xiso and the public runtime tests with the portable zig
# toolchain while no compiler, linker, CMake, Ninja, make or pkg-config from the
# host is on PATH, as on SteamOS, then run the tests. Run
# `DOAXBV_TOOLCHAIN=zig ./build_game.sh --help` first to install the tools.
# The optional argument is the build folder (default build/portable).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_BIN="${ROOT}/private/venv/bin"
BUILD="${1:-${ROOT}/build/portable}"
cd "${ROOT}"

SHIM="$(mktemp -d)"
trap 'rm -rf "${SHIM}"' EXIT
for tool in /usr/bin/*; do
    case "$(basename "${tool}")" in
        gcc* | g++* | cc | c++ | c89* | c99* | clang* | cpp* | ld | ld.* | as | ar | ranlib | \
            cmake | ctest | cpack | ninja | make | gmake | pkg-config | pkgconf | \
            x86_64-linux-gnu-* | lld* | llvm-* | tcc) continue ;;
    esac
    ln -s "${tool}" "${SHIM}/$(basename "${tool}")"
done
export PATH="${VENV_BIN}:${SHIM}" DOAXBV_TOOLCHAIN=zig
for tool in cc c++ gcc clang make pkg-config; do
    if found="$(command -v "${tool}")"; then
        echo "${tool} is still on PATH at ${found}" >&2
        exit 1
    fi
done

mkdir -p private/portable-xiso
"${VENV_BIN}/python3" -I -c "
import sys; sys.path.insert(0, 'tools')
from pathlib import Path
import build_prerequisites as p
prepared = p.prepare_portable_toolchain(Path.cwd())
print(f\"Portable toolchain: {prepared['target']} with the {prepared['sdl3']} SDL3\")
p.ensure_extract_xiso(Path.cwd(), Path('private/portable-xiso'))
Path(sys.argv[1]).write_text('\n'.join(prepared['cmake_args']) + '\n')" "${SHIM}/cmake-args"
mapfile -t ARGS < "${SHIM}/cmake-args"
"$(ls -d tools/extract-xiso/*/build/extract-xiso | head -n 1)" -h 2>&1 | grep extract-xiso

rm -rf "${BUILD}"
cmake -S recomp-runtime -B "${BUILD}" -G Ninja -DCMAKE_BUILD_TYPE=Debug "${ARGS[@]}"
# zig must compile against its own libc headers, never the host's.
if grep -n '/usr/include' "${BUILD}/build.ninja"; then
    echo "The build reads host headers from /usr/include." >&2
    exit 1
fi
cmake --build "${BUILD}" --parallel 4
ctest --test-dir "${BUILD}" --output-on-failure
