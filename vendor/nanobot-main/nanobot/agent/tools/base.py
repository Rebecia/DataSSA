"""Base class for agent tools."""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    Abstract base class for agent tools.

    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.

    中文速读（你后面写 RAG Tool 会用到）：
    - 一个 Tool 最重要的 4 件事：`name / description / parameters(JSON Schema) / execute()`
    - `parameters` 会被 `ToolRegistry.get_definitions()` 汇总给 LLM：告诉它“这个工具怎么用”
    - 当 LLM 产出 tool_calls 后，`ToolRegistry.execute()` 会把参数交给这里的
      `cast_params()` + `validate_params()` 做“纠偏 + 校验”，最后才 `await execute()`
    """

    _TYPE_MAP = {
        # JSON Schema 的基础类型 -> Python 类型（用于 cast/validate）
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        """
        中文解释：
        - JSON Schema 允许类型是列表（联合类型），比如 ["string","null"]
        - 这里的策略是：优先取第一个非 null 的主类型，方便后面的 cast/validate
        """
        if isinstance(t, list):
            for item in t:
                if item != "null":
                    return item
            return None
        return t

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        # 例如：ExecTool.name == "exec"，WebSearchTool.name == "web_search"
        # LLM 的 tool_call 里会直接用这个名字来指定调用哪个工具
        raise NotImplementedError

# @property：把一个方法变成“像字段一样访问”的属性：访问时写 tool.name，而不是 tool.name()
# @abstractmethod：声明这是抽象方法/抽象属性
# 只在基类里“规定接口”，不提供可用实现
# 任何继承 Tool 的子类都必须实现 name/description/parameters（以及 execute），否则 Python 会报错：Can't instantiate abstract class ...
    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        # 给 LLM/用户看的说明，越具体越好（否则模型容易误用）
        raise NotImplementedError

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        # 这是 OpenAI tools/function calling 的参数 schema（JSON Schema 子集）
        # 用来约束模型生成参数的结构、类型、必填字段等
        raise NotImplementedError

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            Result of the tool execution (string or list of content blocks).

        中文解释：
        - 返回值会被 AgentLoop 包装为 role=tool 的消息，回填到 LLM 上下文
        - 一般返回 str 就够；如果要多模态/富文本，也可以返回 content blocks(list[dict])
        """
        raise NotImplementedError

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply safe schema-driven casts before validation."""
        # 中文解释：LLM 有时会把数字/布尔值以字符串形式传来（例如 "60"）
        # 这里在“验证前”做一次温和的类型转换，提高工具调用成功率
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params

        return self._cast_object(params, schema)

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        """Cast an object (dict) according to schema."""
        if not isinstance(obj, dict):
            return obj

        props = schema.get("properties", {})
        result = {}

        for key, value in obj.items():
            if key in props:
                result[key] = self._cast_value(value, props[key])
            else:
                # schema 里没声明的字段：原样透传（不强行丢弃，避免误伤）
                result[key] = value

        return result

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        """Cast a single value according to schema."""
        target_type = self._resolve_type(schema.get("type"))

        # 下面几段是“如果本来就是正确类型，就不动”，避免把 bool 当成 int 等边界问题
        if target_type == "boolean" and isinstance(val, bool):
            return val
        if target_type == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if target_type in self._TYPE_MAP and target_type not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[target_type]
            if isinstance(val, expected):
                return val

        if target_type == "integer" and isinstance(val, str):
            # "123" -> 123；转不了就保持原值，让 validate 报错给模型纠正
            try:
                return int(val)
            except ValueError:
                return val

        if target_type == "number" and isinstance(val, str):
            # "3.14" -> 3.14
            try:
                return float(val)
            except ValueError:
                return val

        if target_type == "string":
            # 统一转成 str（None 保持 None）
            return val if val is None else str(val)

        if target_type == "boolean" and isinstance(val, str):
            # 常见布尔字符串映射
            val_lower = val.lower()
            if val_lower in ("true", "1", "yes"):
                return True
            if val_lower in ("false", "0", "no"):
                return False
            return val

        if target_type == "array" and isinstance(val, list):
            item_schema = schema.get("items")
            # 如果 items 有 schema，就对每个元素递归 cast
            return [self._cast_value(item, item_schema) for item in val] if item_schema else val

        if target_type == "object" and isinstance(val, dict):
            # 对嵌套 object 递归 cast
            return self._cast_object(val, schema)

        # 不认识/不需要 cast 的情况：原样返回
        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        # 从根开始递归校验（path 用于生成更友好的报错位置）
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        raw_type = schema.get("type")
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get(
            "nullable", False
        )
        t, label = self._resolve_type(raw_type), path or "parameter"
        if nullable and val is None:
            return []

        # 先做类型校验：类型不对就直接返回错误（不要继续深挖）
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (
            not isinstance(val, self._TYPE_MAP[t]) or isinstance(val, bool)
        ):
            return [f"{label} should be number"]
        if t in self._TYPE_MAP and t not in ("integer", "number") and not isinstance(val, self._TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        if t == "object":
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {path + '.' + k if path else k}")
            for k, v in val.items():
                if k in props:
                    errors.extend(self._validate(v, props[k], path + "." + k if path else k))
        if t == "array" and "items" in schema:
            for i, item in enumerate(val):
                errors.extend(
                    self._validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]")
                )
        return errors

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        # 把 Tool 转成 OpenAI tools/function calling 需要的 schema 结构
        # {"type":"function","function":{"name":...,"description":...,"parameters":...}}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
