#!/usr/bin/env python3
"""兼容旧入口：未安装时也可直接运行仓库内的 main.py。"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    """调用包内入口。"""
    # pylint: disable=import-outside-toplevel
    from mcp_oi_assistant.__main__ import main as package_main

    package_main()


if __name__ == "__main__":
    main()
