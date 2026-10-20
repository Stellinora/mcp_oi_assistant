"""安全模块：路径消毒、命令验证、目录创建等。"""

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import (
    REPO_ROOT,
    default_temp_dir,
    load_config,
    resolve_config_relative,
)


class SecurityManager:
    """管理安全相关的操作，包括路径验证、文件名消毒等。"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化安全管理器，加载配置文件并创建必要目录。"""
        self.config, self.config_path = load_config(config_path)
        config_dir = (
            self.config_path.parent if self.config_path else Path.cwd()
        )

        paths = self.config['paths']
        temp_value = paths.get('temp_dir')
        self.temp_dir = (
            resolve_config_relative(config_dir, temp_value)
            if temp_value
            else default_temp_dir()
        )

        mingw_value = os.environ.get('OI_MINGW_DIR') or paths.get('mingw_dir')
        if mingw_value:
            self.mingw_dir: Optional[Path] = resolve_config_relative(
                config_dir, mingw_value
            )
        else:
            candidates = [REPO_ROOT / 'mingw64', Path.cwd() / 'mingw64']
            self.mingw_dir = next(
                (path for path in candidates if path.is_dir()), None
            )

        self.create_secure_directories()

    def create_secure_directories(self) -> None:
        """创建安全的临时目录结构，设置适当权限。"""
        directories = [
            self.temp_dir / "compile",
            self.temp_dir / "execute",
            self.temp_dir / "cache",
            self.temp_dir / "inputs",
            self.temp_dir / "outputs",
            self.temp_dir / "sources"
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                os.chmod(directory, 0o700)

    def sanitize_filename(self, filename: str) -> str:
        """对文件名进行消毒，防止路径遍历攻击。"""
        safe_name = re.sub(r'[^\w\.\-]', '_', filename)
        safe_name = safe_name.lstrip('.')
        return safe_name[:100]

    def get_secure_temp_path(self, prefix: str = "oi") -> Path:
        """生成一个安全的临时文件路径（不实际创建文件）。"""
        timestamp = int(time.time() * 1000)
        random_hash = hashlib.md5(str(timestamp).encode()).hexdigest()[:8]
        safe_dir = self.temp_dir / prefix
        safe_dir.mkdir(exist_ok=True)
        return safe_dir / f"{timestamp}_{random_hash}"

    def validate_path(self, path: str) -> Tuple[bool, Optional[Path]]:
        """检查给定路径是否在允许的目录内。"""
        try:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(self.temp_dir.resolve()):
                return True, resolved
            if (
                self.mingw_dir is not None
                and resolved.is_relative_to(self.mingw_dir.resolve())
            ):
                return True, resolved
            return False, None
        except (OSError, ValueError):
            return False, None

    def validate_command(self, command: str) -> bool:
        """检查命令是否包含危险操作。"""
        forbidden = self.config['security']['forbidden_commands']
        for forbidden_cmd in forbidden:
            if forbidden_cmd in command.lower():
                return False

        dangerous_patterns = [
            r'&&\s*rm',
            r';\s*rm',
            r'\|\s*rm',
            r'`.*`',
            r'\$\(.*\)',
            r'>\s*/dev/',
            r'>>\s*/dev/'
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return False
        return True
