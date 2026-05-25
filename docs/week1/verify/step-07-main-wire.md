# 步骤 7 验收手册

> 对应：[step-07-main-wire.md](../step-07-main-wire.md)
>
> 说明：只做 **静态检查**（不启动服务器），确认 `server/main.py` 已挂载 `/api/v1` 路由并启用 CORS。

---

## 1. 运行验证指令（无脚本）

在仓库根目录执行：

```bash
./.venv/bin/python - <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(".").resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server.main as main_mod

app = main_mod.app
paths = {getattr(route, "path", None) for route in app.routes}

required = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/me",
    "/health",
    "/chat",
    "/admin",
}
missing = sorted(p for p in required if p not in paths)
assert not missing, f"missing routes: {missing}"

middleware_classes = {mw.cls.__name__ for mw in app.user_middleware}
assert "CORSMiddleware" in middleware_classes, "CORS middleware not applied"

print("OK: main wired")
PY
```

**期望：**

- 输出包含 `OK: main wired`
