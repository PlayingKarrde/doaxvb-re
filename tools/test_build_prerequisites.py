# SPDX-License-Identifier: GPL-3.0-or-later
"""Prerequisite checks without network downloads."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

import build_game
import build_prerequisites as prerequisites


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        private = build_game.ROOT / "private"
        private.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="prerequisites-test-", dir=private)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.download = self.enterContext(mock.patch.object(
            prerequisites.urllib.request, "urlopen", side_effect=AssertionError("Unexpected download")))
        self.enterContext(mock.patch("builtins.print"))

    def test_bundled_path_precedes_system_and_is_inherited(self):
        with mock.patch.dict(os.environ, PATH="system-tools"):
            prerequisites.prefer_bundled_tools(self.root)
            self.assertEqual(os.environ["PATH"], "system-tools")
            for name in ("tools/git/cmd/git.exe", "tools/cmake/bin/cmake.exe"):
                path = self.root / name
                path.parent.mkdir(parents=True)
                path.touch()
            prerequisites.prefer_bundled_tools(self.root)
            expected = os.pathsep.join([str(self.root / "tools/git/cmd"),
                                        str(self.root / "tools/cmake/bin"), "system-tools"])
            self.assertEqual(os.environ["PATH"], expected)
            prerequisites.prefer_bundled_tools(self.root)
            self.assertEqual(os.environ["PATH"], expected)
            child = subprocess.check_output(
                [sys.executable, "-c", "import os; print(os.environ['PATH'])"], text=True).strip()
            self.assertEqual(child, expected)
            if os.name == "nt":
                self.assertEqual(Path(prerequisites.shutil.which("git")), self.root / "tools/git/cmd/git.exe")
                self.assertEqual(Path(prerequisites.shutil.which("cmake")), self.root / "tools/cmake/bin/cmake.exe")

    def test_existing_compiler_skips_download(self):
        with mock.patch.object(prerequisites, "find_toolchain", return_value=Path("clang")), \
                mock.patch.object(prerequisites, "install_llvm_mingw") as install, \
                mock.patch.object(prerequisites, "install_dxc") as install_dxc:
            self.assertEqual(prerequisites.ensure_prerequisites(self.root, True), Path("clang"))
            install.assert_not_called()
            # Windows setup also fetches DXC for RTGI, once, and only when asked to install.
            self.assertEqual(install_dxc.call_count, int(os.name == "nt"))
            dxc = self.root / "tools/dxc/bin/x64/dxc.exe"
            dxc.parent.mkdir(parents=True)
            dxc.touch()
            prerequisites.ensure_prerequisites(self.root, True)
            prerequisites.ensure_prerequisites(self.root)
            self.assertEqual(install_dxc.call_count, int(os.name == "nt"))

    def test_missing_compiler_requires_flag_and_is_rechecked(self):
        with mock.patch.object(prerequisites, "find_toolchain", return_value=None), \
                mock.patch.object(prerequisites, "install_llvm_mingw") as install:
            with self.assertRaisesRegex(ValueError, "--install-prerequisites"):
                prerequisites.ensure_prerequisites(self.root)
            install.assert_not_called()
            with self.assertRaisesRegex(ValueError, "still missing"):
                prerequisites.ensure_prerequisites(self.root, True)
            install.assert_called_once_with(self.root)

    def llvm_mingw_zip(self):
        # Its final path is 250 characters, so the staging path passes 260 (#171).
        self.deep_name = "lib/" + "a" * (250 - len(str(self.root / "tools/llvm-mingw/lib")) - 1)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("llvm-mingw-test-ucrt-x86_64/bin/clang.exe", b"clang")
            archive.writestr("llvm-mingw-test-ucrt-x86_64/" + self.deep_name, b"deep")
        return data.getvalue()

    def test_download_installs_verified_toolchain(self):
        payload = self.llvm_mingw_zip()
        stale = self.root / "private/llvm-mingw-old/extract"
        stale.mkdir(parents=True)
        (stale / "locked.dll").write_bytes(b"")
        self.download.side_effect = lambda *args, **kwargs: io.BytesIO(payload)
        with mock.patch.object(prerequisites, "LLVM_MINGW_SHA256", hashlib.sha256(payload).hexdigest()), \
                mock.patch.object(prerequisites.tempfile, "TemporaryDirectory",
                                  wraps=tempfile.TemporaryDirectory) as staging:
            prerequisites.install_llvm_mingw(self.root)
        # Long paths may be enabled on the test host, so check the staging path directly (#171).
        staging.assert_called_once_with(prefix="llvm-mingw-",
                                        dir=prerequisites.extended_path(self.root / "private"),
                                        ignore_cleanup_errors=True)
        self.assertEqual((self.root / "tools/llvm-mingw/bin/clang.exe").read_bytes(), b"clang")
        self.assertEqual((self.root / "tools/llvm-mingw" / self.deep_name).read_bytes(), b"deep")
        self.assertEqual(list((self.root / "private").iterdir()), [])
        self.download.assert_called_once_with(prerequisites.LLVM_MINGW_URL, timeout=60)

    def test_download_installs_verified_dxc(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:  # the DXC ZIP has no top-level folder
            archive.writestr("bin/x64/dxc.exe", b"dxc")
            archive.writestr("LICENSE-MS.txt", b"license")
        payload = data.getvalue()
        self.download.side_effect = lambda *args, **kwargs: io.BytesIO(payload)
        with mock.patch.object(prerequisites, "DXC_SHA256", hashlib.sha256(payload).hexdigest()):
            prerequisites.install_dxc(self.root)
        self.assertEqual((self.root / "tools/dxc/bin/x64/dxc.exe").read_bytes(), b"dxc")
        self.assertTrue((self.root / "tools/dxc/LICENSE-MS.txt").is_file())
        self.assertEqual(list((self.root / "private").iterdir()), [])
        self.download.assert_called_once_with(prerequisites.DXC_URL, timeout=60)

    def test_extended_path_prefixes_only_windows_paths(self):
        if os.name != "nt":
            self.assertEqual(prerequisites.extended_path("x"), os.path.abspath("x"))
            return
        for path, expected in (("C:/a/../x", "\\\\?\\C:\\x"),
                               ("\\\\srv\\share\\x", "\\\\?\\UNC\\srv\\share\\x"),
                               ("\\\\?\\C:\\x", "\\\\?\\C:\\x")):
            with self.subTest(path=path):
                self.assertEqual(prerequisites.extended_path(path), expected)

    def test_failed_download_or_hash_installs_nothing(self):
        payload = self.llvm_mingw_zip()
        for failure, message in ((urllib.error.URLError("offline"), "Could not download"),
                                 (None, "SHA-256")):
            with self.subTest(message=message):
                self.download.side_effect = failure or (lambda *args, **kwargs: io.BytesIO(payload))
                with self.assertRaisesRegex(ValueError, message):
                    prerequisites.install_llvm_mingw(self.root)
                self.assertFalse((self.root / "tools/llvm-mingw").exists())
                self.assertEqual(list((self.root / "private").iterdir()), [])

    def test_locked_extraction_is_retried_then_reported(self):
        payload = self.llvm_mingw_zip()
        self.download.side_effect = lambda *args, **kwargs: io.BytesIO(payload)
        move = Path.replace
        for failures, installed in ((10, False), (2, True)):
            with self.subTest(failures=failures):
                calls = []

                def locked(path, target):
                    calls.append(target)
                    if len(calls) <= failures:
                        raise PermissionError(13, "Access is denied")
                    return move(path, target)

                with mock.patch.object(prerequisites, "LLVM_MINGW_SHA256", hashlib.sha256(payload).hexdigest()), \
                        mock.patch.object(Path, "replace", locked), \
                        mock.patch.object(prerequisites.time, "sleep") as sleep:
                    if installed:
                        prerequisites.install_llvm_mingw(self.root)
                    else:
                        with self.assertRaisesRegex(ValueError, "rerun setup"):
                            prerequisites.install_llvm_mingw(self.root)
                self.assertEqual(sleep.call_args_list, [mock.call(3)] * min(failures, 9))
                self.assertEqual((self.root / "tools/llvm-mingw/bin/clang.exe").is_file(), installed)
                self.assertEqual(list((self.root / "private").iterdir()), [])

    def test_build_checks_before_extraction_and_skips_for_generation(self):
        recipe = self.root / "tools/game-recipe/recipe.json"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(json.dumps({"files": {}}), encoding="utf-8")
        with mock.patch.object(build_game, "ROOT", self.root), \
                mock.patch.object(build_game.shutil, "which", return_value="tool"), \
                mock.patch.dict(sys.modules, capstone=mock.Mock()), \
                mock.patch.object(build_game, "ensure_prerequisites", side_effect=ValueError("missing tools")) as ensure, \
                mock.patch.object(build_game, "extract", side_effect=ValueError("extraction reached")) as extract:
            args = argparse.Namespace(generate_only=False, imported=None, iso=Path("input.iso"), extractor="extract-xiso")
            with self.assertRaisesRegex(ValueError, "missing tools"):
                build_game.build(args, verify_parity=False)
            ensure.assert_called_once_with(self.root, False)
            extract.assert_not_called()
            ensure.reset_mock()
            args.generate_only = True
            with self.assertRaisesRegex(ValueError, "extraction reached"):
                build_game.build(args, verify_parity=False)
            ensure.assert_not_called()
            extract.assert_called_once()

    def test_missing_capstone_hint_matches_platform(self):
        recipe = self.root / "tools/game-recipe/recipe.json"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(json.dumps({"files": {}}), encoding="utf-8")
        expected = {"win32": f'& "{sys.executable}" -m pip install capstone==5.0.9',
                    "darwin": f"run {shlex.quote(str(self.root / 'build_game.sh'))}, which installs it into private/venv"}
        args = argparse.Namespace(generate_only=True, imported=None, iso=Path("input.iso"), extractor="extract-xiso")
        for platform, hint in expected.items():
            with self.subTest(platform=platform), \
                    mock.patch.object(build_game, "ROOT", self.root), \
                    mock.patch.object(build_game.shutil, "which", return_value="tool"), \
                    mock.patch.object(build_game.sys, "platform", platform), \
                    mock.patch.dict(sys.modules, capstone=None):
                with self.assertRaises(ValueError) as caught:
                    build_game.build(args, verify_parity=False)
                self.assertEqual(str(caught.exception), "Install Capstone: " + hint)

    def test_extract_xiso_reuses_only_a_finished_build(self):
        source = self.root / "tools/extract-xiso" / prerequisites.EXTRACT_XISO_COMMIT[:12]
        tool = source / "build/extract-xiso"

        def fake_run(args, log, cwd=None):
            if args[:2] == ["cmake", "--build"]:
                tool.parent.mkdir(parents=True, exist_ok=True)
                tool.write_bytes(b"built")

        with mock.patch.object(prerequisites.shutil, "which", return_value="tool"), \
                mock.patch.object(prerequisites, "run_logged", side_effect=fake_run) as run:
            self.assertEqual(prerequisites.ensure_extract_xiso(self.root, self.root), tool)
            self.assertEqual(run.call_count, 4)
            run.reset_mock()
            self.assertEqual(prerequisites.ensure_extract_xiso(self.root, self.root), tool)
            run.assert_not_called()
            (source / "build-complete").unlink()  # as after an interrupted link
            tool.write_bytes(b"partial")
            prerequisites.ensure_extract_xiso(self.root, self.root)
            self.assertEqual(run.call_count, 4)
            self.assertEqual(tool.read_bytes(), b"built")
        shutil.rmtree(source)
        with mock.patch.object(prerequisites.shutil, "which", side_effect=lambda name: None if name == "cmake" else "tool"), \
                mock.patch.object(prerequisites, "run_logged") as run:
            with self.assertRaisesRegex(ValueError, "Install cmake to build extract-xiso"):
                prerequisites.ensure_extract_xiso(self.root, self.root)
            run.assert_not_called()

    def fake_ziglang(self):
        """A ziglang package whose zig prints its arguments, as installed in private/venv."""
        package = self.root / "site-packages/ziglang"
        package.mkdir(parents=True)
        (package / "__init__.py").touch()
        zig = package / "zig"
        zig.write_text('#!/bin/sh\necho "$@"\n', encoding="utf-8")
        zig.chmod(0o755)
        spec = mock.Mock(origin=str(package / "__init__.py"))
        return zig, mock.patch("importlib.util.find_spec",
                               side_effect=lambda name: spec if name == "ziglang" else None)

    def portable_host(self, glibc="2.43"):
        return mock.patch.object(prerequisites, "check_portable_host", return_value=glibc)

    def bundled_sdl3(self):
        config = self.root / prerequisites.SDL3_LINUX / "lib/cmake/SDL3"
        config.mkdir(parents=True)
        (config / "SDL3Config.cmake").touch()
        return config

    def test_portable_toolchain_is_opt_in(self):
        with mock.patch.dict(os.environ, {"DOAXBV_TOOLCHAIN": ""}), \
                mock.patch.object(prerequisites, "prepare_portable_toolchain") as prepare:
            self.assertEqual(prerequisites.portable_cmake_args(self.root), [])
            prepare.assert_not_called()

    def test_portable_toolchain_wraps_zig_and_roots_searches_in_sdl3(self):
        zig, find_spec = self.fake_ziglang()
        prefix = self.root / "sdl prefix"
        with find_spec:
            toolchain = prerequisites.write_portable_toolchain(self.root, "2.43", prefix)
        folder = self.root / prerequisites.PORTABLE_TOOLCHAIN
        self.assertEqual(toolchain, folder / "toolchain.cmake")
        text = toolchain.read_text(encoding="utf-8")
        self.assertIn(f'set(CMAKE_CXX_COMPILER "{(folder / "c++").as_posix()}")', text)
        self.assertIn(f'set(CMAKE_FIND_ROOT_PATH "{prefix.as_posix()}")', text)
        self.assertIn("set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)", text)
        wrapper = (folder / "c++").read_text(encoding="utf-8")
        self.assertIn(f"{shlex.quote(str(zig))} c++ -target x86_64-linux-gnu.2.43 -fno-sanitize=all", wrapper)
        self.assertIn(f"ZIG_GLOBAL_CACHE_DIR={shlex.quote(str(self.root / 'private/zig-cache'))}", wrapper)
        if os.name != "nt":
            output = subprocess.check_output([folder / "cc", "-c", "a b.c"], text=True)
            self.assertEqual(output.split(), ["cc", "-target", "x86_64-linux-gnu.2.43",
                                              "-fno-sanitize=all", "-c", "a", "b.c"])
            output = subprocess.check_output([folder / "c++", "-O3", "-c", "a.cpp"], text=True)
            self.assertEqual(output.split()[-3:], ["-fomit-frame-pointer", "-Xclang", "-O3"])
            output = subprocess.check_output([folder / "c++", "-O2", "-c", "a.cpp"], text=True)
            self.assertEqual(output.split()[-1], "-fomit-frame-pointer")
            self.assertEqual(subprocess.check_output([folder / "c++", "--version"], text=True).split(),
                             ["c++", "--version"])
            self.assertEqual(subprocess.check_output([folder / "ar", "rcs"], text=True).split(), ["ar", "rcs"])
        # CMake reconfigures when a toolchain file's timestamp moves, so an unchanged rewrite must not touch it.
        stamps = {path: path.stat().st_mtime_ns for path in folder.iterdir()}
        for path in stamps:
            os.utime(path, ns=(10**12, 10**12))
        with find_spec:
            prerequisites.write_portable_toolchain(self.root, "2.43", prefix)
        self.assertEqual({path.stat().st_mtime_ns for path in stamps}, {10**12})
        with find_spec:  # tools/build_sdl3_linux.sh: the bundled glibc and no search root
            text = prerequisites.write_portable_toolchain(self.root).read_text(encoding="utf-8")
        self.assertNotIn("CMAKE_FIND_ROOT_PATH", text)
        self.assertIn("-target x86_64-linux-gnu.2.31 ", (folder / "cc").read_text(encoding="utf-8"))

    def test_portable_toolchain_needs_ziglang(self):
        self.bundled_sdl3()
        with mock.patch("importlib.util.find_spec", return_value=None), self.portable_host(), \
                mock.patch.object(prerequisites, "host_sdl3", return_value=None), \
                mock.patch.dict(os.environ, {"DOAXBV_TOOLCHAIN": "zig"}):
            with self.assertRaisesRegex(ValueError, "ziglang is missing; run build_game.sh"):
                prerequisites.ensure_prerequisites(self.root)

    def test_portable_host_must_be_x86_64_glibc_linux(self):
        cases = (("Linux", "x86_64", "2.43", None), ("Linux", "aarch64", "2.43", "x86-64 Linux only"),
                 ("Darwin", "x86_64", None, "x86-64 Linux only"), ("Linux", "x86_64", None, "needs glibc"))
        for system, machine, glibc, error in cases:
            with self.subTest(system=system, machine=machine, glibc=glibc), \
                    mock.patch.object(prerequisites.platform, "system", return_value=system), \
                    mock.patch.object(prerequisites.platform, "machine", return_value=machine), \
                    mock.patch.object(prerequisites, "host_glibc", return_value=glibc):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        prerequisites.check_portable_host()
                else:
                    self.assertEqual(prerequisites.check_portable_host(), glibc)

    def test_host_sdl3_must_be_sdl3_at_least_the_pinned_release(self):
        folders = [self.root / name for name in ("old", "four", "new", "newer")]
        versions = {"old": (3, 2, 20), "four": (4, 0, 0), "new": (3, 4, 16), "newer": (3, 6, 0)}
        for folder in folders:
            folder.mkdir()
            (folder / "libSDL3.so.0").touch()
        with mock.patch.object(prerequisites, "HOST_LIBRARY_DIRS", [str(f) for f in folders]), \
                mock.patch.object(prerequisites, "sdl3_version", side_effect=lambda path: versions[path.parent.name]):
            self.assertEqual(prerequisites.host_sdl3(), (folders[2] / "libSDL3.so.0", (3, 4, 16)))
        with mock.patch.object(prerequisites, "HOST_LIBRARY_DIRS", [str(folders[0]), str(self.root / "none")]), \
                mock.patch.object(prerequisites, "sdl3_version", return_value=(3, 2, 20)):
            self.assertIsNone(prerequisites.host_sdl3())
        self.assertIsNone(prerequisites.sdl3_version(self.root / "missing.so"))

    def test_host_sdl3_gets_the_pinned_headers_once(self):
        archive = io.BytesIO()
        with prerequisites.tarfile.open(fileobj=archive, mode="w:gz") as contents:
            for name, data in (("SDL3-3.4.12/include/SDL3/SDL.h", b"header"),
                               ("SDL3-3.4.12/include/../escape.h", b"bad"),
                               ("SDL3-3.4.12/src/SDL.c", b"source")):
                info = prerequisites.tarfile.TarInfo(name)
                info.size = len(data)
                contents.addfile(info, io.BytesIO(data))
        payload = archive.getvalue()
        library = Path("/usr/lib/libSDL3.so.0")
        self.download.side_effect = lambda url, timeout: io.BytesIO(payload)
        prefix = self.root / prerequisites.SDL3_HOST
        with mock.patch.object(prerequisites, "SDL3_SOURCE_SHA256", hashlib.sha256(payload).hexdigest()):
            config = prerequisites.write_host_sdl3(self.root, library)
            self.assertEqual(config, prefix / "lib/cmake/SDL3")
            self.assertEqual((prefix / "include/SDL3/SDL.h").read_bytes(), b"header")
            self.assertEqual(sorted(p.name for p in prefix.rglob("*.h")), ["SDL.h"])
            text = (config / "SDL3Config.cmake").read_text(encoding="utf-8")
            self.assertIn('IMPORTED_LOCATION "/usr/lib/libSDL3.so.0"', text)
            self.assertIn(f'INTERFACE_INCLUDE_DIRECTORIES "{(prefix / "include").as_posix()}"', text)
            self.assertEqual(self.download.call_count, 1)
            prerequisites.write_host_sdl3(self.root, library)
            self.assertEqual(self.download.call_count, 1)
        shutil.rmtree(prefix)
        with mock.patch.object(prerequisites, "SDL3_SOURCE_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "SDL headers download failed its SHA-256 check"):
                prerequisites.write_host_sdl3(self.root, library)
        self.assertFalse(prefix.exists())

    def test_portable_sdl3_prefers_the_host_then_the_bundled_build(self):
        host = self.root / "host/lib/cmake/SDL3"
        library = Path("/usr/lib/libSDL3.so.0")
        with mock.patch.object(prerequisites, "host_sdl3", return_value=(library, (3, 4, 12))), \
                mock.patch.object(prerequisites, "write_host_sdl3", return_value=host) as write:
            self.assertEqual(prerequisites.select_portable_sdl3(self.root, "2.43"),
                             (host, "2.43", f"host {library} 3.4.12"))
            write.assert_called_once_with(self.root, library)
        with mock.patch.object(prerequisites, "host_sdl3", return_value=None):
            with self.assertRaisesRegex(ValueError, "No SDL3 found"):
                prerequisites.select_portable_sdl3(self.root, "2.43")
            bundled = self.bundled_sdl3()
            self.assertEqual(prerequisites.select_portable_sdl3(self.root, "2.43"),
                             (bundled, "2.31", "bundled tools/sdl3-linux"))

    def test_prepared_toolchain_gives_cmake_its_sdl3_and_target(self):
        zig, find_spec = self.fake_ziglang()
        bundled = self.bundled_sdl3()
        with find_spec, self.portable_host(), mock.patch.object(prerequisites, "host_sdl3", return_value=None), \
                mock.patch.dict(os.environ, {"DOAXBV_TOOLCHAIN": "zig"}):
            prepared = prerequisites.prepare_portable_toolchain(self.root)
            self.assertEqual(prerequisites.portable_cmake_args(self.root), prepared["cmake_args"])
            self.assertEqual(prerequisites.ensure_prerequisites(self.root),
                             self.root / prerequisites.PORTABLE_TOOLCHAIN / "c++")
        toolchain = self.root / prerequisites.PORTABLE_TOOLCHAIN / "toolchain.cmake"
        self.assertEqual(prepared, {"cmake_args": [f"-DCMAKE_TOOLCHAIN_FILE={toolchain.as_posix()}",
                                                   f"-DSDL3_DIR={bundled.as_posix()}"],
                                    "target": "x86_64-linux-gnu.2.31", "sdl3": "bundled tools/sdl3-linux"})
        self.assertIn(f'CMAKE_FIND_ROOT_PATH "{(self.root / prerequisites.SDL3_LINUX).as_posix()}"',
                      toolchain.read_text(encoding="utf-8"))

    def test_host_glibc_newer_than_zig_knows_targets_zigs_newest(self):
        zig, find_spec = self.fake_ziglang()
        package = self.root / "sdl3/lib/cmake/SDL3"
        for host, expected in (("2.43", prerequisites.ZIG_GLIBC_MAX), ("2.39", "2.39")):
            with find_spec, self.portable_host(host), \
                    mock.patch.object(prerequisites, "select_portable_sdl3", return_value=(package, host, "host")):
                prepared = prerequisites.prepare_portable_toolchain(self.root)
            self.assertEqual(prepared["target"], f"x86_64-linux-gnu.{expected}")

    def test_extract_xiso_builds_with_the_portable_toolchain(self):
        prepared = {"cmake_args": ["-DCMAKE_TOOLCHAIN_FILE=t.cmake", "-DSDL3_DIR=sdl"]}
        with mock.patch.dict(os.environ, {"DOAXBV_TOOLCHAIN": "zig"}), \
                mock.patch.object(prerequisites, "prepare_portable_toolchain", return_value=prepared), \
                mock.patch.object(prerequisites.shutil, "which", return_value="tool"), \
                mock.patch.object(prerequisites, "run_logged") as run:
            with self.assertRaisesRegex(ValueError, "is missing. Pass --extractor"):
                prerequisites.ensure_extract_xiso(self.root, self.root)
        configure = next(call.args[0] for call in run.call_args_list if "-S" in call.args[0])
        self.assertEqual(configure[-2:], prepared["cmake_args"])

    def test_missing_extractor_is_built_only_when_not_chosen(self):
        recipe = self.root / "tools/game-recipe/recipe.json"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(json.dumps({"files": {}}), encoding="utf-8")
        built = Path("built/extract-xiso")
        for extractor, on_path, expected in (("extract-xiso", False, built),
                                             ("extract-xiso", True, "extract-xiso"),
                                             ("/opt/extract-xiso", False, "/opt/extract-xiso")):
            args = argparse.Namespace(generate_only=True, imported=None, iso=Path("input.iso"), extractor=extractor)
            with self.subTest(extractor=extractor, on_path=on_path), \
                    mock.patch.object(build_game, "ROOT", self.root), \
                    mock.patch.object(build_game.sys, "platform", "linux"), \
                    mock.patch.object(build_game.shutil, "which",
                                      side_effect=lambda name: None if name == "extract-xiso" and not on_path else "tool"), \
                    mock.patch.dict(sys.modules, capstone=mock.Mock()), \
                    mock.patch.object(build_game, "ensure_extract_xiso", return_value=built) as ensure, \
                    mock.patch.object(build_game, "extract", side_effect=ValueError("extraction reached")) as extract:
                with self.assertRaisesRegex(ValueError, "extraction reached"):
                    build_game.build(args, verify_parity=False)
                self.assertEqual(ensure.called, expected is built)
                self.assertEqual(extract.call_args.args[2], expected)


if __name__ == "__main__":
    unittest.main()
