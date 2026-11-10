#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the pinned SDL3 into tools/sdl3-linux for hosts that build the game
# with zig (SteamOS): zig has no X11 or Wayland headers, so SDL3 is built once
# where they exist and shipped in the Linux release ZIP. SDL loads X11,
# Wayland, PipeWire, PulseAudio and ALSA at run time, so the library needs only
# glibc.
#
# Run it on any x86-64 Linux with SDL3's build dependencies from
# docs/building.md, after `DOAXBV_TOOLCHAIN=zig ./build_game.sh --help` has
# installed zig into private/venv. It compiles with that zig toolchain against
# zig's glibc 2.31 headers, reading only the X11 and Wayland headers from
# /usr/include, so the library runs on SteamOS whatever glibc built it.
# Extra arguments go to SDL's CMake configure.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=3.4.12
SHA256=f07b958a9ac5020fb7a44cadb957f658b2149c3c8abb4f63145fac9303249db7  # as recomp-runtime/CMakeLists.txt
NEWEST_GLIBC=2.31  # PORTABLE_GLIBC in tools/build_prerequisites.py
PREFIX="${ROOT}/tools/sdl3-linux"
TOOLCHAIN="${ROOT}/private/portable-toolchain"
if [[ -e "${PREFIX}" ]]; then
    echo "${PREFIX} exists; delete it to rebuild." >&2
    exit 1
fi
if ! "${ROOT}/private/venv/bin/python3" -I -c "import ziglang" 2>/dev/null; then
    echo "Run DOAXBV_TOOLCHAIN=zig ./build_game.sh --help first; it installs zig into private/venv." >&2
    exit 1
fi
"${ROOT}/private/venv/bin/python3" -I -c "import sys; sys.path.insert(0, sys.argv[1]); \
from pathlib import Path; from build_prerequisites import write_portable_toolchain; \
write_portable_toolchain(Path(sys.argv[2]))" "${ROOT}/tools" "${ROOT}"
mkdir -p "${ROOT}/private"
WORK="$(mktemp -d "${ROOT}/private/sdl3-build-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
curl -fsSL -o "${WORK}/sdl.tar.gz" \
    "https://github.com/libsdl-org/SDL/releases/download/release-${VERSION}/SDL3-${VERSION}.tar.gz"
echo "${SHA256}  ${WORK}/sdl.tar.gz" | sha256sum -c --quiet
tar -xzf "${WORK}/sdl.tar.gz" -C "${WORK}"
GENERATOR="Unix Makefiles"
command -v ninja >/dev/null 2>&1 && GENERATOR=Ninja
# -idirafter: zig's glibc headers win; /usr/include supplies only what zig lacks.
# zig reports no multiarch folder, so name Debian's for SDL's library sonames.
cmake -S "${WORK}/SDL3-${VERSION}" -B "${WORK}/build" -G "${GENERATOR}" \
    -DCMAKE_C_COMPILER="${TOOLCHAIN}/cc" -DCMAKE_CXX_COMPILER="${TOOLCHAIN}/c++" \
    -DCMAKE_AR="${TOOLCHAIN}/ar" -DCMAKE_RANLIB="${TOOLCHAIN}/ranlib" \
    -DCMAKE_C_FLAGS="-idirafter /usr/include" -DCMAKE_CXX_FLAGS="-idirafter /usr/include" \
    -DCMAKE_LIBRARY_ARCHITECTURE=x86_64-linux-gnu \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="${WORK}/install" -DCMAKE_INSTALL_LIBDIR=lib \
    -DSDL_SHARED=ON -DSDL_STATIC=OFF -DSDL_TEST_LIBRARY=OFF -DSDL_EXAMPLES=OFF -DSDL_TESTS=OFF \
    -DSDL_DEPS_SHARED=ON "$@"
cmake --build "${WORK}/build" --parallel
cmake --install "${WORK}/build"
LIBRARY="${WORK}/install/lib/libSDL3.so.0"
# Anything beyond glibc and the dynamic loader would fail on a host without it.
if readelf -d "${LIBRARY}" | grep NEEDED | grep -vE 'lib(c|m|dl|pthread|rt)\.so|ld-linux'; then
    echo "libSDL3.so.0 links a library beyond glibc; see the NEEDED lines above." >&2
    exit 1
fi
newest="$(readelf -V "${LIBRARY}" | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sed 's/GLIBC_//' | sort -V | tail -1)"
if [[ "$(printf '%s\n%s\n' "${newest}" "${NEWEST_GLIBC}" | sort -V | tail -1)" != "${NEWEST_GLIBC}" ]]; then
    echo "libSDL3.so.0 needs glibc ${newest}, newer than ${NEWEST_GLIBC}." >&2
    exit 1
fi
mv "${WORK}/install" "${PREFIX}"
echo "SDL3 ${VERSION} installed in ${PREFIX} (needs glibc ${newest})"
