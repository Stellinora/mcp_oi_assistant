"""工具链探测与安装辅助逻辑的单元测试。"""

# pylint: disable=protected-access,missing-function-docstring,consider-using-with

import hashlib
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mcp_oi_assistant.config import DEFAULT_CONFIG
from mcp_oi_assistant.toolchain import (
    ToolchainError,
    ToolchainManager,
    load_manifest,
    platform_key,
)


def make_config(**toolchain_updates):
    """构造测试配置。"""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["toolchain"].update(toolchain_updates)
    return config


class ManifestTests(unittest.TestCase):
    """校验随包分发的工具链清单。"""

    def test_all_entries_have_common_fields(self):
        manifest = load_manifest()
        self.assertGreaterEqual(manifest["manifest_version"], 1)
        for name, entry in manifest["platforms"].items():
            with self.subTest(platform=name):
                self.assertIn("id", entry)
                self.assertIn("provider", entry)
                self.assertIn("version", entry)
                self.assertGreater(entry["estimated_bytes"], 0)
                if entry["provider"] == "winlibs":
                    self.assertTrue(entry["url"].startswith("https://"))
                    self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                elif entry["provider"] == "micromamba":
                    self.assertTrue(entry["micromamba_url"].startswith("https://"))
                    self.assertRegex(
                        entry["micromamba_sha256"], r"^[0-9a-f]{64}$"
                    )
                    self.assertTrue(entry["packages"])
                    self.assertTrue(entry["compiler_names"])
                else:
                    self.fail(f"未知 provider: {entry['provider']}")

    def test_platform_key_is_known_or_none(self):
        key = platform_key()
        self.assertTrue(key is None or key in load_manifest()["platforms"])


class DetectionTests(unittest.TestCase):
    """探测优先级与托管工具链指针。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"

    def tearDown(self):
        self.temp.cleanup()

    def _manager(self, prefer="system", **kwargs):
        config = make_config(auto_install=False, prefer=prefer, **kwargs)
        return ToolchainManager(config, self.root)

    @unittest.skipIf(os.name == "nt", "需要 POSIX 可执行脚本")
    def test_env_override_wins(self):
        compiler = self.root / "fake-g++"
        compiler.write_text("#!/bin/sh\necho 'g++ fake 1.0'\n", encoding="utf-8")
        compiler.chmod(0o700)
        manager = self._manager()
        with mock.patch.dict(os.environ, {"OI_CXX": str(compiler)}, clear=False):
            info = manager.detect()
        self.assertIsNotNone(info)
        self.assertEqual(info.compiler, str(compiler.resolve()))
        self.assertEqual(info.source, "env")

    @unittest.skipIf(os.name == "nt", "需要 POSIX 可执行脚本")
    def test_managed_toolchain_can_be_preferred(self):
        prefix = self.root / "managed"
        bin_dir = prefix / "bin"
        bin_dir.mkdir(parents=True)
        compiler = bin_dir / "g++"
        compiler.write_text("#!/bin/sh\necho 'g++ managed 1.0'\n", encoding="utf-8")
        compiler.chmod(0o700)
        self.cache.mkdir()
        (self.cache / "current.json").write_text(
            json.dumps(
                {
                    "compiler": str(compiler),
                    "gdb": None,
                    "prefix": str(prefix),
                    "provider": "test",
                    "version": "test",
                }
            ),
            encoding="utf-8",
        )
        manager = self._manager(prefer="managed", cache_dir=str(self.cache))
        info = manager.detect()
        self.assertIsNotNone(info)
        self.assertEqual(info.source, "managed")
        self.assertEqual(info.prefix, str(prefix))

    @unittest.skipIf(os.name == "nt", "需要 POSIX 可执行脚本")
    def test_finds_triple_prefixed_compiler(self):
        prefix = self.root / "env"
        (prefix / "bin").mkdir(parents=True)
        compiler = prefix / "bin" / "arm64-apple-darwin23.0-clang++"
        compiler.write_text("#!/bin/sh\necho 'clang version test'\n", encoding="utf-8")
        compiler.chmod(0o700)
        manager = self._manager()
        self.assertEqual(
            manager._find_compiler_fallback(prefix),
            str(compiler.resolve()),
        )

    def test_auto_install_disabled_does_not_start(self):
        manager = self._manager()
        with mock.patch.object(manager, "detect", return_value=None):
            manager.start_background_install()
        self.assertEqual(manager.state, "idle")

    def test_auto_install_starts_background(self):
        manager = ToolchainManager(
            make_config(auto_install=True, prefer="managed"),
            self.root,
        )
        called = threading.Event()

        def fake_install():
            called.set()

        with mock.patch.object(manager, "detect", return_value=None), mock.patch.object(
            manager, "install_managed", side_effect=fake_install
        ):
            manager.start_background_install()
            self.assertTrue(called.wait(2))
            manager._installed_event.wait(2)
        self.assertEqual(manager.state, "ready")

    @unittest.skipIf(os.name == "nt", "需要 POSIX 可执行脚本")
    def test_clean_managed_removes_prefix(self):
        self.cache.mkdir()
        prefix = self.cache / "managed"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "g++").write_text("#!/bin/sh\n", encoding="utf-8")
        current = self.cache / "current.json"
        current.write_text(
            json.dumps({"prefix": str(prefix), "compiler": "x"}),
            encoding="utf-8",
        )
        manager = self._manager(cache_dir=str(self.cache))
        removed = manager.clean_managed()
        self.assertEqual(removed, [str(prefix)])
        self.assertFalse(prefix.exists())
        self.assertFalse(current.exists())


class DownloadTests(unittest.TestCase):
    """下载校验与镜像。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.manager = ToolchainManager(
            make_config(auto_install=False, cache_dir=str(self.cache)),
            self.root,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_local_mirror_and_checksum(self):
        payload = b"toolchain-bytes"
        source = self.root / "archive.zip"
        source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.root / "downloads" / "archive.zip"
        with mock.patch.dict(
            os.environ, {"OI_TOOLCHAIN_MIRROR": str(self.root)}, clear=False
        ):
            self.manager._download(
                "https://example.invalid/archive.zip",
                destination,
                digest,
            )
        self.assertEqual(destination.read_bytes(), payload)
        self.assertFalse(
            (destination.parent / (destination.name + ".part")).exists()
        )

    def test_checksum_mismatch_removes_partial_file(self):
        source = self.root / "archive.zip"
        source.write_bytes(b"bad")
        destination = self.root / "downloads" / "archive.zip"
        with mock.patch.dict(
            os.environ, {"OI_TOOLCHAIN_MIRROR": str(self.root)}, clear=False
        ):
            with self.assertRaises(ToolchainError):
                self.manager._download(
                    "https://example.invalid/archive.zip",
                    destination,
                    "0" * 64,
                )
        self.assertFalse(destination.exists())


class WinlibsInstallTests(unittest.TestCase):
    """用本地假压缩包验证 WinLibs 安装流程。"""

    def test_extracts_compiler(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache"
            manager = ToolchainManager(
                make_config(auto_install=False, cache_dir=str(cache)),
                root,
            )
            entry = {
                "id": "winlibs-test",
                "compiler_name": "g++.exe",
                "gdb_name": "gdb.exe",
                "url": "https://example.invalid/winlibs.zip",
            }
            archive = cache / "downloads" / "winlibs-test.zip"
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("mingw64/bin/g++.exe", b"fake compiler")
                bundle.writestr("mingw64/bin/gdb.exe", b"fake gdb")
            entry["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()

            work_dir = root / "work"
            work_dir.mkdir()
            final_dir = cache / "winlibs-test"
            info = manager._install_winlibs(entry, work_dir, final_dir)
            self.assertEqual(
                Path(info.compiler), final_dir / "bin" / "g++.exe"
            )
            self.assertTrue(Path(info.compiler).is_file())
            self.assertEqual(info.source, "managed")


class ExtractTests(unittest.TestCase):
    """压缩包路径与链接安全。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_zip_rejects_path_traversal(self):
        archive = self.root / "evil.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escape.txt", "x")
        with self.assertRaises(ToolchainError):
            ToolchainManager._safe_extract_zip(archive, self.root / "out")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_zip_rejects_symlink(self):
        archive = self.root / "link.zip"
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(info, "target")
        with self.assertRaises(ToolchainError):
            ToolchainManager._safe_extract_zip(archive, self.root / "out")

if __name__ == "__main__":
    unittest.main()
