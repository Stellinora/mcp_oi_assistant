#!/usr/bin/env python3
"""OI助手MCP服务器启动入口。"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import load_config
from .security import SecurityManager
from .server import OIAssistantServer
from .toolchain import ToolchainError, ToolchainManager


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="OI助手MCP服务器")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check-toolchain",
        action="store_true",
        help="输出工具链状态后退出",
    )
    actions.add_argument(
        "--install-toolchain",
        action="store_true",
        help="下载并安装托管工具链到缓存目录",
    )
    actions.add_argument(
        "--clean-toolchain",
        action="store_true",
        help="删除缓存目录中的托管工具链",
    )
    return parser.parse_args()


def _configure_logging(config: dict) -> None:
    log_level = str(config.get("logging", {}).get("level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_manager(
    config: dict,
    config_path: Optional[Path],
) -> ToolchainManager:
    security = SecurityManager(
        str(config_path) if config_path is not None else None
    )
    config_dir = config_path.parent if config_path is not None else Path.cwd()
    return ToolchainManager(config, config_dir, security.mingw_dir)


def _run_action(
    args: argparse.Namespace,
    config: dict,
    config_path: Optional[Path],
) -> int:
    manager = _build_manager(config, config_path)
    if args.check_toolchain:
        print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
        return 0
    if args.install_toolchain:
        try:
            info = manager.install_managed()
        except (ToolchainError, OSError) as exc:
            print(f"❌ 工具链安装失败: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
        return 0
    removed = manager.clean_managed()
    if removed:
        print(f"已删除: {', '.join(removed)}")
    else:
        print("没有可删除的托管工具链")
    return 0


def main() -> None:
    """主函数：加载配置并启动服务器。"""
    args = parse_args()
    try:
        config, config_path = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    _configure_logging(config)

    if args.check_toolchain or args.install_toolchain or args.clean_toolchain:
        sys.exit(_run_action(args, config, config_path))

    print(f"🚀 OI助手MCP服务器 v{__version__}", file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    print(f"配置文件: {config_path or '内置默认值'}", file=sys.stderr)

    server = OIAssistantServer(
        str(config_path) if config_path is not None else None
    )
    print(f"临时目录: {server.security.temp_dir}", file=sys.stderr)
    print(
        f"MinGW目录: {server.security.mingw_dir or '未使用（走 PATH 探测）'}",
        file=sys.stderr,
    )
    if server.runner.toolchain.detect() is None:
        print("编译器: 未找到，正在后台准备托管工具链", file=sys.stderr)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n👋 服务器已停止", file=sys.stderr)
    except RuntimeError as exc:
        print(f"❌ 服务器运行时错误: {exc}", file=sys.stderr)
        logging.getLogger(__name__).exception("服务器运行时错误")
        sys.exit(1)
    except OSError as exc:
        print(f"❌ 系统错误: {exc}", file=sys.stderr)
        logging.getLogger(__name__).exception("系统错误")
        sys.exit(1)


if __name__ == "__main__":
    main()
