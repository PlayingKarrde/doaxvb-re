# SPDX-License-Identifier: GPL-3.0-or-later
# Sourced by build_game.sh and launcher.sh: prepare private/venv with Capstone
# and, on hosts without a compiler or CMake, the portable zig toolchain. The
# caller sets DOAXBV_ROOT; this sets PYTHON and exports DOAXBV_TOOLCHAIN.
VENV="${DOAXBV_ROOT}/private/venv"
PYTHON="${VENV}/bin/python3"
# A standalone CPython for x86-64 Linux hosts whose python3 is missing, too old,
# or cannot make a venv (Ubuntu without python3-venv). Pinned like llvm-mingw.
STANDALONE_PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.13.16%2B20261013-x86_64-unknown-linux-gnu-install_only.tar.gz"
STANDALONE_PYTHON_SHA256=e588e50db882fa6a89f926d9a9ad64e445183af0eda5bda226a2c151b17db8a6
STANDALONE_PYTHON="${DOAXBV_ROOT}/tools/python-linux"

make_venv() {
    # The lifter needs Python 3.10 or newer.
    if python3 -I -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>/dev/null &&
        python3 -m venv --clear "${VENV}" 2>/dev/null; then
        return
    fi
    if [[ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]]; then
        echo "Install Python 3.10 or newer with its venv module; see docs/building.md." >&2
        exit 1
    fi
    if [[ ! -x "${STANDALONE_PYTHON}/bin/python3" ]]; then
        echo "The system Python cannot make a venv; downloading a standalone Python 3.13 (76 MB)..."
        local staging
        mkdir -p "${DOAXBV_ROOT}/private"
        staging="$(mktemp -d "${DOAXBV_ROOT}/private/python-XXXXXX")"
        curl -fL --retry 3 -o "${staging}/python.tar.gz" "${STANDALONE_PYTHON_URL}"
        echo "${STANDALONE_PYTHON_SHA256}  ${staging}/python.tar.gz" | sha256sum -c --quiet
        tar -xzf "${staging}/python.tar.gz" -C "${staging}"
        rm -rf "${STANDALONE_PYTHON}"
        mv "${staging}/python" "${STANDALONE_PYTHON}"
        rm -rf "${staging}"
    fi
    "${STANDALONE_PYTHON}/bin/python3" -m venv --clear "${VENV}"
}

# A venv records its own path, so one from before the game folder moved is rebuilt.
if [[ -f "${VENV}/doaxbv-root" && "$(cat "${VENV}/doaxbv-root")" != "${DOAXBV_ROOT}" ]]; then
    echo "The game folder moved; recreating private/venv..."
    rm -rf "${VENV}"
fi
# A venv made by another Python, such as a distrobox's while SteamOS's runs it, is rebuilt.
if [[ -f "${VENV}/pyvenv.cfg" ]]; then
    made_with="$(sed -n 's/^version *= *//p' "${VENV}/pyvenv.cfg")"
    if [[ -n "${made_with}" && "$("${PYTHON}" -I -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" != "${made_with}" ]]; then
        echo "private/venv was made by another Python (${made_with}); recreating it..."
        rm -rf "${VENV}"
    fi
fi
# Homebrew and Ubuntu Pythons refuse pip install, so setup keeps Capstone in its own venv.
# -I keeps the caller's folder and PYTHON* variables out of these checks.
if ! "${PYTHON}" -I -c "import capstone, importlib.metadata as m, sys; sys.exit(m.version('capstone') != '5.0.9')" 2>/dev/null; then
    echo "Installing Capstone 5.0.9 into private/venv..."
    if ! "${PYTHON}" -I -m pip --version >/dev/null 2>&1; then
        make_venv
    fi
    "${PYTHON}" -I -m pip install --disable-pip-version-check --force-reinstall --require-hashes \
        -r "${DOAXBV_ROOT}/tools/capstone-requirements.txt"
fi
printf '%s\n' "${DOAXBV_ROOT}" > "${VENV}/doaxbv-root"
# SteamOS has a read-only system with no compiler or headers. When the host lacks
# a C++ compiler or CMake, build with zig, CMake and Ninja from pinned wheels in
# private/venv instead. DOAXBV_TOOLCHAIN=zig forces this; =system disables it.
if [[ "$(uname -s)" == "Linux" && -z "${DOAXBV_TOOLCHAIN:-}" ]]; then
    if ! command -v cmake >/dev/null 2>&1 || ! { command -v c++ || command -v clang++ || command -v g++; } >/dev/null 2>&1; then
        DOAXBV_TOOLCHAIN=zig
    fi
fi
if [[ "${DOAXBV_TOOLCHAIN:-}" == "zig" ]]; then
    if [[ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]] || ! getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
        echo "This system has no C++ compiler or CMake, and the portable toolchain builds only for" >&2
        echo "x86-64 Linux with glibc. Install CMake and a C++17 compiler; see docs/building.md." >&2
        exit 1
    fi
    export DOAXBV_TOOLCHAIN
    if ! "${PYTHON}" -I -c "import importlib.metadata as m, sys; sys.exit(m.version('ziglang') != '0.14.1' or m.version('cmake') != '4.4.4' or m.version('ninja') != '1.13.2')" 2>/dev/null; then
        echo "Installing the portable build tools (zig, CMake, Ninja) into private/venv..."
        "${PYTHON}" -I -m pip install --disable-pip-version-check --require-hashes \
            -r "${DOAXBV_ROOT}/tools/portable-toolchain-requirements.txt"
    fi
    export PATH="${VENV}/bin:${PATH}"
fi
