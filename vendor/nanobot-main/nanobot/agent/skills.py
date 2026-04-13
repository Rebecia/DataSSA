"""Skills loader for agent capabilities.

中文速读：
- Skill 在 nanobot 里本质是“一份可读的工作流/说明书”（`SKILL.md`），让模型按固定步骤做事。
- SkillsLoader 负责三件事：
  1) 扫描 workspace 自定义 skills（优先级最高）：`{workspace}/skills/<name>/SKILL.md`
  2) 扫描内置 skills（fallback）：`nanobot/skills/<name>/SKILL.md`
  3) 把 skills 以“摘要清单”形式注入 system prompt（summary），需要时再让模型用 read_file 读取全文
- requirements：
  - skill 的 frontmatter 可以声明依赖（bins/env），不满足则标记 available=false（避免模型盲用）。
"""

"""
“主要函数”就这 5 个：
- `list_skills`：扫描 workspace/builtin 的 `SKILL.md`，返回技能清单（可按 requirements 过滤不可用）
- `load_skill(name)`：按名称读取某个 skill 的 `SKILL.md`（workspace 优先，其次 builtin）
- `build_skills_summary()`：生成注入到 system prompt 的 `<skills>...</skills>` 摘要清单（含 available/requires/location）
- `get_always_skills()`：找出标记了 `always=true` 且依赖满足的 skills（用于“全文注入 Active Skills”）
- `load_skills_for_context(skill_names)`：把指定 skills 的正文拼成可直接注入 prompt 的文本
"""


import json
import os
import re
import shutil
from pathlib import Path

# Default builtin skills directory (relative to this file)
# 中文解释：内置 skills 跟着代码发布；workspace skills 则是用户项目内自定义/覆盖的能力
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        # 中文解释（核心函数 1）：
        # - 返回一个 skills 清单，每个元素是 {"name","path","source"}：
        #   - source="workspace"：来自 {workspace}/skills（最高优先级，可覆盖同名 builtin）
        #   - source="builtin"：来自 nanobot 自带 skills 目录
        # - filter_unavailable=True 时会检查 requirements（bins/env），不满足依赖的 skill 会被过滤掉
        skills = []

        # Workspace skills（最高优先级，允许你在项目里覆盖同名 builtin skill）
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # Built-in skills（内置 skills：只有当 workspace 没有同名 skill 时才加入）
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements：不满足 bins/env 的 skill 会被过滤掉（默认行为）
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # 中文解释（核心函数 2）：
        # - 按技能名读取 SKILL.md 的全文
        # - 查找顺序：workspace 优先，其次 builtin（同名覆盖）
        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        # 中文解释（核心函数 3：用于“全文注入”）：
        # - 把指定 skills 的正文拼到 system prompt 的“Active Skills”里
        # - 会剥离 frontmatter（YAML）避免把元数据当正文注入
        # - 返回的字符串格式包含分隔线，便于模型阅读
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        # 中文解释（核心函数 4：用于“摘要注入”）：
        # - 生成一个 <skills>...</skills> 的 XML 清单，注入到 system prompt
        # - 清单会包含：
        #   - name/description/location
        #   - available=true|false（requirements 是否满足）
        #   - requires（缺的依赖提示：缺哪个 CLI 或 env）
        # - 目的：渐进式加载，默认不把所有 SKILL.md 全文塞进 prompt，省 token
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        # SKILL.md 常会在最上面放 YAML frontmatter，这里从正文里剥离掉（避免注入到 prompt 的正文污染）
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports nanobot and openclaw keys)."""
        # 中文解释：这里不是解析 YAML，而是假设 frontmatter 里某个字段是 JSON 字符串：
        # - 支持 key=nanobot 或 key=openclaw（兼容旧命名）
        try:
            data = json.loads(raw)
            return data.get("nanobot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        # requires:
        # - bins：系统里必须能找到的命令（shutil.which）
        # - env：必须存在的环境变量
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        # 中文解释（核心函数 5）：
        # - 找出“永远启用”的技能列表
        # - always 标记来源有两种：
        #   1) SKILL.md frontmatter 里的 always 字段（简单 YAML）
        #   2) frontmatter 里 metadata(JSON) 的 nanobot.always/openclaw.always
        # - 同时要求 requirements 满足（list_skills(filter_unavailable=True) 已过滤）
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None
