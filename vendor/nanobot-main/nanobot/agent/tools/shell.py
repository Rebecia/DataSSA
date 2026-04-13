"""Shell execution tool.

这份工具给 LLM 提供一个名为 `exec` 的能力：执行一条 shell 命令并返回输出。

阅读重点（你学 agent 工具体系时可以抓这几点）：
- `name/description/parameters`：这三者决定 LLM “看到的工具签名”（function calling schema）
- `execute()`：真正执行命令（异步子进程 + 超时控制 + 输出截断）
- `_guard_command()`：安全护栏（黑名单/白名单/内网 URL/工作区路径限制）
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
    ):
        # 默认超时（秒）。调用时也可以传入 timeout 覆盖（但会被 _MAX_TIMEOUT 限制）
        self.timeout = timeout

        # 默认工作目录：如果 execute(...) 没传 working_dir，就用这个；
        # 再不行就用 os.getcwd()（当前进程目录）
        self.working_dir = working_dir

        # deny_patterns：危险命令的正则黑名单。命中就直接拒绝执行。
        # 语法点：`a or b` —— 如果 a 是 None/空，就使用 b（这里就是默认黑名单）
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]

        # allow_patterns：允许命令的白名单（可选）。
        # 如果给了白名单，则必须至少匹配一条白名单才允许执行。
        self.allow_patterns = allow_patterns or []

        # restrict_to_workspace：如果为 True，会限制命令不能访问工作目录之外的路径（尽力而为）
        self.restrict_to_workspace = restrict_to_workspace

        # path_append：给子进程 PATH 追加一段（常用于让工具可用，例如自定义 bin 目录）
        self.path_append = path_append

    @property
    def name(self) -> str:
        # 工具名（LLM 调用时用的名字）：tool_calls 里会出现 {"name": "exec", ...}
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        # 给 LLM 看的描述：告诉它这个工具做什么、要谨慎
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        # 这是“工具参数 schema”（OpenAI function calling 风格）。
        # LLM 会根据它生成 tool_calls 的 arguments。
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, **kwargs: Any,
    ) -> str:
        # 选择工作目录：优先用调用参数 working_dir，其次用初始化时的 self.working_dir，
        # 再不行用当前进程目录。
        cwd = working_dir or self.working_dir or os.getcwd()

        # 安全检查：如果命中危险模式/越界路径/内网 URL 等，直接返回错误文本，不执行。
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        # 计算有效超时：调用时 timeout 覆盖默认 self.timeout，并且不会超过 _MAX_TIMEOUT
        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        # 给子进程准备环境变量（复制当前进程的 env，避免污染全局）
        env = os.environ.copy()
        if self.path_append:
            # 语法点：os.pathsep 在 POSIX 是 ":"，在 Windows 是 ";"
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            # create_subprocess_shell：通过 shell 执行命令（允许管道/重定向等）
            # stdout/stderr 都用 PIPE：我们要捕获输出，返回给 LLM。
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                # process.communicate()：等待进程结束，并收集 stdout/stderr
                # wait_for：加超时，避免命令卡死占用资源
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                # 超时：杀掉进程，并尽力等待它退出（给 5 秒）
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts: list[str] = []  # 拼接 stdout/stderr/exit code

            if stdout:
                # decode：把 bytes 转成 str；errors="replace" 防止非 UTF-8 输出导致崩溃
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            # exit code：命令返回码（0 通常表示成功）
            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Head + tail truncation to preserve both start and end of output
            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            # 兜底：子进程创建失败、权限问题、cwd 不存在等都会走到这里
            return f"Error executing command: {str(e)}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()  # 清理两端空白
        lower = cmd.lower()  # 做成小写，便于正则匹配（大小写不敏感）

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            # 如果启用了白名单：必须至少匹配一个允许模式
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        # 安全：禁止命令里出现内网/私网 URL（例如 169.254.* / 10.* / localhost 等）
        from nanobot.security.network import contains_internal_url
        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            # 最简单的路径穿越检测（尽力而为）：禁止 ../ 或 ..\ 出现在命令里
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            # cwd_path：工作目录的绝对路径
            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue
                # 如果命令里出现了绝对路径，并且它不在 cwd 下面：拒绝执行
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # 从命令字符串里“尽力”提取路径，用于 restrict_to_workspace 检查。
        # 注意：这是正则启发式，不可能覆盖所有 shell 语法（所以叫 best-effort）。
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths
