# SPDX-License-Identifier: GPL-3.0-or-later
"""Check local build tools and optionally download the pinned llvm-mingw compiler and DXC."""
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from extract_iso import run_logged, sha256


LLVM_MINGW_URL = ("https://github.com/mstorsjo/llvm-mingw/releases/download/20261006/"
                  "llvm-mingw-20261006-ucrt-x86_64.zip")
LLVM_MINGW_SHA256 = "317492c456aa27ee607a5919f1d2d38dcdc1112516a24d0bf4b00d078f52d17a"
LLVM_MINGW = Path("tools/llvm-mingw")
# RTGI's shaders need DXC; recomp-runtime/CMakeLists.txt looks for it in tools/dxc.
DXC_URL = ("https://github.com/microsoft/DirectXShaderCompiler/releases/download/v1.9.2609/"
           "dxc_2026_09_29.zip")
DXC_SHA256 = "ad31b1fc8443175d204f77a611fdb3ef2ec42759bdc2f1167368de24a4a7e7f1"
DXC = Path("tools/dxc")
EXTRACT_XISO_URL = "https://github.com/XboxDev/extract-xiso.git"
EXTRACT_XISO_COMMIT = "b72e5b60d598ec6df80534cda19cdcd4361aa18c"  # build-202505152050, as the Windows ZIP bundles
# SteamOS and other hosts without a compiler build with zig from private/venv.
PORTABLE_TOOLCHAIN = Path("private/portable-toolchain")
PORTABLE_GLIBC = "2.31"  # Steam Runtime 3 (sniper) glibc; the bundled SDL3 targets it
# A host libSDL3.so.0 at least this new is linked with these headers instead of the bundled SDL3.
SDL3_VERSION = (3, 4, 12)
SDL3_SOURCE_URL = "https://github.com/libsdl-org/SDL/releases/download/release-3.4.12/SDL3-3.4.12.tar.gz"
SDL3_SOURCE_SHA256 = "f07b958a9ac5020fb7a44cadb957f658b2149c3c8abb4f63145fac9303249db7"  # as recomp-runtime
SDL3_HOST = Path("private/sdl3-host")
SDL3_LINUX = Path("tools/sdl3-linux")
# zig 0.14.1 (tools/portable-toolchain-requirements.txt): its clang 19 builds the
# game about 10% faster than zig 0.16's clang 21 on a Steam Deck, but it carries
# glibc stubs only up to 2.41.
ZIG_GLIBC_MAX = "2.41"
HOST_LIBRARY_DIRS = ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu",
                     "/usr/local/lib")
HELP = ("The Windows build compiles with llvm-mingw. Rerun with --install-prerequisites "
        "(BuildGame.cmd does this) to download it into tools/llvm-mingw, or extract "
        f"{LLVM_MINGW_URL} there so that tools/llvm-mingw/bin/clang.exe exists.")


def prefer_bundled_tools(root):
    bundled = [str((root / name).parent) for name in
               ("tools/git/cmd/git.exe", "tools/cmake/bin/cmake.exe", LLVM_MINGW / "bin/clang.exe")
               if (root / name).is_file()]
    if bundled:
        existing = os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(bundled + [p for p in existing if p not in bundled])


def portable_toolchain_requested():
    """build_game.sh sets DOAXBV_TOOLCHAIN=zig when the host has no compiler or CMake (SteamOS)."""
    return os.environ.get("DOAXBV_TOOLCHAIN", "").lower() == "zig"


def host_glibc():
    """The host's glibc version such as "2.43", or None on musl and other C libraries."""
    try:
        name = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, ValueError, OSError):
        return None
    match = re.fullmatch(r"glibc (\d+\.\d+)", name or "")
    return match.group(1) if match else None


def check_portable_host():
    """zig builds for x86-64 glibc here; say so before anything downloads. Returns the host glibc."""
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("The portable toolchain builds for x86-64 Linux only. Install CMake and a "
                         "C++17 compiler instead; see docs/building.md.")
    glibc = host_glibc()
    if glibc is None:
        raise ValueError("The portable toolchain needs glibc. On musl distributions such as Alpine, "
                         "install CMake, a C++17 compiler and SDL3 instead; see docs/building.md.")
    return glibc


def sdl3_version(library):
    """SDL_GetVersion() of a shared library as (major, minor, micro), or None if it will not load."""
    import ctypes
    try:
        sdl = ctypes.CDLL(str(library))
        sdl.SDL_GetVersion.restype = ctypes.c_int
        version = sdl.SDL_GetVersion()
    except (OSError, AttributeError):
        return None
    return version // 1000000, version // 1000 % 1000, version % 1000


def host_sdl3():
    """The host's libSDL3.so.0 when it is SDL 3 at least SDL3_VERSION, as (path, version), else None."""
    for folder in HOST_LIBRARY_DIRS:
        library = Path(folder) / "libSDL3.so.0"
        if library.is_file():
            version = sdl3_version(library)
            if version and version[0] == SDL3_VERSION[0] and version >= SDL3_VERSION:
                return library, version
    return None


def write_if_changed(path, text):
    """Write text unless the file already holds it. CMake reruns configure whenever a
    toolchain or package file it read gets a new timestamp, so unchanged files stay untouched."""
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeDecodeError):
        pass
    path.write_text(text, encoding="utf-8")


def write_host_sdl3(root, library):
    """A CMake package for the host library with the pinned SDL headers; SteamOS ships no headers."""
    prefix = root / SDL3_HOST
    if not (prefix / "include/SDL3/SDL.h").is_file():
        private = root / "private"
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sdl3-headers-", dir=private) as folder:
            archive = Path(folder) / "sdl3.tar.gz"
            print("Downloading the SDL 3.4.12 headers (4 MB)...", flush=True)
            try:
                with urllib.request.urlopen(SDL3_SOURCE_URL, timeout=60) as response, archive.open("xb") as file:
                    shutil.copyfileobj(response, file)
            except (OSError, urllib.error.URLError) as error:
                raise ValueError(f"Could not download the SDL headers: {error}. Retry when online.") from error
            if sha256(archive) != SDL3_SOURCE_SHA256:
                raise ValueError("The SDL headers download failed its SHA-256 check. Rerun setup.")
            staging = Path(folder) / "include"
            top = "SDL3-3.4.12/include/"
            with tarfile.open(archive) as contents:
                for member in contents.getmembers():
                    relative = member.name[len(top):] if member.name.startswith(top) else ""
                    if not member.isfile() or not relative or ".." in Path(relative).parts:
                        continue
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(contents.extractfile(member).read())
            shutil.rmtree(prefix, ignore_errors=True)
            prefix.mkdir(parents=True)
            staging.replace(prefix / "include")
    config = prefix / "lib/cmake/SDL3"
    config.mkdir(parents=True, exist_ok=True)
    write_if_changed(config / "SDL3Config.cmake",
        "# Written by tools/build_prerequisites.py: the host's SDL3 with the pinned headers.\n"
        "set(SDL3_FOUND TRUE)\n"
        "set(RECOMP_SDL3_HOST TRUE)  # the system's library: never copied beside the program\n"
        "if(NOT TARGET SDL3::SDL3)\n"
        "    add_library(SDL3::SDL3 SHARED IMPORTED)\n"
        f'    set_target_properties(SDL3::SDL3 PROPERTIES IMPORTED_LOCATION "{Path(library).as_posix()}"\n'
        f'        INTERFACE_INCLUDE_DIRECTORIES "{(prefix / "include").as_posix()}")\n'
        "endif()\n")
    write_if_changed(config / "SDL3ConfigVersion.cmake",
        "set(PACKAGE_VERSION 3.4.12)\n"
        "set(PACKAGE_VERSION_COMPATIBLE TRUE)\n"
        'if(PACKAGE_FIND_VERSION VERSION_EQUAL PACKAGE_VERSION)\n'
        "    set(PACKAGE_VERSION_EXACT TRUE)\n"
        "endif()\n")
    return config


def select_portable_sdl3(root, glibc):
    """Pick the SDL3 the zig build links and the glibc it targets.

    A host SDL3 (SteamOS ships one without headers) is linked against the host
    glibc, which only grows on that machine. Otherwise the bundled SDL3, built
    for PORTABLE_GLIBC, is used. Host SDL headers are never used: they would put
    the host's libc headers ahead of zig's.
    Returns (SDL3 CMake package folder, target glibc, description).
    """
    # DOAXBV_SDL3=bundled keeps a build that moves to older systems: glibc 2.31 and its own SDL3.
    host = None if os.environ.get("DOAXBV_SDL3", "").lower() == "bundled" else host_sdl3()
    if host:
        library, version = host
        return write_host_sdl3(root, library), glibc, f"host {library} {'.'.join(map(str, version))}"
    bundled = root / SDL3_LINUX / "lib/cmake/SDL3"
    if (bundled / "SDL3Config.cmake").is_file():
        return bundled, PORTABLE_GLIBC, f"bundled {SDL3_LINUX.as_posix()}"
    raise ValueError(f"No SDL3 found. Install your distribution's SDL3 (3.4.12 or newer), or copy "
                     f"{SDL3_LINUX.as_posix()} from a Linux release ZIP or from tools/build_sdl3_linux.sh "
                     "run on a machine with SDL3's build dependencies.")


def prepare_portable_toolchain(root):
    """Check the host, pick SDL3 and write the zig toolchain. Returns the CMake arguments and receipt fields."""
    glibc = check_portable_host()
    sdl3, target, description = select_portable_sdl3(root, glibc)
    # zig 0.14.1 knows glibc up to ZIG_GLIBC_MAX; a newer host still runs that target.
    if tuple(map(int, target.split("."))) > tuple(map(int, ZIG_GLIBC_MAX.split("."))):
        target = ZIG_GLIBC_MAX
    toolchain = write_portable_toolchain(root, target, sdl3.parents[2])
    return {"cmake_args": [f"-DCMAKE_TOOLCHAIN_FILE={toolchain.as_posix()}", f"-DSDL3_DIR={sdl3.as_posix()}"],
            "target": f"x86_64-linux-gnu.{target}", "sdl3": description}


def write_portable_toolchain(root, glibc=PORTABLE_GLIBC, find_root=None):
    """Wrap the venv's ziglang as cc, c++, ar and ranlib and write a CMake toolchain file.

    zig cc is clang with its own glibc and libc++ headers, so it compiles on a
    read-only host without system headers. When find_root is given, searches
    are rooted in that SDL3 prefix so host libraries, which may lack headers,
    are never picked up. Returns the toolchain file.
    """
    import importlib.util
    spec = importlib.util.find_spec("ziglang")
    if spec is None or spec.origin is None:
        raise ValueError("ziglang is missing; run build_game.sh, which installs it into private/venv")
    zig = Path(spec.origin).parent / "zig"
    if not zig.is_file():
        raise ValueError(f"ziglang is installed, but {zig} is missing; delete private/venv and rerun build_game.sh")
    folder = root / PORTABLE_TOOLCHAIN
    folder.mkdir(parents=True, exist_ok=True)
    cache = root / "private/zig-cache"
    # Guest code relies on wrapping arithmetic, so keep zig's debug-mode UBSan traps off.
    target = f"x86_64-linux-gnu.{glibc}"
    commands = {"cc": f"cc -target {target} -fno-sanitize=all",
                "c++": f"c++ -target {target} -fno-sanitize=all",
                "ar": "ar", "ranlib": "ranlib"}
    # zig cc hands clang -O2 for any optimised build and keeps frame pointers.
    # Optimised builds get what a host clang gives them: -O3 when asked for
    # and no frame pointer. The generated game ran 15-20% slower without this.
    # zig 0.14.1 rejects a glibc version in the target of a bare --version.
    optimise = ('opt=\ncase " $* " in\n'
                f'    " --version ") exec {shlex.quote(str(zig))} {{name}} --version ;;\n'
                '    *" -O3 "*) opt="-fomit-frame-pointer -Xclang -O3" ;;\n'
                '    *" -O1 "*|*" -O2 "*|*" -Os "*) opt="-fomit-frame-pointer" ;;\n'
                'esac\n')
    for name, tail in commands.items():
        wrapper = folder / name
        compiler = name in ("cc", "c++")
        write_if_changed(wrapper, "#!/bin/sh\n"
                         f"export ZIG_GLOBAL_CACHE_DIR={shlex.quote(str(cache))}\n"
                         f"export ZIG_LOCAL_CACHE_DIR={shlex.quote(str(cache))}\n"
                         + (optimise.replace("{name}", name) if compiler else "")
                         + f'exec {shlex.quote(str(zig))} {tail} "$@"'
                         + (" $opt" if compiler else "") + "\n")
        wrapper.chmod(0o755)
    lines = ["# Written by tools/build_prerequisites.py; see docs/building.md.",
             f'set(CMAKE_C_COMPILER "{(folder / "cc").as_posix()}")',
             f'set(CMAKE_CXX_COMPILER "{(folder / "c++").as_posix()}")',
             f'set(CMAKE_AR "{(folder / "ar").as_posix()}" CACHE FILEPATH "")',
             f'set(CMAKE_RANLIB "{(folder / "ranlib").as_posix()}" CACHE FILEPATH "")']
    if find_root is not None:
        lines += [f'set(CMAKE_FIND_ROOT_PATH "{Path(find_root).as_posix()}")',
                  "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)",
                  "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)",
                  "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)",
                  "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)"]
    toolchain = folder / "toolchain.cmake"
    write_if_changed(toolchain, "\n".join(lines) + "\n")
    return toolchain


def find_toolchain(root):
    if portable_toolchain_requested():
        return root / PORTABLE_TOOLCHAIN / "c++"
    if os.name != "nt":
        # CMake finds the host compiler; check only that the tools are installed.
        compiler = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")
        if compiler is None or shutil.which("cmake") is None:
            raise ValueError("CMake and a C++17 compiler are required; see docs/building.md.")
        return compiler
    clang = root / LLVM_MINGW / "bin/clang.exe"
    return clang if clang.is_file() else None


def extended_path(path):
    """Return an absolute path that Windows file APIs accept beyond 260 characters."""
    path = os.path.abspath(path)
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def install_archive(root, name, url, digest, folder_name, size, single_folder):
    """Download a pinned ZIP, check its SHA-256 and move it into root/folder_name.

    single_folder: the ZIP holds one versioned folder whose contents are the tool.
    """
    target = root / folder_name
    if target.exists():
        raise ValueError(f"{target} exists but is incomplete. Delete it and rerun setup.")
    private = root / "private"
    private.mkdir(exist_ok=True)
    for stale in private.glob(f"{name}-*"):  # staging an earlier run could not clean up
        shutil.rmtree(extended_path(stale), ignore_errors=True)
    # Staging adds ~70 characters to the ZIP's deepest paths, which can pass 260 under a long
    # install folder when Windows long paths are disabled (#171).
    # A folder antivirus still locks must not hide the rename error below.
    with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=extended_path(private),
                                     ignore_cleanup_errors=True) as folder:
        archive = Path(folder) / f"{name}.zip"
        print(f"Downloading {name} ({size})...", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as file:
                shutil.copyfileobj(response, file)
        except (OSError, urllib.error.URLError) as error:
            raise ValueError(f"Could not download {name}: {error}. Retry when online.") from error
        if sha256(archive) != digest:
            raise ValueError(f"The {name} download failed its SHA-256 check. Rerun setup.")
        print(f"Extracting {name}...", flush=True)
        staging = Path(folder) / "extract"
        with zipfile.ZipFile(archive) as contents:
            contents.extractall(staging)
        if single_folder:
            (staging,) = staging.iterdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(10):
            try:
                staging.replace(target)
                break
            except PermissionError as error:
                # Antivirus can hold the freshly extracted files open for a few seconds.
                if attempt == 9:
                    raise ValueError(f"Could not move {name} into {target}: {error}. "
                                     "Antivirus may still be scanning it; rerun setup.") from error
                time.sleep(3)


def install_llvm_mingw(root):
    install_archive(root, "llvm-mingw", LLVM_MINGW_URL, LLVM_MINGW_SHA256, LLVM_MINGW, "190 MB", True)


def install_dxc(root):
    install_archive(root, "dxc", DXC_URL, DXC_SHA256, DXC, "32 MB", False)


def ensure_extract_xiso(root, logs):
    """Build the pinned XboxDev extract-xiso; macOS and Linux have no package for it."""
    source = root / "tools/extract-xiso" / EXTRACT_XISO_COMMIT[:12]
    tool = source / "build/extract-xiso"
    built = source / "build-complete"  # an interrupted link can leave a partial binary
    if built.is_file() and tool.is_file():
        return tool
    missing = [name for name in ("git", "cmake") if shutil.which(name) is None]
    if missing:
        raise ValueError(f"Install {' and '.join(missing)} to build extract-xiso, or pass --extractor <executable>")
    shutil.rmtree(source, ignore_errors=True)  # a build an earlier run did not finish
    generator = "Ninja" if shutil.which("ninja") else "Unix Makefiles"
    try:
        run_logged(["git", "clone", "--quiet", "--no-checkout", EXTRACT_XISO_URL, source],
                   logs / "extract-xiso-download.log")
        run_logged(["git", "-C", source, "checkout", "--quiet", EXTRACT_XISO_COMMIT], logs / "extract-xiso-checkout.log")
        run_logged(["cmake", "-S", source, "-B", source / "build", "-G", generator, "-DCMAKE_BUILD_TYPE=Release",
                    *portable_cmake_args(root)], logs / "extract-xiso-configure.log")
        run_logged(["cmake", "--build", source / "build"], logs / "extract-xiso-build.log")
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"Could not build extract-xiso: {error}. Install XboxDev extract-xiso "
                         "or pass --extractor <executable>") from error
    if not tool.is_file():
        raise ValueError(f"extract-xiso built, but {tool} is missing. Pass --extractor <executable>")
    built.touch()
    return tool


def portable_cmake_args(root):
    """CMake arguments for the zig toolchain, or none when the host compiler builds."""
    if not portable_toolchain_requested():
        return []
    return prepare_portable_toolchain(root)["cmake_args"]


def ensure_prerequisites(root, install=False):
    if portable_toolchain_requested():
        prepare_portable_toolchain(root)
    compiler = find_toolchain(root)
    if not compiler:
        if not install:
            raise ValueError(HELP)
        install_llvm_mingw(root)
        compiler = find_toolchain(root)
        if not compiler:
            raise ValueError("llvm-mingw was extracted, but tools/llvm-mingw/bin/clang.exe is still missing. " + HELP)
    # Without DXC the build still succeeds, but without RTGI.
    if os.name == "nt" and install and not (root / DXC / "bin/x64/dxc.exe").is_file():
        install_dxc(root)
    return compiler
