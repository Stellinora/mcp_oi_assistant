"""三端托管工具链集成检查，供 CI 手动触发。"""

import json
import tempfile
from pathlib import Path

from mcp_oi_assistant.config import DEFAULT_CONFIG
from mcp_oi_assistant.runner import CodeRunner

CODE = """#include <iostream>
int main() {
    int a, b;
    std::cin >> a >> b;
    std::cout << a + b << std::endl;
    return 0;
}
"""


def main() -> None:
    """使用托管工具链编译并运行 A+B。"""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["toolchain"].update({"prefer": "managed", "auto_install": False})

    config_dir = Path(tempfile.mkdtemp(prefix="oi-ci-"))
    config_path = config_dir / "config.yaml"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    runner = CodeRunner(str(config_path))
    info = runner.toolchain.detect()
    assert info is not None, runner.toolchain.status()
    assert info.source == "managed", info.to_dict()

    compiled = runner.compile_cpp(CODE, "ci_integration")
    assert compiled["success"], compiled

    ran = runner.run_with_input(compiled["executable"], "3 5\n")
    assert ran["success"], ran
    assert ran["output"].strip() == "8", ran

    print(
        json.dumps(
            {"toolchain": info.to_dict(), "output": ran["output"].strip()},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
