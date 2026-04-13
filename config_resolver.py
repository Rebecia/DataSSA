from __future__ import annotations

"""
config_resolver.py

把“可公开提交到 GitHub 的 config.json（不含密钥）”在运行时解析为“可用配置”：
- 从项目根目录读取 `.env`（若存在）
- 将形如 `DEEPSEEK_API_KEY` / `${DEEPSEEK_API_KEY}` 的占位符替换为真实环境变量值
- 生成一个“resolved config”文件（放在 workspace/memory 下），避免修改原始 config.json

注意：
- nanobot 的 provider apiKey 不是 env placeholder 语义，因此需要在这里做替换。
- 生成的 resolved config 不应提交到 git。
"""

import json
import os
import re
from pathlib import Path
from typing import Any


_ENV_VAR_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def load_dotenv(dotenv_path: Path) -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE），不依赖第三方库。"""
    if not dotenv_path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        out[k] = v
    return out


def apply_env_defaults(env: dict[str, str]) -> None:
    """把 .env 中的变量写入 os.environ（仅当当前未设置时）。"""
    for k, v in env.items():
        os.environ.setdefault(k, v)


def _resolve_api_key(value: Any) -> str:
    """
    支持两种写法：
    - "DEEPSEEK_API_KEY"（纯变量名）
    - "${DEEPSEEK_API_KEY}"
    """
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    m = _ENV_VAR_RE.match(s)
    if m:
        return os.getenv(m.group(1), "")
    if s.isupper() and re.fullmatch(r"[A-Z0-9_]+", s):
        return os.getenv(s, "")
    return s


def resolve_config(config_path: Path, *, project_root: Path) -> Path:
    """
    生成一个 resolved 配置文件并返回其路径。

    - 保持 config.json 本身不变（便于公开仓库）
    - resolved 文件放在 `<workspace>/memory/config.resolved.json`，保证相对路径仍以项目为基准
    """
    env = load_dotenv(project_root / ".env")
    apply_env_defaults(env)

    raw = json.loads(config_path.read_text(encoding="utf-8"))

    providers = raw.get("providers") or {}
    for provider_name, cfg in list(providers.items()):
        if not isinstance(cfg, dict):
            continue
        if "apiKey" in cfg:
            resolved = _resolve_api_key(cfg.get("apiKey"))
            if resolved:
                cfg["apiKey"] = resolved

    # Determine workspace (relative to config.json location)
    workspace = (
        (raw.get("agents") or {})
        .get("defaults", {})
        .get("workspace", "./workspace")
    )
    workspace_path = (config_path.parent / workspace).resolve()
    resolved_path = (workspace_path / "memory" / "config.resolved.json").resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return resolved_path

