"""MCP服务器主模块，提供OI助手工具。"""

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
import mcp.server.stdio
from mcp import types

from .config import EXE_SUFFIX
from .runner import CodeRunner
from .security import SecurityManager


class OIAssistantServer:
    """MCP服务器，提供代码编译、运行、调试和测试工具。"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化服务器、运行器和安全管理器。"""
        self.runner: CodeRunner = CodeRunner(config_path)
        self.security: SecurityManager = self.runner.security
        # Server 构造只接受名称参数
        self.server: Server = Server("oi-assistant")
        self.setup_handlers()
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def setup_handlers(self) -> None:
        """注册MCP工具处理器。"""
        @self.server.list_tools()
        async def handle_list_tools() -> List[types.Tool]:
            return [
                types.Tool(
                    name="compile_and_run",
                    description="编译并运行C++代码",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "C++源代码",
                            },
                            "input": {
                                "type": "string",
                                "description": "输入数据",
                            },
                            "expected_output": {
                                "type": "string",
                                "description": "预期输出（可选）",
                            },
                            "filename": {
                                "type": "string",
                                "description": "文件名（可选）",
                            },
                            "time_limit": {
                                "type": "integer",
                                "description": "时间限制（毫秒）",
                            },
                            "memory_limit": {
                                "type": "integer",
                                "description": "内存限制（MB）",
                            }
                        },
                        "required": ["code", "input"]
                    }
                ),
                types.Tool(
                    name="debug_with_gdb",
                    description="使用GDB调试C++程序",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "C++源代码"},
                            "gdb_script": {"type": "string", "description": "GDB调试脚本（可选）"}
                        },
                        "required": ["code"]
                    }
                ),
                types.Tool(
                    name="compare_outputs",
                    description="比较两个输出",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "actual": {
                                "type": "string",
                                "description": "实际输出",
                            },
                            "expected": {
                                "type": "string",
                                "description": "预期输出",
                            },
                            "ignore_whitespace": {
                                "type": "boolean",
                                "description": "是否忽略空白字符",
                                "default": True,
                            },
                            "ignore_case": {
                                "type": "boolean",
                                "description": "是否忽略大小写",
                                "default": False,
                            }
                        },
                        "required": ["actual", "expected"]
                    }
                ),
                types.Tool(
                    name="read_test_case",
                    description="读取测试用例文件",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "test_case_id": {"type": "string", "description": "测试用例ID"}
                        },
                        "required": ["test_case_id"]
                    }
                )
            ]

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
            """分发工具调用请求。"""
            session_id = (
                f"session_{int(time.time())}_"
                f"{abs(hash(str(arguments))) % 10000}"
            )
            self.sessions[session_id] = {
                "start_time": time.time(),
                "tool": name,
                "arguments": arguments
            }
            try:
                if name == "compile_and_run":
                    return await self._handle_compile_and_run(arguments, session_id)
                if name == "debug_with_gdb":
                    return await self._handle_debug_with_gdb(arguments, session_id)
                if name == "compare_outputs":
                    return await self._handle_compare_outputs(arguments)
                if name == "read_test_case":
                    return await self._handle_read_test_case(arguments)
                return [types.TextContent(type="text", text=f"未知工具: {name}")]
            except Exception as exc:  # pylint: disable=broad-exception-caught
                print(f"工具执行错误: {exc}", file=sys.stderr)
                return [types.TextContent(type="text", text=f"工具执行错误: {str(exc)}")]
            finally:
                self.sessions.pop(session_id, None)

    async def _handle_compile_and_run(  # pylint: disable=too-many-locals,too-many-statements
        self,
        arguments: Dict[str, Any],
        session_id: str,
    ) -> List[types.TextContent]:
        """处理编译运行请求。"""
        code = arguments.get("code", "")
        input_data = arguments.get("input", "")
        expected_output = arguments.get("expected_output", "")
        filename = arguments.get("filename", f"program_{session_id}")
        time_limit = arguments.get("time_limit")
        memory_limit = arguments.get("memory_limit")

        result_lines = [
            "## 编译与运行报告",
            f"会话ID: {session_id}",
            f"文件名: {filename}",
        ]

        # 1. 编译
        result_lines.append("\n### 1. 编译阶段")
        compile_result = self.runner.compile_cpp(code, filename)
        if compile_result["success"]:
            result_lines.append("✅ 编译成功")
            if compile_result.get("output"):
                result_lines.append("编译输出:")
                result_lines.append("```")
                result_lines.append(compile_result.get("output", ""))
                result_lines.append("```")
        else:
            result_lines.append("❌ 编译失败")
            if compile_result.get("error"):
                result_lines.append("错误信息:")
                result_lines.append("```")
                result_lines.append(str(compile_result.get("error")))
                result_lines.append("```")
            return [types.TextContent(type="text", text="\n".join(result_lines))]

        # 2. 运行
        result_lines.append("\n### 2. 运行阶段")
        run_result = self.runner.run_with_input(
            compile_result['executable'],
            input_data,
            time_limit,
            memory_limit,
        )
        result_lines.append(
            f"运行状态: {'✅ 成功' if run_result.get('success') else '❌ 失败'}"
        )
        result_lines.append(f"时间消耗: {run_result.get('time_used')}ms")
        result_lines.append(f"内存使用: {run_result.get('memory_used')}KB")
        result_lines.append(f"退出代码: {run_result.get('exit_code')}")

        if run_result.get("output"):
            result_lines.append("\n程序输出:")
            result_lines.append("```")
            result_lines.append(run_result.get("output", ""))
            result_lines.append("```")
        if run_result.get("error"):
            result_lines.append("\n错误输出:")
            result_lines.append("```")
            result_lines.append(run_result.get("error", ""))
            result_lines.append("```")

        # 3. 输出比较
        if expected_output:
            result_lines.append("\n### 3. 输出比较")
            compare_result = self.runner.compare_outputs(
                run_result.get('output', ''), expected_output
            )
            if compare_result.get('match'):
                result_lines.append("✅ 输出完全匹配！")
            else:
                result_lines.append("❌ 输出不匹配")
                result_lines.append(
                    f"实际行数: {compare_result.get('actual_line_count')}"
                )
                result_lines.append(
                    f"预期行数: {compare_result.get('expected_line_count')}"
                )
                for diff in compare_result.get('differences', [])[:5]:
                    result_lines.append(f"第{diff['line']}行:")
                    result_lines.append(f"  实际: {diff['actual']}")
                    result_lines.append(f"  预期: {diff['expected']}")
                if len(compare_result.get('differences', [])) > 5:
                    extra = len(compare_result.get('differences', [])) - 5
                    result_lines.append(f"... 还有{extra}处差异未显示")

        # 4. 文件信息
        temp_dir = Path(self.security.temp_dir)
        safe_name = self.security.sanitize_filename(filename)
        result_lines.append("\n### 4. 文件信息")
        src_path = temp_dir / "sources" / f"{safe_name}.cpp"
        exe_path = temp_dir / "execute" / f"{safe_name}{EXE_SUFFIX}"
        in_path = temp_dir / "inputs" / f"{session_id}.in"
        out_path = temp_dir / "outputs" / f"{session_id}.out"
        result_lines.append(f"源代码: `{src_path}`")
        result_lines.append(f"可执行文件: `{exe_path}`")
        result_lines.append(f"输入文件: `{in_path}`")
        result_lines.append(f"输出文件: `{out_path}`")

        return [types.TextContent(type="text", text="\n".join(result_lines))]

    async def _handle_debug_with_gdb(
        self,
        arguments: Dict[str, Any],
        session_id: str,
    ) -> List[types.TextContent]:
        """处理GDB调试请求。"""
        code = arguments.get("code", "")
        gdb_script = arguments.get("gdb_script")
        filename = f"debug_{session_id}"
        compile_result = self.runner.compile_cpp(code, filename)
        if not compile_result['success']:
            msg = "编译失败，无法调试:\n" + str(compile_result.get('error'))
            return [types.TextContent(type="text", text=msg)]

        gdb_result = self.runner.run_gdb(compile_result['executable'], gdb_script)
        result_lines = [
            "## GDB调试报告",
            f"会话ID: {session_id}",
        ]
        if gdb_result['success']:
            result_lines.append("✅ 调试完成")
            if gdb_result['output']:
                result_lines.append("**GDB输出**:")
                result_lines.append("```")
                result_lines.append(gdb_result['output'])
                result_lines.append("```")
        else:
            result_lines.append("❌ 调试失败")
            if gdb_result['error']:
                result_lines.append(f"错误信息:\n```\n{gdb_result['error']}\n```")
        return [types.TextContent(type="text", text="\n".join(result_lines))]

    async def _handle_compare_outputs(self, arguments: Dict[str, Any]) -> List[types.TextContent]:
        """处理输出比较请求。"""
        actual = arguments.get("actual", "")
        expected = arguments.get("expected", "")
        ignore_whitespace = arguments.get("ignore_whitespace", True)
        ignore_case = arguments.get("ignore_case", False)

        compare_result = self.runner.compare_outputs(
            actual,
            expected,
            ignore_whitespace,
            ignore_case,
        )
        result_lines = ["## 输出比较结果\n"]
        if compare_result['match']:
            result_lines.append("✅ 输出完全匹配！")
        else:
            result_lines.append("❌ 输出不匹配")
            result_lines.append(f"实际行数: {compare_result['actual_line_count']}")
            result_lines.append(f"预期行数: {compare_result['expected_line_count']}")
            result_lines.append("差异详情:")
            for diff in compare_result['differences'][:10]:
                result_lines.append(f"第{diff['line']}行:")
                result_lines.append(f"   实际: `{diff['actual']}`")
                result_lines.append(f"   预期: `{diff['expected']}`")
            if len(compare_result['differences']) > 10:
                result_lines.append(f"... 还有{len(compare_result['differences']) - 10}处差异未显示")
        return [types.TextContent(type="text", text="\n".join(result_lines))]

    async def _handle_read_test_case(self, arguments: Dict[str, Any]) -> List[types.TextContent]:
        """读取测试用例文件（支持预定义和自定义文件）。"""
        test_case_id = arguments.get("test_case_id", "")
        safe_id = self.security.sanitize_filename(test_case_id)

        sample_cases = {
            "a+b": {
                "input": "3 5\n",
                "output": "8\n",
                "description": "A+B问题示例"
            },
            "fibonacci": {
                "input": "10\n",
                "output": "55\n",
                "description": "斐波那契数列第10项"
            }
        }

        if safe_id in sample_cases:
            case = sample_cases[safe_id]
            result_lines = [
                f"## 测试用例: {test_case_id}",
                f"描述: {case['description']}",
                "输入:",
                "```",
                case['input'],
                "```",
                "输出:",
                "```",
                case['output'],
                "```"
            ]
        else:
            test_file = self.security.temp_dir / "tests" / f"{safe_id}.txt"
            try:
                if test_file.exists():
                    content = test_file.read_text(encoding='utf-8')
                    result_lines = [f"## 测试用例文件: {test_case_id}", "```", content, "```"]
                else:
                    result_lines = [f"未找到测试用例: {test_case_id}"]
            except (IOError, OSError) as e:
                result_lines = [f"读取测试用例文件失败: {str(e)}"]

        return [types.TextContent(type="text", text="\n".join(result_lines))]

    async def run(self) -> None:
        """启动MCP服务器。"""
        # 修复2：正确的运行方式
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )
