# mcp_oi_assistant
一个为编程竞赛（OI）选手设计的 MCP 服务器，能够安全地编译、运行并测试 C++ 代码。

## 📋 功能列表

### 核心功能
- ✅ **编译 C++ 代码**：自动探测 MinGW 或 PATH 中的 `g++` / `clang++`
- ✅ **运行程序**：提供输入数据，获取程序输出
- ✅ **输出比较**：自动比对实际输出与预期输出
- ✅ **GDB 调试**：支持使用 GDB 进行代码调试
- ✅ **测试用例管理**：读取预设的测试用例文件

### 安全特性
- ✅ **目录隔离**：所有文件操作限制在临时工作区内
- ✅ **命令过滤**：阻止执行危险系统命令
- ✅ **资源限制**：自动限制运行时间、内存和输出大小
- ✅ **文件名消毒**：防止路径遍历攻击

## 🛠️ 依赖的命令行工具
- **C++ 编译器**：自动探测 `mingw64/bin`、`PATH` 中的 `g++` / `clang++`
- **GDB**（可选）：自动探测 `mingw64/bin`、`PATH` 中的 `gdb`

Linux / macOS 直接安装 `g++`、`gdb` 即可；Windows 可将 [MinGW-w64](https://www.mingw-w64.org/) 加入 PATH。也可用环境变量 `OI_CXX`、`OI_GDB`、`OI_MINGW_DIR` 显式指定路径。

## 🚀 快速开始

需要先安装 [uv](https://docs.astral.sh/uv/) 和 `git`（一次性）。之后一条命令即可启动，首次运行会下载并缓存，后续直接复用缓存：

```bash
uvx --from "git+https://github.com/Stellinora/mcp_oi_assistant.git@v1.0.0" mcp-oi-assistant
```

- 首次：拉取源码、构建并安装依赖，缓存到 `uv` 缓存目录。
- 之后：复用已构建的缓存；使用 tag 时仍会向 GitHub 解析一次引用（约 1–2 秒）。
- 完全离线：把 `@v1.0.0` 换成对应提交 SHA（`git rev-parse v1.0.0`），已缓存后可用 `UV_OFFLINE=1` 启动。
- 升级：改用新 tag，或加 `--refresh`；清理缓存：`uv cache prune`。

本地开发也可直接运行：

```bash
uv run mcp-oi-assistant
# 或兼容旧入口
python main.py
```

### 配置到 AI 客户端
MCP 服务器需要配合支持 MCP 协议的 AI 客户端使用，如 Claude Desktop、Cursor、CherryStudio 等。

```json
{
  "mcpServers": {
    "oi-assistant": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Stellinora/mcp_oi_assistant.git@v1.0.0",
        "mcp-oi-assistant"
      ],
      "env": {
        "OI_CXX": "/usr/bin/g++",
        "OI_GDB": "/usr/bin/gdb"
      }
    }
  }
}
```

Windows 示例：

```json
{
  "mcpServers": {
    "oi-assistant": {
      "command": "C:\\Users\\你的用户名\\.local\\bin\\uvx.exe",
      "args": [
        "--from",
        "git+https://github.com/Stellinora/mcp_oi_assistant.git@v1.0.0",
        "mcp-oi-assistant"
      ],
      "env": {
        "OI_CXX": "C:\\mingw64\\bin\\g++.exe",
        "OI_GDB": "C:\\mingw64\\bin\\gdb.exe"
      }
    }
  }
}
```

> GUI 客户端可能找不到 shell PATH 中的 `uvx`，请填写 `which uvx`（Windows 用 `where uvx`）得到的绝对路径。`env` 中的工具路径可省略，省略时自动探测。

## 目录结构
```
mcp_oi_assistant/
├── pyproject.toml       # 打包与依赖定义
├── main.py              # 兼容旧入口
├── config.yaml          # 开发用配置样例
├── src/mcp_oi_assistant/
│   ├── __main__.py      # 启动入口
│   ├── server.py        # MCP 服务器核心
│   ├── runner.py        # 代码运行器
│   ├── security.py      # 安全模块
│   └── config.py        # 配置加载与路径解析
├── tmp/                 # 开发时的临时工作区（自动创建）
└── mingw64/             # MinGW 工具链（可选，需自行放置）
```

## ⚙️ 配置文件

配置文件是可选的，未找到时使用内置默认值。查找顺序：

1. `--config 路径`
2. 环境变量 `OI_CONFIG`
3. 当前目录的 `config.yaml`
4. 用户配置目录下的 `config.yaml`（Linux 通常为 `~/.config/mcp-oi-assistant/`）
5. 仓库根目录的 `config.yaml`（仅开发场景）

主要配置项：

```yaml
# 编译设置
compilation:
  compiler_path: "auto"     # auto 或 g++ 的显式路径
  cpp_standard: "c++17"     # C++标准
  optimization_level: "-O2" # 优化级别

# 运行限制
execution:
  max_time: 5000            # 最大运行时间（毫秒）
  max_memory: 256           # 最大内存（MB）
  max_output_size: 1048576  # 最大输出（1MB）

# 路径设置
paths:
  temp_dir: "./tmp"         # 临时文件目录（相对配置文件所在目录）
  mingw_dir: "./mingw64"    # MinGW目录（可选）
  gdb_path: "auto"          # auto 或 gdb 的显式路径
```

未配置 `paths.temp_dir` 时，默认使用用户缓存目录：

- Linux：`~/.cache/mcp-oi-assistant/tmp`
- macOS：`~/Library/Caches/mcp-oi-assistant/tmp`
- Windows：`%LOCALAPPDATA%\mcp-oi-assistant\tmp`

## 📁 文件存储

程序运行时会自动在临时工作区下创建以下子目录：

```
tmp/
├── sources/     # 源代码文件（*.cpp）
├── execute/     # 可执行文件
├── inputs/      # 输入数据文件（*.in）
├── outputs/     # 程序输出文件（*.out）
└── tests/       # 测试用例文件（*.txt）
```

## 🏷️ 版本策略

- 破坏性变更（改命令名、配置格式、工具参数等）升主版本号
- 新增功能升次版本号
- 修复问题升补丁版本号

## 📄 许可证

MIT License
