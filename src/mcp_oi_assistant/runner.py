"""代码运行模块：编译、执行、调试C++程序。"""

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

import psutil

# 在 Windows 下没有 resource 模块，尽量兼容运行环境
try:
    import resource  # type: ignore
except (ImportError, ModuleNotFoundError):
    resource = None  # type: ignore

from .config import EXE_SUFFIX, load_config
from .security import SecurityManager
from .toolchain import ToolchainManager


class CodeRunner:
    """处理C++代码的编译、运行、调试和输出比较。"""

    def __init__(self, config_path: Optional[str] = None) -> None:
        """初始化运行器、安全管理器与工具链管理器。"""
        self.config, self.config_path = load_config(config_path)
        self.config_dir = (
            self.config_path.parent if self.config_path else Path.cwd()
        )
        self.security = SecurityManager(
            str(self.config_path) if self.config_path else None
        )
        self.toolchain = ToolchainManager(
            self.config,
            self.config_dir,
            self.security.mingw_dir,
        )
        self.toolchain.start_background_install()

    def _resolve_compiler(self) -> Optional[str]:
        """返回当前可用的 C++ 编译器路径。"""
        info = self.toolchain.detect()
        return info.compiler if info else None

    def _resolve_gdb(self) -> Optional[str]:
        """返回当前可用的 GDB 路径。"""
        info = self.toolchain.detect()
        return info.gdb if info else None

    def _compiler_error(self) -> str:
        """构造工具链不可用的错误信息。"""
        if self.toolchain.state == "installing":
            return "工具链正在自动安装，请稍后重试"
        if self.toolchain.last_error:
            return f"工具链自动安装失败: {self.toolchain.last_error}"
        return (
            "未找到 C++ 编译器。请安装 g++/MinGW，"
            "或运行 `mcp-oi-assistant --install-toolchain` 自动安装"
        )

    def compile_cpp(self, code: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """编译C++代码，返回包含成功标志、可执行文件路径和错误信息的字典。"""
        if filename is None:
            temp_path = self.security.get_secure_temp_path("compile")
            cpp_file = temp_path.with_suffix('.cpp')
            exe_file = temp_path.with_name(temp_path.name + EXE_SUFFIX)
        else:
            safe_name = self.security.sanitize_filename(filename)
            cpp_file = self.security.temp_dir / "sources" / f"{safe_name}.cpp"
            exe_file = self.security.temp_dir / "execute" / f"{safe_name}{EXE_SUFFIX}"
        cpp_file.parent.mkdir(parents=True, exist_ok=True)
        # 确保可执行文件父目录存在
        exe_file.parent.mkdir(parents=True, exist_ok=True)
        cpp_file.write_text(code, encoding='utf-8')

        timeout = float(self.config.get("toolchain", {}).get("install_timeout", 900))
        info = self.toolchain.ensure_ready(timeout)
        if info is None:
            return {
                'success': False,
                'error': self._compiler_error(),
                'executable': None
            }

        compiler = info.compiler
        std = self.config['compilation']['cpp_standard']
        optimization = self.config['compilation']['optimization_level']

        compile_cmd = [
            compiler,
            str(cpp_file),
            '-std=' + std,
            optimization,
            '-o', str(exe_file),
            '-Wall',
            '-Wextra',
            '-Werror'
        ]

        try:
            result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                cwd=cpp_file.parent,
                env=self.toolchain.child_env(),
            )
            return {
                'success': result.returncode == 0,
                'executable': str(exe_file) if result.returncode == 0 else None,
                'output': result.stdout,
                'error': result.stderr,
                'return_code': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {
                'success': False,
                'error': '编译超时（30秒）',
                'executable': None
            }
        except (OSError, subprocess.SubprocessError) as exc:  # 更具体的异常捕获
            return {
                'success': False,
                'error': f'编译异常: {str(exc)}',
                'executable': None
            }

    def _get_memory_usage(self, pid: int) -> int:
        """获取进程内存使用量（KB）。"""
        try:
            process = psutil.Process(pid)
            return process.memory_info().rss // 1024
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return 0
        except OSError:
            return 0

    def run_with_input(  # pylint: disable=subprocess-popen-preexec-fn,too-many-locals,too-many-statements
        self,
        executable: str,
        input_data: str,
        time_limit: Optional[int] = None,
        memory_limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """运行可执行文件，提供输入数据，限制时间和内存。"""
        if not self.security.validate_command(executable):
            return {'success': False, 'error': '不安全的可执行文件路径'}

        # 处理可选参数：如果为 None，则使用配置默认值
        if time_limit is not None:
            actual_time_limit = time_limit
        else:
            actual_time_limit = self.config['execution']['max_time']

        if memory_limit is not None:
            actual_memory_limit = memory_limit
        else:
            actual_memory_limit = self.config['execution']['max_memory']

        input_file = self.security.get_secure_temp_path("inputs").with_suffix('.in')
        input_file.write_text(input_data, encoding='utf-8')
        output_file = input_file.with_suffix('.out')

        # 资源限制函数（仅 Unix）
        def set_limits() -> None:
            # 仅在类 Unix 平台并且 resource 模块可用时设置限制
            if os.name == 'nt' or resource is None:
                return
            try:
                cpu_seconds = int(actual_time_limit // 1000 + 1)
                resource.setrlimit(
                    resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds)
                )
            except (OSError, ValueError):
                pass
            try:
                memory_bytes = int(actual_memory_limit * 1024 * 1024)
                resource.setrlimit(
                    resource.RLIMIT_AS, (memory_bytes, memory_bytes)
                )
            except (OSError, ValueError):
                # macOS 等平台可能不支持 RLIMIT_AS
                pass

        start_time = time.time()
        try:
            # 确保输入/输出目录存在
            input_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.parent.mkdir(parents=True, exist_ok=True)

            with open(input_file, 'r', encoding='utf-8') as infile:
                with open(output_file, 'w', encoding='utf-8') as outfile:
                    process = subprocess.Popen(  # pylint: disable=consider-using-with
                        [executable],
                        stdin=infile,
                        stdout=outfile,
                        stderr=subprocess.PIPE,
                        preexec_fn=(
                            set_limits if os.name != 'nt' else None
                        ),  # pylint: disable=subprocess-popen-preexec-fn
                        text=True,
                        cwd=self.security.temp_dir / "execute",
                        env=self.toolchain.child_env(),
                    )
                try:
                    # 使用 actual_time_limit（毫秒）计算超时秒数
                    timeout_seconds = actual_time_limit / 1000 + 1
                    _, stderr = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, stderr = process.communicate()
                    return {
                        'success': False,
                        'error': f'运行超时（{actual_time_limit}ms）',
                        'time_used': actual_time_limit,
                        'output': None,
                        'exit_code': -1
                    }
                exit_code = process.returncode

            elapsed_time = (time.time() - start_time) * 1000
            output_content = output_file.read_text(encoding='utf-8', errors='ignore')

            max_output = self.config['execution']['max_output_size']
            if len(output_content.encode('utf-8')) > max_output:
                output_content = output_content[:max_output//4] + "\n... (输出被截断)"

            memory_used = 0
            if getattr(process, 'pid', None):
                memory_used = self._get_memory_usage(process.pid)

            return {
                'success': exit_code == 0,
                'output': output_content,
                'error': stderr,
                'time_used': int(elapsed_time),
                'memory_used': memory_used,
                'exit_code': exit_code,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                'success': False,
                'error': f'运行异常: {str(exc)}',
                'output': None,
                'time_used': 0
            }

    def compare_outputs(
        self,
        actual: str,
        expected: str,
        ignore_whitespace: bool = True,
        ignore_case: bool = False
    ) -> Dict[str, Any]:
        """比较实际输出和预期输出，返回差异详情。"""
        if ignore_whitespace:
            actual = ' '.join(actual.split())
            expected = ' '.join(expected.split())
        if ignore_case:
            actual = actual.lower()
            expected = expected.lower()

        actual_lines = actual.strip().split('\n')
        expected_lines = expected.strip().split('\n')
        differences = []
        max_lines = max(len(actual_lines), len(expected_lines))

        for i in range(max_lines):
            actual_line = actual_lines[i] if i < len(actual_lines) else ""
            expected_line = expected_lines[i] if i < len(expected_lines) else ""
            if actual_line != expected_line:
                differences.append({
                    'line': i + 1,
                    'actual': actual_line,
                    'expected': expected_line
                })

        return {
            'match': len(differences) == 0,
            'differences': differences,
            'actual_line_count': len(actual_lines),
            'expected_line_count': len(expected_lines)
        }

    def run_gdb(self, executable: str, script: Optional[str] = None) -> Dict[str, Any]:
        """使用GDB调试程序，返回调试输出。"""
        if not self.security.validate_command(executable):
            return {'success': False, 'error': '不安全的可执行文件路径'}

        gdb_script = self.security.get_secure_temp_path("gdb").with_suffix('.gdb')
        if script:
            gdb_script.write_text(script, encoding='utf-8')
        else:
            default_script = "\n".join([
                "set pagination off",
                "break main",
                "run",
                "backtrace",
                "info registers",
                "x/10i $pc",
                "quit",
            ])
            gdb_script.write_text(default_script, encoding='utf-8')

        gdb_path = self._resolve_gdb()
        if not gdb_path:
            return {
                'success': False,
                'error': '未找到 gdb，请安装或设置 paths.gdb_path / OI_GDB',
                'output': None
            }

        try:
            cmd = [gdb_path, '-x', str(gdb_script), executable, '--batch']
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                cwd=self.security.temp_dir / "execute",
                env=self.toolchain.child_env(),
            )
            return {
                'success': result.returncode == 0,
                'output': result.stdout,
                'error': result.stderr,
                'return_code': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'GDB调试超时', 'output': None}
        except (OSError, subprocess.SubprocessError) as exc:
            return {'success': False, 'error': f'GDB调试异常: {str(exc)}', 'output': None}
