"""CLI commands for nanobot.

学习视角（读这份文件你应该抓住什么）
1) CLI 入口如何把配置/组件“装配”起来（Typer 命令：onboard/agent/gateway/…）
2) 交互式 CLI 如何做到“边输出边输入”不互相打架（prompt_toolkit + Rich）
3) AgentLoop 与 MessageBus 的两种运行形态：
   - `nanobot agent`：可直接 `process_direct()`（单次模式），也可走 bus（交互模式）
   - `nanobot gateway`：常驻服务，channels 通过 bus 与 AgentLoop 解耦

常见语法点提示（本文件里会频繁出现）
- `@app.command()` / `@app.callback()`：装饰器（decorator），用于注册 CLI 子命令/回调
- `typer.Option(...)` / `typer.Argument(...)`：声明 CLI 参数（含默认值、help）
- `async def` + `await`：异步函数与等待（I/O 密集：读写队列、网络、子进程）
- `with ...:`：上下文管理器（例如 Rich capture / patch_stdout / spinner.pause）
- 嵌套函数（inner function）与闭包（closure）：在函数内部定义函数并捕获外部变量
- `:=`（walrus operator，海象运算符）：赋值表达式（例如 `if mt := ...:`）
"""

import asyncio
from contextlib import contextmanager, nullcontext

import os
import select
import signal
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from nanobot import __logo__, __version__
from nanobot.cli.stream import StreamRenderer, ThinkingSpinner
from nanobot.config.paths import get_workspace_path
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates

# 命令名称为 "nanobot"，如果没有这个，则打印 help 退出
app = typer.Typer(
    name="nanobot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} nanobot - Personal AI Assistant",
    # 没命令时打印 help
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output.

    中文解释：模型在输出期间，用户可能还在敲键盘；这些按键会“堆积”在 TTY 缓冲区，
    造成下一次 prompt 时出现残留输入。这里的目标就是清掉这些残留。

    语法点：
    - `try/except`：兼容不同平台与不同 stdin 状态（非 TTY、权限受限等）
    - `select.select`：非阻塞轮询 stdin 是否可读
    """
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.).
    """
    # 把终端（TTY）的设置恢复回程序启动前的样子，避免你退出交互模式后终端变得“不能正常输入/不回显/光标错乱”。
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        # termios 是 POSIX（Linux/macOS）终端控制库；Windows 可能没有或行为不同，所以用 try/except 保证跨平台不崩。
        import termios
        #  _SAVED_TERM_ATTRS 不为空时，得到一份“终端模式快照”，放到模块变量 _SAVED_TERM_ATTRS 中
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
        # sys.stdin.fileno()：拿到 stdin 的文件描述符（fd），TCSADRAIN：一种应用方式，含义是“等输出写完再切换设置”，避免切换时把输出/输入搞乱
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history.

    中文解释：初始化 prompt_toolkit 的会话对象（带历史文件），用于交互式聊天输入。

    语法点：
    - `global _PROMPT_SESSION, _SAVED_TERM_ATTRS`：在函数内修改模块级变量必须声明 global
    - `Path(...).mkdir(parents=True, exist_ok=True)`：创建目录（幂等）
    """
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from nanobot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _make_console() -> Console:
    """创建一个写到 stdout 的 Rich Console。

    中文解释：Rich 的 Console 默认可能写到 stderr，这里显式绑定 stdout，避免混乱。
    """
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely.

    中文解释：prompt_toolkit 在交互模式下需要“安全打印”，直接用 Rich 输出可能破坏输入行。
    做法：用 Rich 的 `Console.capture()` 先渲染到 ANSI 字符串，再交给 prompt_toolkit 打印。

    语法点：
    - `with ansi_console.capture() as capture:`：上下文管理器，退出时自动收集输出
    - 传入 `render_fn(ansi_console)`：高阶函数（把 console 作为参数传给外部渲染函数）
    """
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Render assistant response with consistent terminal styling.

    中文解释：统一 CLI 下 assistant 的展示样式（logo + 内容 + 空行）。
    """
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    console.print()
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render output as Rich renderable (Text/Markdown).

    中文解释：有些输出（例如命令结果）不适合 markdown 渲染（会折叠换行），因此用 metadata 控制。

    语法点：
    - `metadata` 可能为 None，因此用 `(metadata or {})` 安全取值
    """
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling.

    中文解释：在 prompt_toolkit 的输入框存在时，不能直接 print；需要 `run_in_terminal`
    把输出放到“安全的终端输出区域”。

    语法点：
    - `async def` + `await run_in_terminal(...)`：异步等待终端安全输出
    - 内部嵌套函数 `_write()`：闭包捕获 `text`
    """
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling.

    中文解释：同上，但用于最终回复的完整渲染（可能是 Markdown）。
    """
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} nanobot[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed.

    中文解释：模型在思考时会有 spinner；打印进度行前暂停 spinner，避免输出错位。

    语法点：
    - `with thinking.pause() if thinking else nullcontext():`：条件上下文管理器写法
    """
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed.

    中文解释：交互模式的进度输出也要暂停 spinner，并通过 `_print_interactive_line` 安全打印。
    """
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat.

    中文解释：交互模式支持多种退出命令（exit/quit/:q 等），统一归一到小写集合判断。
    """
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:  # 防御式：必须先 init prompt session
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            # 语法点：`await _PROMPT_SESSION.prompt_async(...)` 是异步读一行输入
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        # 语法点：`raise X from exc` 保留原始异常上下文（链式异常）
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    """Typer 的 eager 回调：实现 `--version` 立即退出。

    语法点：
    - `raise typer.Exit()`：中断 CLI 正常流程，立即退出
    """
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    # 这是布尔开关，检测到--version/-v 时 Click 会把flag 设为 true 传给 version_callback
    # 否则就是默认值，一般是 None 或者 False，这里是 None
    # 不触发 verison 的时候则 pass 无逻辑

    # is_eager=True：提前处理。即使用户还输入了子命令（如 agent/gateway），也会先执行这个 callback（并且它 raise Exit 后就结束了）
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    # `typer.Option`：声明可选参数（带 help 文案）
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """
    - `--config` 指定路径：将其设为当前实例配置（支持多实例）
    - `--wizard`：进入交互式向导（可选择是否保存）
    - `--workspace` 根据config 变化不断覆写

    语法点：
    - 嵌套函数 `_apply_workspace_override`：闭包，复用 workspace 覆写逻辑
    - `Path(...).expanduser().resolve()`：处理 `~` 并转绝对路径
    """
    from nanobot.config.loader import get_config_path, load_config, save_config, set_config_path
    from nanobot.config.schema import Config

    if config:
        # Common CLI pitfall: typing `-config` (single dash) gets parsed by Click/Typer as `-c onfig`.
        # That can accidentally create a file literally named `onfig` in the current directory.
        config_path = Path(config).expanduser().resolve()
        # 把当前进程使用的配置文件路径”设置成全局变量，
        # 之后所有的运行信息都会存储在这个目录下，区分多实例
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        # 未指定则使用默认路径 `~/.nanobot/config.json`
        config_path = get_config_path()

    # 如果命令行传了 --workspace，则覆盖 config 里 agents.defaults.workspace
    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

       # 已存在配置：要么走 wizard（读取现有配置让向导编辑），要么询问覆盖/刷新
    if config_path.exists():
        if wizard:
            # 走 wizard，读取现有配置，load_config解析成 config 对象，传参
            config = _apply_workspace_override(load_config(config_path))
        else:
            # 走简单交互确认
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
            console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
            # 要覆盖，传入全新的空的
            if typer.confirm("Overwrite?"):
                # 终端显示效果：Overwrite? [y/N]: yes 返回 true，no 返回 false
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            # 否则，只是读取一次旧的，再保存一次
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        # 不存在配置：创建默认 Config
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")
     
     # 交互式向导：允许用户逐步填写配置，最后再决定是否保存到 config_path
    if wizard:
       
        from nanobot.cli.onboard_wizard import run_onboard

        try:
            # 调用向导函数，梳理配置
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'nanobot onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    # 把“已发现的 channels 默认配置”注入到 config.json 里
    _onboard_plugins(config_path)  

    # 创建workspace目录
    workspace_path = get_workspace_path(config.workspace_path)
    # 首次创建显示
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    # 同步模板文件，只创建缺失的不覆盖，tools，readme，memeory 等
    sync_workspace_templates(workspace_path)
    
    # 下一步命令行提示
    agent_cmd = 'nanobot agent -m "Hello!"'
    gateway_cmd = "nanobot gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    if wizard:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]")


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config.

    中文解释：用于“刷新配置”场景：如果新版本新增了字段，希望补齐默认值，但不能覆盖用户已有设置。

    语法点：
    - 递归函数：处理嵌套 dict
    """
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins).

    中文解释：扫描所有可用 channel（含插件），把它们的默认配置写到 `config.json` 的 channels 段。
    已存在的 channel 配置不会被覆盖，只会补齐缺失字段。

    语法点：
    - `dict.setdefault`：如果 key 不存在则写入默认值并返回该值
    - 读写 JSON：`json.load` / `json.dump`
    """
    import json

    from nanobot.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config.

    中文解释：根据 config（model/provider/api_key/api_base…）选择并实例化具体 LLMProvider。
    这里是“面试可讲点”：通过 provider 抽象把不同模型/网关统一成一个接口。

    语法点：
    - 多分支选择（if/elif/else）
    - 运行时 import：避免不必要依赖在未使用时加载
    """
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model  # 例如 "anthropic/claude-..." 或 "openai/gpt-..."
    provider_name = config.get_provider_name(model)  # 根据 model 与配置推断 provider（或用户强制指定）
    p = config.get_provider(model)  # 拿到该 provider 对应的 ProviderConfig（api_key/api_base/headers）

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        # OAuth provider：不依赖 API Key（走 OAuth token）
        provider = OpenAICodexProvider(default_model=model)
    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    elif provider_name == "custom":
        # 自定义 OpenAI 兼容端点：绕过 LiteLLM，直接用 CustomProvider
        from nanobot.providers.custom_provider import CustomProvider
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    # Azure OpenAI: direct Azure OpenAI endpoint with deployment name
    elif provider_name == "azure_openai":
        # Azure：要求 api_key + api_base；model 字段一般作为 deployment 名
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.nanobot/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    # OpenVINO Model Server: direct OpenAI-compatible endpoint at /v3
    elif provider_name == "ovms":
        # OpenVINO Model Server：也是 OpenAI 兼容，但默认 base 在 /v3
        from nanobot.providers.custom_provider import CustomProvider
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v3",
            default_model=model,
        )
    else:
        # 默认：走 LiteLLMProvider（统一适配多家模型/网关）
        from nanobot.providers.litellm_provider import LiteLLMProvider
        from nanobot.providers.registry import find_by_name
        spec = find_by_name(provider_name)
        if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and (spec.is_oauth or spec.is_local)):
            console.print("[red]Error: No API key configured.[/red]")
            console.print("Set one in ~/.nanobot/config.json under providers section")
            raise typer.Exit(1)
        provider = LiteLLMProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            provider_name=provider_name,
        )

    defaults = config.agents.defaults  # 把 config 的默认 generation 参数下发到 provider
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace.

    中文解释：运行时加载配置的统一入口（给 agent/gateway 等命令复用）。
    - `--config`：切换配置路径（并设置为全局 config_path，影响数据目录）
    - `--workspace`：覆盖 agents.defaults.workspace（临时生效）

    语法点：
    - `Path.exists()`：校验文件存在，否则 `raise typer.Exit(1)`
    """
    from nanobot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file.

    中文解释：对旧配置字段做温和提示（不强制迁移/不报错），帮助用户清理无效 key。
    """
    import json
    from nanobot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )



# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """

    多渠道服务模式：
    - AgentLoop 常驻消费 inbound 队列
    - ChannelManager 启动所有已启用渠道（并把消息 publish 到 bus）
    - CronService / HeartbeatService 提供定时与周期性任务入口

    语法点：
    - `asyncio.gather(...)`：并发运行多个长期任务
    """
    from nanobot.agent.loop import AgentLoop  
    from nanobot.bus.queue import MessageBus 
    from nanobot.channels.manager import ChannelManager  # 启动/管理渠道，并分发 outbound
    from nanobot.config.paths import get_cron_dir  # cron 存储目录（跟 config 实例相关）
    from nanobot.cron.service import CronService  # 定时任务服务
    from nanobot.cron.types import CronJob  # cron job 数据结构（回调参数类型）
    from nanobot.heartbeat.service import HeartbeatService  # 周期性任务服务
    from nanobot.session.manager import SessionManager  # 会话持久化（JSONL）

    if verbose:
        # verbose=True：打开更详细的标准 logging（有些依赖库用 logging，不用 loguru）
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)  # 读取配置（可用 CLI 覆盖 workspace）
    port = port if port is not None else config.gateway.port  # port 参数优先，否则用 config.gateway.port

    console.print(
        f"{__logo__} Starting nanobot gateway version {__version__} on port {port}..."
    )  # 启动提示（仅输出）

    sync_workspace_templates(config.workspace_path)  # 同步文件，确保 workspace 模板文件存在（AGENTS.md/MEMORY.md 等）
    bus = MessageBus()  # 创建消息总线：channels->inbound->agent->outbound->channels
    provider = _make_provider(config)  
    session_manager = SessionManager(config.workspace_path)  # 会话存储（sessions/*.jsonl）

    cron_store_path = get_cron_dir() / "jobs.json"  # cron job 持久化文件路径
    cron = CronService(cron_store_path)  # cron 服务：负责“到点触发”，不负责“怎么执行”

    # Create agent with cron service
    agent = AgentLoop(  # 核心 agent 引擎：消费 inbound、调用模型、执行工具、产出 outbound
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )


    # 当某个定时任务到点触发时，把它“伪装成一条用户消息”交给 AgentLoop 去跑，
    # 然后（可选）把结果再推送回对应渠道/用户。
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool
        from nanobot.utils.evaluator import evaluate_response

        reminder_note = (  # 把“定时任务触发”包装成一条 user message，交给 agent 正常处理
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")  # CronTool：需要标记“我现在在跑 cron”，避免递归创建 job
        cron_token = None  # token 用于之后恢复 CronTool 的上下文状态
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            # 直接调用 agent（不依赖某个具体 channel 的 inbound），但仍可指定要投递到哪个 channel/chat
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}", # 让每个 cron job 有独立会话上下文。
                # 下面两个参数用于指定要投递到的 channel/chat，默认是 cli 直接推送到用户
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            # 恢复 CronTool 上下文，reset 避免 cron 执行期间再次创建 cron 或形成递归/循环触发（属于安全/防重入设计）。
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        response = resp.content if resp else ""  
        message_tool = agent.tools.get("message")  # MessageTool：若本轮已主动发送，则 cron 不再重复推送
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            # deliver=True：表示允许“真正推送到用户渠道”
            # 这里用 evaluator 再判一次：避免把无关内容打扰用户（比如只是内部日志）
            should_notify = await evaluate_response(
                response, job.payload.message, provider, agent.model,
            )
            if should_notify:
                from nanobot.bus.events import OutboundMessage
                # 值得通知时，将信息发送到渠道
                await bus.publish_outbound(OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                ))
        return response
    cron.on_job = on_cron_job  # 将 cron 的触发回调绑定到这里定义的协程函数

    # Create channel manager
    channels = ChannelManager(config, bus)  # 创建渠道管理器（会读取 config，实例化 enabled 的渠道）

    def _pick_heartbeat_target() -> tuple[str, str]:
        """选一个“能把消息送达用户”的目标地址（channel, chat_id）。"""
        enabled = set(channels.enabled_channels)
        for item in session_manager.list_sessions():  # 最近会话优先，找一个“可路由”的 channel/chat_id
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        return "cli", "direct"  # 兜底：没有可用外部渠道就回到本地 CLI

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop. 执行 heartbeat 任务"""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            # 心跳任务默认不把 progress 推给用户（避免打扰），这里用空回调吞掉 on_progress
            pass
        # 执行 agentloop
        resp = await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )
        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel. 推送 heartbeat 结果"""
        from nanobot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # 没有外部渠道可投递，就不通知（避免在本地刷屏）
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response)
        )

    hb_cfg = config.gateway.heartbeat  # 心跳配置（是否启用、间隔秒数）
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()  # 启动定时器（后台 ticking）
            await heartbeat.start()  # 启动心跳任务（后台 ticking）
            # gather：并发运行两个“长驻任务”
            # - agent.run()：消费 inbound，处理消息并产出 outbound
            # - channels.start_all()：启动各渠道（各自监听平台消息并 publish_inbound）
            await asyncio.gather(agent.run(), channels.start_all())
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback
            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()  # 等待 agent 的后台归档任务收尾，并关闭 MCP
            heartbeat.stop()  # 请求停止心跳
            cron.stop()  # 请求停止 cron
            agent.stop()  # 请求停止 agent.run() 主循环
            await channels.stop_all()  # 停止渠道与 outbound dispatcher

    asyncio.run(run())




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show nanobot runtime logs during chat"),
):
    """Interact with the agent directly.

    中文解释：CLI 单机模式，分两种用法：
    1) `--message`：单次调用（直接走 `AgentLoop.process_direct`，不走 bus）
    2) 不传 message：交互模式（走 bus，与 gateway/渠道一致），支持流式与进度提示

    语法点：
    - `async def` 嵌套函数：在命令内部组织异步流程（run_once / run_interactive）
    - `signal.signal(...)`：注册信号处理器（Ctrl+C 等）
    - `asyncio.Event()`：用于“等待一轮对话结束”
    """
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.paths import get_cron_dir
    from nanobot.cron.service import CronService

    config = _load_runtime_config(config, workspace)  # 读取配置（支持 --config/--workspace 覆盖）
    sync_workspace_templates(config.workspace_path)  # 确保 workspace 基础文件存在（AGENTS.md/MEMORY.md 等）

    bus = MessageBus()  # 交互模式会使用 bus 模拟“channel → agent → channel”的完整链路
    provider = _make_provider(config)  # 创建 LLM provider

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        # 语法点：按参数开关 loguru 日志（便于调试 agent/tool/provider 的内部日志）
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    
    _thinking: ThinkingSpinner | None = None  # “思考中”的 spinner（可能为空；打印进度时会临时暂停它）

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        # on_progress 回调：AgentLoop 在“想提示用户当前进展”时会调用它（例如工具提示、非流式的中间输出）
        ch = agent_loop.channels_config 
        if ch and tool_hint and not ch.send_tool_hints:  # 配置关闭“工具提示”时跳过
            return  
        if ch and not tool_hint and not ch.send_progress:  # 配置关闭“进度文本”时跳过
            return  
        _print_cli_progress_line(content, _thinking)  # 打印一行进度（内部会暂停 spinner，避免输出错位）

    # 单次模式：`nanobot agent -m "..."`（不进入交互循环） 不需要 bus
    if message:  
        async def run_once():
            # 语法点：在同步命令里定义 async，用 asyncio.run 执行（让代码能用 await）
            # 负责拼接流式输出并渲染 markdown
            renderer = StreamRenderer(render_markdown=markdown)  
            # 直接把消息交给 AgentLoop 处理（不通过 MessageBus）
            response = await agent_loop.process_direct(  
                message, session_id,  # message：用户输入；session_id：会话 key（如 cli:direct）
                on_progress=_cli_progress,  # 中间进度回调（工具提示/非流式中间输出）
                on_stream=renderer.on_delta,  # 流式片段（delta）回调
                on_stream_end=renderer.on_end,  # 流式段落结束回调（可能 resuming=True）
            )
            if not renderer.streamed:  # 如果没有真正走流式（比如 provider 不支持/未启用）
                await renderer.close()  # 关闭 renderer（释放资源/停止状态）
                _print_agent_response(  # 用统一样式打印最终回复
                    response.content if response else "",  # response 可能为 None，做个兜底
                    render_markdown=markdown,  # 是否 markdown 渲染
                    metadata=response.metadata if response else None,  # 透传渲染控制信息
                )
            await agent_loop.close_mcp()  # 收尾：关闭 MCP 连接（如果有）

        asyncio.run(run_once())  # 语法点：启动事件循环

    # 交互模式：`nanobot agent`（不带 -m），走 bus
    # “channel → bus → agent → bus → channel” 链路
    else:
        from nanobot.bus.events import InboundMessage 
        _init_prompt_session()  # 初始化 prompt_toolkit 会话（历史/编辑/粘贴等）
        console.print(  # 打印交互模式提示
            f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n"
        )

        if ":" in session_id:  # session_id 形如 "cli:direct"：显式指定 channel 前缀
            cli_channel, cli_chat_id = session_id.split(":", 1)  # 语法点：split(":", 1) 只切一次
        else:
            cli_channel, cli_chat_id = "cli", session_id  # 没有前缀时默认走 cli 渠道

        def _handle_signal(signum, frame):
            # 信号处理器：收到 SIGINT/SIGTERM 时恢复终端并退出
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)  # 直接结束进程（CLI 常见做法）

        signal.signal(signal.SIGINT, _handle_signal)  # Ctrl+C
        signal.signal(signal.SIGTERM, _handle_signal)  # 进程终止
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)  # 终端挂断（部分系统）
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)  # 忽略 SIGPIPE，防止输出到断开的管道时直接崩

        async def run_interactive():
            # 启动 agent_loop.run() 常驻消费 inbound（这里是 bus 模式）
            # create_task 会把协程“挂到事件循环并发执行”，当前函数不会被阻塞
            bus_task = asyncio.create_task(agent_loop.run())  # 后台：AgentLoop 从 inbound 队列取消息并处理

            # asyncio.Event() 异步版的开关灯
            # unset，await event.wait() 会一直等；set 则表示已完成，await event.wait()会立刻返回
            turn_done = asyncio.Event()
            turn_done.set()  

            # 用列表暂存“非流式模式下的最终回复”（只取第一条）
            turn_response: list[tuple[str, dict]] = []

            # StreamRenderer：把多段 delta 拼成一段完整输出并做 markdown 渲染
            renderer: StreamRenderer | None = None

            async def _consume_outbound():
                # 消费 outbound 队列：把 agent 的输出渲染到终端，并驱动 turn_done 事件
                while True:
                    try:
                        # wait_for + timeout：避免永久阻塞，方便响应取消/退出
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        # 流式处理
                        if msg.metadata.get("_stream_delta"):
                            # 流式片段：交给 renderer 追加显示（不会结束本轮）
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            # 流式边界：通知 renderer 一段流结束（resuming=True 表示后面还会继续）
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            # 表示“本轮最终回复已走流式”，turn_done 用来解除输入循环的等待。
                            # 但有些 provider 虽然走了 stream API，却没有实际发出 delta；
                            # 这时需要回退为“非流式最终回复”，否则用户看不到任何输出。
                            if not turn_done.is_set() and msg.content and (not renderer or not renderer.streamed):
                                meta = dict(msg.metadata or {})
                                meta.pop("_streamed", None)
                                turn_response.append((msg.content, meta))
                            turn_done.set()
                            continue

                        if msg.metadata.get("_progress"):
                            # progress/tool_hint：按配置决定是否展示（否则跳过）
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass  # 配置关闭了 tool hint，就不显示
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass  # 配置关闭了 progress，就不显示
                            else:
                                await _print_interactive_progress_line(msg.content, _thinking)
                            continue

                        if not turn_done.is_set():
                            # 首条“最终回复”到达：记录下来，解除等待
                            if msg.content:
                                # dict(msg.metadata or {})：复制一份 metadata，避免被后续修改影响
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()  # 标记本轮结束，让输入侧继续下一轮
                        elif msg.content:
                            # 回合已结束但仍有消息：作为异步通知直接打印（例如后台任务输出）
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        # 1 秒内没有新 outbound 消息：继续循环等待
                        continue
                    except asyncio.CancelledError:
                        # 任务被取消：退出循环，结束 outbound consumer
                        break

            outbound_task = asyncio.create_task(_consume_outbound())  # 后台：持续消费并渲染 outbound

            try:
                while True:
                    try:
                        _flush_pending_tty_input()  # 清理上轮输出期间用户误输入的残留按键
                        user_input = await _read_interactive_input_async()  # 异步读取一行用户输入
                        command = user_input.strip()  # 去掉两端空白，便于判断空输入/退出命令
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()  # 退出前恢复终端模式
                            console.print("\nGoodbye!")  # 打印告别语
                            break

                        turn_done.clear()  # 标记“新一轮开始”，输入侧将等待输出侧 set()
                        turn_response.clear()  # 清空上一轮的最终回复缓存
                        renderer = StreamRenderer(render_markdown=markdown)  # 为本轮准备一个新的流式渲染器

                        # 把用户输入封装成 InboundMessage 丢进 bus，由 agent_loop.run() 消费处理
                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                            metadata={"_wants_stream": True},  # 告诉 agent：我想要流式输出（delta）
                        ))

                        # 等待这一轮对话结束（由 outbound consumer 设置 turn_done）
                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                # 非流式模式：renderer 可能没用上，需要关闭它，避免占用资源
                                if renderer:
                                    await renderer.close()
                                _print_agent_response(
                                    content, render_markdown=markdown, metadata=meta,
                                )
                        elif renderer and not renderer.streamed:
                            # 没有 turn_response 且 renderer 未真正流式输出：关闭 renderer 避免泄露
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                # 退出时清理：停止 agent_loop、取消任务、关闭 MCP 连接
                agent_loop.stop()  # 让 agent_loop.run() 的 while self._running 退出
                outbound_task.cancel()  # 取消 outbound consumer（触发 CancelledError）
                # gather(..., return_exceptions=True)：等待任务结束；不要因为 CancelledError 让退出流程报错
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()  # 关闭 MCP 连接 + 等待后台任务收尾

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """
    列出所有发现到的 channels，并显示其在 config 中是否 enabled。
    """
    from nanobot.channels.registry import discover_all
    from nanobot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed.

    中文解释：WhatsApp bridge 是一个 Node 项目，需要 npm install/build。
    这里会把桥接代码复制到用户目录并构建，返回可运行目录。

    语法点：
    - `shutil.which("npm")`：查找外部命令
    - `subprocess.run(..., check=True)`：失败抛 CalledProcessError
    """
    import shutil
    import subprocess

    # User's bridge location
    from nanobot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code.

    中文解释：启动 WhatsApp bridge，让用户扫码登录。
    """
    import shutil
    import subprocess

    from nanobot.config.loader import load_config
    from nanobot.config.paths import get_runtime_subdir

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    wa_cfg = getattr(config.channels, "whatsapp", None) or {}
    bridge_token = wa_cfg.get("bridgeToken", "") if isinstance(wa_cfg, dict) else getattr(wa_cfg, "bridge_token", "")
    if bridge_token:
        env["BRIDGE_TOKEN"] = bridge_token
    env["AUTH_DIR"] = str(get_runtime_subdir("whatsapp-auth"))

    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js.[/red]")
        raise typer.Exit(1)

    try:
        subprocess.run([npm_path, "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins).

    中文解释：展示 channel 的发现结果（builtin/plugin）以及当前是否启用。
    """
    from nanobot.channels.registry import discover_all, discover_channel_names
    from nanobot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled", style="green")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """
    输出 config/workspace 是否存在、默认模型、以及各 provider 的 key 配置情况。
    """
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    """注册 OAuth 登录处理器（装饰器工厂）。

    中文解释：这是“装饰器返回装饰器”的典型写法：
    - `_register_login("openai_codex")` 返回 `decorator`
    - `@decorator` 装饰 `_login_openai_codex`，把函数注册到 `_LOGIN_HANDLERS`

    语法点：
    - 装饰器工厂（decorator factory）
    - 闭包捕获 `name`
    """
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider.

    中文解释：根据 provider 名（openai-codex / github-copilot）选择对应 handler 并执行登录流程。

    语法点：
    - `typer.Argument(...)`：必填位置参数
    - `provider.replace("-", "_")`：把 CLI 传入的 kebab-case 映射到 registry 的 snake_case
    """
    from nanobot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    """OpenAI Codex OAuth 登录（交互式）。

    中文解释：优先尝试读取已有 token；没有则启动交互式 OAuth device flow。
    """
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    """GitHub Copilot OAuth 登录（device flow 触发）。

    中文解释：通过一次 `litellm.acompletion` 触发 Copilot 的设备授权流程。

    语法点：
    - 在同步函数里 `asyncio.run(_trigger())` 执行异步协程
    """
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
