# 步骤 7：main.py 挂载路由 + CORS

## 本步目标

启动 API 服务后，auth 路由可访问；**旧功能不被破坏**。

## 前置条件

- [步骤 6](./step-06-auth-api.md) 已完成

## 你要做什么

1. 新建 `server/middleware.py`：CORS（允许 Vite 开发源 `http://localhost:5173`）
2. 修改 `server/main.py`：
   - `include_router` 挂载 `/api/v1`
   - **保留** 现有 `lifespan`、`/chat`、`/admin/*`、静态页路由
   - 可把巨型函数逐步抽到 `server/api/legacy.py`（可选，不强制一次抽完）

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `server/middleware.py` |
| 修改 | `server/main.py` |

## 本步完成标志

```bash
./bin/database-agent api --host 127.0.0.1 --port 18790
curl http://127.0.0.1:18790/health
```

- [ ] 服务能启动，无 import 报错
- [ ] `/health` 仍返回 ok

## 下一步

[步骤 8：API 验收](./step-08-verify-api.md)（必做，通过后再做前端）
