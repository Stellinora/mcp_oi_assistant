"""配置加载与路径解析。"""

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from platformdirs import user_cache_dir, user_config_dir

APP_NAME = "mcp-oi-assistant"
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent.parent
EXE_SUFFIX = ".exe" if os.name == "nt" else ""

DEFAULT_CONFIG: Dict[str, Any] = {
    "security": {
        "allowed_paths": ["./tmp", "./mingw64/bin"],
        "forbidden_commands": ["rm -rf", "format", "del", "chmod 777"],
    },
    "execution": {
        "max_time": 5000,
        "max_memory": 256,
        "max_output_size": 1048576,
    },
    "compilation": {
        "compiler_path": "auto",
        "cpp_standard": "c++17",
        "optimization_level": "-O2",
    },
    "paths": {
        "temp_dir": None,
        "mingw_dir": None,
        "gdb_path": "auto",
        "cache_expiry": 300,
    },
    "toolchain": {
        "auto_install": True,
        "prefer": "system",
        "cache_dir": None,
        "install_timeout": 900,
        "mirror": None,
    },
    "logging": {
        "level": "INFO",
        "audit_log": None,
    },
}


def default_temp_dir() -> Path:
    """返回用户缓存目录下的默认临时工作区。"""
    return Path(user_cache_dir(APP_NAME)) / "tmp"


def resolve_config_path(config_path: Optional[str] = None) -> Optional[Path]:
    """按优先级查找配置文件，未找到时返回 None。"""
    explicit = config_path or os.environ.get("OI_CONFIG")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        candidates.extend([
            Path.cwd() / "config.yaml",
            Path(user_config_dir(APP_NAME, appauthor=False)) / "config.yaml",
            REPO_ROOT / "config.yaml",
        ])

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved

    if explicit:
        raise FileNotFoundError(f"配置文件 {explicit} 未找到")
    return None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置，override 覆盖 base。"""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(
    config_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """加载配置，返回内置默认值合并后的字典和实际使用的文件路径。"""
    path = resolve_config_path(config_path)
    config = deepcopy(DEFAULT_CONFIG)
    if path is None:
        return config, None

    with open(path, "r", encoding="utf-8") as config_file:
        loaded = yaml.safe_load(config_file) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"配置文件 {path} 的内容必须是 YAML 映射")

    return _deep_merge(config, loaded), path


def resolve_config_relative(base_dir: Path, value: str) -> Path:
    """将配置中的相对路径按配置文件所在目录解析。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()
