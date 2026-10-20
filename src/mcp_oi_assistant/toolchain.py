"""C++ 工具链探测、下载与缓存管理。"""

import contextlib
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from platformdirs import user_cache_dir

from .config import APP_NAME, EXE_SUFFIX, resolve_config_relative

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).with_name("toolchain_manifest.json")
DOWNLOAD_CHUNK = 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
MAX_EXTRACTED_BYTES = 6 * 1024 * 1024 * 1024


class ToolchainError(RuntimeError):
    """工具链探测或安装失败。"""


@dataclass
class ToolchainInfo:
    """解析后的工具链信息。"""

    compiler: str
    source: str
    gdb: Optional[str] = None
    prefix: Optional[str] = None
    provider: Optional[str] = None
    version: Optional[str] = None
    bin_dirs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "compiler": self.compiler,
            "gdb": self.gdb,
            "source": self.source,
            "prefix": self.prefix,
            "provider": self.provider,
            "version": self.version,
            "bin_dirs": self.bin_dirs,
        }


def platform_key() -> Optional[str]:  # pylint: disable=too-many-return-statements
    """返回当前平台的 manifest 键。"""
    system = sys.platform
    machine = platform.machine().lower()
    if system.startswith(("win", "cygwin", "msys")):
        if machine in ("amd64", "x86_64"):
            return "win-64"
        return None
    if system.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-64"
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
        return None
    if system == "darwin":
        if machine in ("x86_64", "amd64"):
            return "osx-64"
        if machine in ("arm64", "aarch64"):
            return "osx-arm64"
    return None


def load_manifest() -> Dict[str, Any]:
    """读取随包分发的工具链清单。"""
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as manifest_file:
            return json.load(manifest_file)
    except (OSError, ValueError) as exc:
        raise ToolchainError(f"读取工具链清单失败: {exc}") from exc


def _is_executable(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ToolchainManager:  # pylint: disable=too-many-instance-attributes
    """负责工具链探测、后台安装与子进程环境构造。"""

    def __init__(
        self,
        config: Dict[str, Any],
        config_dir: Path,
        repo_mingw_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.config_dir = config_dir
        self.tc_config = config.get("toolchain", {}) or {}
        self.repo_mingw_dir = repo_mingw_dir
        self.cache_root = self._resolve_cache_root()
        self._info: Optional[ToolchainInfo] = None
        self._state_lock = threading.Lock()
        self._install_lock = threading.Lock()
        self._installed_event = threading.Event()
        self._state = "idle"
        self._error: Optional[str] = None

    # ------------------------------------------------------------------ 路径
    def _resolve_cache_root(self) -> Path:
        value = self.tc_config.get("cache_dir")
        if value:
            return resolve_config_relative(self.config_dir, str(value))
        return Path(self._user_cache_dir()) / "toolchains"

    @staticmethod
    def _user_cache_dir() -> str:
        return user_cache_dir(APP_NAME)

    @property
    def current_file(self) -> Path:
        """托管工具链的当前指针文件。"""
        return self.cache_root / "current.json"

    @property
    def state(self) -> str:
        """当前安装状态。"""
        with self._state_lock:
            return self._state

    @property
    def last_error(self) -> Optional[str]:
        """最近一次安装错误。"""
        with self._state_lock:
            return self._error

    # ------------------------------------------------------------------ 探测
    def _candidate_path(self, value: Optional[str]) -> Optional[str]:
        if not value or value == "auto":
            return None
        candidate = Path(value).expanduser()
        if candidate.is_absolute() or os.sep in value or "/" in value:
            if candidate.is_file():
                return str(candidate.resolve())
            return None
        found = shutil.which(value)
        return found

    def _find_in_dir(self, root: Optional[Path], names: List[str]) -> Optional[str]:
        if root is None:
            return None
        for name in names:
            for candidate in (root / name, root / "bin" / name):
                if _is_executable(candidate):
                    return str(candidate.resolve())
        return None

    def _find_compiler_fallback(self, root: Path) -> Optional[str]:
        """在 bin 中按名称模式查找编译器，兼容带目标三元组前缀的命名。"""
        for pattern in ("clang++*", "*clang++*", "g++*", "*g++*"):
            candidates = sorted(
                (
                    path
                    for path in (root / "bin").glob(pattern)
                    if _is_executable(path)
                ),
                key=lambda path: (len(path.name), path.name),
            )
            if candidates:
                return str(candidates[0].resolve())
        return None

    def _managed_info(self) -> Optional[ToolchainInfo]:
        try:
            with open(self.current_file, "r", encoding="utf-8") as current:
                data = json.load(current)
        except (OSError, ValueError):
            return None
        compiler = data.get("compiler")
        if not compiler or not _is_executable(Path(compiler)):
            return None
        prefix = data.get("prefix")
        gdb = data.get("gdb")
        if gdb and not _is_executable(Path(gdb)):
            gdb = None
        return ToolchainInfo(
            compiler=compiler,
            gdb=gdb,
            source="managed",
            prefix=prefix,
            provider=data.get("provider"),
            version=self._compiler_version(compiler) or data.get("version"),
            bin_dirs=self._bin_dirs(compiler, gdb, prefix),
        )

    @staticmethod
    def _bin_dirs(
        compiler: Optional[str],
        gdb: Optional[str],
        prefix: Optional[str],
    ) -> List[str]:
        dirs: List[str] = []
        for value in (compiler, gdb):
            if value:
                parent = str(Path(value).resolve().parent)
                if parent not in dirs:
                    dirs.append(parent)
        if prefix:
            bin_dir = str((Path(prefix) / "bin").resolve())
            if bin_dir not in dirs:
                dirs.append(bin_dir)
        return dirs

    def _compiler_version(self, compiler: str) -> Optional[str]:
        try:
            result = subprocess.run(
                [compiler, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        line = (result.stdout or result.stderr or "").splitlines()
        return line[0].strip() if line else None

    def _find_system_compiler(self) -> Optional[str]:
        compiler = shutil.which(f"g++{EXE_SUFFIX}") or shutil.which("g++")
        if not compiler:
            compiler = shutil.which(f"clang++{EXE_SUFFIX}") or shutil.which(
                "clang++"
            )
        return compiler

    def detect(  # pylint: disable=too-many-branches,too-many-statements
        self,
    ) -> Optional[ToolchainInfo]:
        """探测可用工具链，不触发下载。"""
        if self._info is not None:
            return self._info

        compiler: Optional[str] = None
        source = ""
        managed = self._managed_info()
        prefer_managed = self.tc_config.get("prefer") == "managed"

        env_cxx = os.environ.get("OI_CXX")
        if env_cxx:
            compiler = self._candidate_path(env_cxx)
            source = "env" if compiler else ""

        if not compiler:
            configured = (self.config.get("compilation", {}) or {}).get(
                "compiler_path"
            )
            compiler = self._candidate_path(configured)
            source = "config" if compiler else ""

        toolchain_dir = os.environ.get("OI_TOOLCHAIN_DIR")
        if not compiler and toolchain_dir:
            compiler = self._find_in_dir(
                Path(toolchain_dir).expanduser(),
                [f"g++{EXE_SUFFIX}", "g++", f"clang++{EXE_SUFFIX}", "clang++"],
            )
            source = "toolchain-dir" if compiler else ""

        mingw_env = os.environ.get("OI_MINGW_DIR")
        if not compiler and mingw_env:
            compiler = self._find_in_dir(
                Path(mingw_env).expanduser(),
                [f"g++{EXE_SUFFIX}", "g++"],
            )
            source = "env-mingw" if compiler else ""

        if not compiler and prefer_managed and managed is not None:
            self._info = managed
            return managed

        if not compiler:
            compiler = self._find_system_compiler()
            source = "system" if compiler else ""

        if not compiler and managed is not None:
            self._info = managed
            return managed

        if not compiler and self.repo_mingw_dir is not None:
            compiler = self._find_in_dir(
                self.repo_mingw_dir,
                [f"g++{EXE_SUFFIX}", "g++"],
            )
            source = "repo" if compiler else ""

        if not compiler:
            return None

        gdb: Optional[str] = None
        env_gdb = os.environ.get("OI_GDB")
        if env_gdb:
            gdb = self._candidate_path(env_gdb)
        if not gdb:
            configured_gdb = (self.config.get("paths", {}) or {}).get("gdb_path")
            gdb = self._candidate_path(configured_gdb)
        if not gdb and toolchain_dir:
            gdb = self._find_in_dir(
                Path(toolchain_dir).expanduser(),
                [f"gdb{EXE_SUFFIX}", "gdb"],
            )
        if not gdb and mingw_env:
            gdb = self._find_in_dir(
                Path(mingw_env).expanduser(),
                [f"gdb{EXE_SUFFIX}", "gdb"],
            )
        if not gdb:
            gdb = shutil.which(f"gdb{EXE_SUFFIX}") or shutil.which("gdb")
        if not gdb and managed is not None:
            gdb = managed.gdb
        if not gdb and self.repo_mingw_dir is not None:
            gdb = self._find_in_dir(
                self.repo_mingw_dir,
                [f"gdb{EXE_SUFFIX}", "gdb"],
            )

        use_managed = managed is not None and managed.compiler == compiler
        prefix = managed.prefix if use_managed and managed else None

        self._info = ToolchainInfo(
            compiler=compiler,
            gdb=gdb,
            source="managed" if use_managed else source,
            prefix=prefix,
            provider=managed.provider if use_managed and managed else None,
            version=self._compiler_version(compiler),
            bin_dirs=self._bin_dirs(compiler, gdb, prefix),
        )
        return self._info

    # ------------------------------------------------------------------ 环境
    def child_env(self) -> Dict[str, str]:
        """构造供编译器、运行程序和 GDB 使用的子进程环境。"""
        env = os.environ.copy()
        info = self.detect()
        dirs = info.bin_dirs if info else []
        env["PATH"] = os.pathsep.join(
            [*dirs, env.get("PATH", "")]
        ).strip(os.pathsep)

        if info and info.prefix:
            prefix = Path(info.prefix)
            env.setdefault("CONDA_PREFIX", str(prefix))
            include = str(prefix / "include")
            lib = str(prefix / "lib")
            env["CPLUS_INCLUDE_PATH"] = os.pathsep.join(
                [include, env.get("CPLUS_INCLUDE_PATH", "")]
            ).strip(os.pathsep)
            if os.name != "nt":
                env["LIBRARY_PATH"] = os.pathsep.join(
                    [lib, env.get("LIBRARY_PATH", "")]
                ).strip(os.pathsep)
            lib_var = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
            if os.name != "nt":
                env[lib_var] = os.pathsep.join(
                    [lib, env.get(lib_var, "")]
                ).strip(os.pathsep)
        return env

    # ------------------------------------------------------------------ 自动安装
    def _auto_install_enabled(self) -> bool:
        override = os.environ.get("OI_TOOLCHAIN_AUTO_INSTALL")
        if override is not None:
            return override.lower() not in ("0", "false", "no", "off")
        return bool(self.tc_config.get("auto_install", True))

    @staticmethod
    def _offline() -> bool:
        value = os.environ.get("OI_TOOLCHAIN_OFFLINE", "")
        return value.lower() in ("1", "true", "yes", "on")

    def start_background_install(self) -> None:
        """在没有编译器时启动后台安装（如果允许）。"""
        if self.detect() is not None:
            return
        if not self._auto_install_enabled() or self._offline():
            return
        with self._state_lock:
            if self._state in ("installing", "failed"):
                return
            self._state = "installing"
            self._error = None
            self._installed_event.clear()
        thread = threading.Thread(
            target=self._install_worker,
            name="toolchain-install",
            daemon=True,
        )
        thread.start()

    def _install_worker(self) -> None:
        try:
            self.install_managed()
            with self._state_lock:
                self._state = "ready"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("工具链安装失败: %s", exc)
            with self._state_lock:
                self._state = "failed"
                self._error = str(exc)
        finally:
            self._installed_event.set()

    def ensure_ready(self, timeout: Optional[float] = None) -> Optional[ToolchainInfo]:
        """确保工具链可用；必要时等待后台安装。"""
        info = self.detect()
        if info is not None:
            return info

        install_timeout = timeout
        if install_timeout is None:
            install_timeout = float(self.tc_config.get("install_timeout", 900))
        if self.state == "idle":
            self.start_background_install()
        if self.state == "installing":
            logger.info("工具链缺失，等待自动安装完成（最多 %.0f 秒）", install_timeout)
            self._installed_event.wait(install_timeout)
        if self.state == "failed":
            return None
        self._info = None
        return self.detect()

    # ------------------------------------------------------------------ 安装
    def manifest_entry(self, key: Optional[str] = None) -> Dict[str, Any]:
        """返回当前平台的 manifest 条目。"""
        key = key or platform_key()
        if key is None:
            raise ToolchainError(
                f"当前平台 {sys.platform}/{platform.machine()} 暂不支持托管工具链"
            )
        entry = load_manifest().get("platforms", {}).get(key)
        if not entry:
            raise ToolchainError(f"工具链清单中没有平台 {key} 的条目")
        return entry

    def install_managed(self, force: bool = False) -> ToolchainInfo:
        """下载并安装托管工具链到缓存目录。"""
        with self._install_lock:
            if not force:
                existing = self._managed_info()
                if existing is not None:
                    self._info = existing
                    return existing

            entry = self.manifest_entry()
            self._prepare_cache_dir(entry)
            entry_id = entry["id"]
            final_dir = self.cache_root / entry_id

            with self._install_lock_file():
                if not force:
                    existing = self._managed_info()
                    if existing is not None:
                        self._info = existing
                        return existing

                work_dir = self.cache_root / f".install-{entry_id}-{os.getpid()}"
                if work_dir.exists():
                    shutil.rmtree(work_dir, ignore_errors=True)
                work_dir.mkdir(parents=True, exist_ok=True)
                try:
                    if entry["provider"] == "winlibs":
                        info = self._install_winlibs(entry, work_dir, final_dir)
                    elif entry["provider"] == "micromamba":
                        info = self._install_micromamba(entry, final_dir)
                    else:
                        raise ToolchainError(
                            f"未知工具链提供方: {entry['provider']}"
                        )
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

                self._write_current(entry, info)
                self._info = info
                with self._state_lock:
                    self._state = "ready"
                    self._error = None
                self._installed_event.set()
                logger.info("工具链已安装: %s (%s)", info.compiler, entry_id)
                return info

    @contextlib.contextmanager
    def _install_lock_file(self) -> Iterator[None]:
        """跨进程安装锁，避免多个 MCP 进程同时写入缓存。"""
        lock_path = self.cache_root / "install.lock"
        deadline = time.time() + 1800
        while True:
            try:
                descriptor = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0
                if age > 1800:
                    lock_path.unlink(missing_ok=True)
                    continue
                if time.time() > deadline:
                    raise ToolchainError("等待其他进程安装工具链超时") from None
                time.sleep(1)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def _prepare_cache_dir(self, entry: Dict[str, Any]) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(self.cache_root, 0o700)
            except OSError:
                pass
        required = int(entry.get("estimated_bytes", 0))
        if required:
            free = shutil.disk_usage(self.cache_root).free
            if free < required:
                raise ToolchainError(
                    f"缓存目录空间不足：需要约 {required // (1024 ** 3)}GB，"
                    f"剩余 {free // (1024 ** 3)}GB"
                )

    def _install_winlibs(
        self,
        entry: Dict[str, Any],
        work_dir: Path,
        final_dir: Path,
    ) -> ToolchainInfo:
        archive = self.cache_root / "downloads" / f"{entry['id']}.zip"
        self._download(entry["url"], archive, entry["sha256"])
        extract_dir = work_dir / "extract"
        self._safe_extract_zip(archive, extract_dir)

        candidates = sorted(
            (
                path
                for path in extract_dir.rglob(entry["compiler_name"])
                if path.parent.name == "bin"
            ),
            key=lambda path: len(path.parts),
        )
        if not candidates:
            raise ToolchainError("下载的 MinGW 压缩包中未找到 g++")

        prefix = candidates[0].parent.parent
        self._replace_dir(prefix, final_dir)

        installed_compiler = final_dir / "bin" / entry["compiler_name"]
        if not installed_compiler.is_file():
            installed_compiler = next(
                final_dir.rglob(entry["compiler_name"])
            )
        gdb_path = final_dir / "bin" / entry["gdb_name"]
        gdb = str(gdb_path) if _is_executable(gdb_path) else None

        return ToolchainInfo(
            compiler=str(installed_compiler),
            gdb=gdb,
            source="managed",
            prefix=str(final_dir),
            provider="winlibs",
            version=entry.get("version"),
            bin_dirs=self._bin_dirs(
                str(installed_compiler), gdb, str(final_dir)
            ),
        )

    def _install_micromamba(
        self,
        entry: Dict[str, Any],
        final_dir: Path,
    ) -> ToolchainInfo:
        mamba = self._ensure_micromamba(entry)
        mamba_root = self.cache_root / "mamba-root"
        mamba_root.mkdir(parents=True, exist_ok=True)

        if final_dir.exists():
            shutil.rmtree(final_dir)

        env = os.environ.copy()
        env["MAMBA_ROOT_PREFIX"] = str(mamba_root)
        command = [
            str(mamba),
            "--no-rc",
            "create",
            "-y",
            "-p",
            str(final_dir),
            "-c",
            "conda-forge",
            "--override-channels",
            *entry["packages"],
        ]
        logger.info("正在创建托管工具链环境: %s", " ".join(entry["packages"]))
        output = sys.stderr or subprocess.DEVNULL
        try:
            result = subprocess.run(  # pylint: disable=subprocess-run-check
                command,
                stdout=output,
                stderr=output,
                env=env,
                check=False,
            )
            if result.returncode != 0:
                raise ToolchainError(
                    f"micromamba 创建环境失败（退出码 {result.returncode}）"
                )

            compiler = self._find_in_dir(final_dir, entry["compiler_names"])
            if compiler is None:
                compiler = self._find_compiler_fallback(final_dir)
            if compiler is None:
                raise ToolchainError("conda 环境中未找到 C++ 编译器")
            gdb = self._find_in_dir(final_dir, [entry["gdb_name"]])
        except Exception:
            shutil.rmtree(final_dir, ignore_errors=True)
            raise

        return ToolchainInfo(
            compiler=compiler,
            gdb=gdb,
            source="managed",
            prefix=str(final_dir),
            provider="micromamba",
            version=entry.get("version"),
            bin_dirs=self._bin_dirs(compiler, gdb, str(final_dir)),
        )

    def _ensure_micromamba(self, entry: Dict[str, Any]) -> Path:
        binary = self.cache_root / "bin" / f"micromamba{EXE_SUFFIX}"
        if _is_executable(binary):
            return binary

        self._download(
            entry["micromamba_url"],
            binary,
            entry["micromamba_sha256"],
        )
        if os.name != "nt":
            os.chmod(binary, 0o700)
        return binary

    @staticmethod
    def _replace_dir(source: Path, destination: Path) -> None:
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    def _write_current(self, entry: Dict[str, Any], info: ToolchainInfo) -> None:
        payload = {
            "platform": platform_key(),
            "id": entry["id"],
            "provider": info.provider,
            "version": info.version,
            "prefix": info.prefix,
            "compiler": info.compiler,
            "gdb": info.gdb,
            "source": entry.get("source"),
            "installed_at": int(time.time()),
        }
        self.current_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.current_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as current:
            json.dump(payload, current, ensure_ascii=False, indent=2)
        os.replace(temp_file, self.current_file)

    # ------------------------------------------------------------------ 下载
    def _download(  # pylint: disable=too-many-locals
        self,
        url: str,
        destination: Path,
        sha256: str,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and _sha256(destination) == sha256.lower():
            logger.info("复用已下载文件: %s", destination.name)
            return

        source = self._mirror_url(url)
        if self._offline():
            raise ToolchainError(
                f"离线模式且缓存中没有 {destination.name}，请先联网安装"
            )
        logger.info("正在获取 %s", source)
        temp_file = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        downloaded = 0
        last_report = 0
        try:
            if "://" in source:
                request = urllib.request.Request(
                    source,
                    headers={"User-Agent": f"{APP_NAME}/toolchain"},
                )
                reader = urllib.request.urlopen(request, timeout=60)
            else:
                reader = open(source, "rb")
            with reader as response, open(temp_file, "wb") as output:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_report >= 10 * DOWNLOAD_CHUNK:
                        last_report = downloaded
                        logger.info(
                            "已获取 %.1f MB",
                            downloaded / (1024 * 1024),
                        )
            actual = digest.hexdigest()
            if actual != sha256.lower():
                raise ToolchainError(
                    f"SHA256 校验失败: 期望 {sha256}，实际 {actual}"
                )
            os.replace(temp_file, destination)
            logger.info("下载完成: %s", destination.name)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ToolchainError(f"下载失败 {source}: {exc}") from exc
        finally:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)

    def _mirror_url(self, url: str) -> str:
        mirror = os.environ.get("OI_TOOLCHAIN_MIRROR") or self.tc_config.get(
            "mirror"
        )
        if not mirror:
            return url
        filename = Path(urllib.parse.urlparse(url).path).name
        if "://" in str(mirror):
            return urllib.parse.urljoin(str(mirror).rstrip("/") + "/", filename)
        return str((Path(mirror).expanduser() / filename).resolve())

    # ------------------------------------------------------------------ 解压
    @staticmethod
    def _safe_extract_zip(archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ToolchainError("压缩包条目过多")
            total = sum(member.file_size for member in members)
            if total > MAX_EXTRACTED_BYTES:
                raise ToolchainError("压缩包解压体积过大")
            for member in members:
                name = member.filename.replace("\\", "/")
                target = (destination / name).resolve()
                if (
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or not target.is_relative_to(root)
                ):
                    raise ToolchainError(f"压缩包包含非法路径: {member.filename}")
                mode = member.external_attr >> 16
                if (mode & 0o170000) == 0o120000:
                    raise ToolchainError(f"压缩包包含符号链接: {member.filename}")
            bundle.extractall(destination)

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        """返回用于诊断的工具链状态。"""
        info = self.detect()
        key = platform_key()
        managed = self._managed_info()
        return {
            "platform": key or f"{sys.platform}/{platform.machine()}",
            "supported": key is not None,
            "cache_dir": str(self.cache_root),
            "auto_install": self._auto_install_enabled(),
            "offline": self._offline(),
            "state": self.state,
            "error": self.last_error,
            "managed_installed": managed is not None,
            "compiler": info.compiler if info else None,
            "gdb": info.gdb if info else None,
            "source": info.source if info else None,
            "version": info.version if info else None,
            "prefix": info.prefix if info else None,
        }

    def clean_managed(self) -> List[str]:
        """删除当前托管工具链，返回删除的目录。"""
        removed: List[str] = []
        try:
            with open(self.current_file, "r", encoding="utf-8") as current:
                data = json.load(current)
        except (OSError, ValueError):
            data = {}
        prefix = data.get("prefix")
        if prefix:
            path = Path(prefix)
            if path.is_dir() and path.resolve().is_relative_to(
                self.cache_root.resolve()
            ):
                shutil.rmtree(path)
                removed.append(str(path))
        self.current_file.unlink(missing_ok=True)
        self._info = None
        with self._state_lock:
            self._state = "idle"
            self._error = None
        return removed
