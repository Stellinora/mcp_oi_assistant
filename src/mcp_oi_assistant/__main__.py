#!/usr/bin/env python3
"""OI助手MCP服务器启动入口。"""

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import load_config
from .server import OIAssistantServer


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="OI助手MCP服务器")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def main() -> None:
    """主函数：加载配置并启动服务器。"""
    args = parse_args()
    try:
        config, config_path = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    log_level = str(config.get("logging", {}).get("level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
