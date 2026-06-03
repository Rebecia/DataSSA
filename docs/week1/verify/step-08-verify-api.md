# 步骤 8 验收手册（后端 API checkpoint）

> 对应：[step-08-verify-api.md](../step-08-verify-api.md)
>
> 说明：本步只验收后端；通过后才能做 React（步骤 9）。

---

## 0. 前置

- MySQL/Redis 已启动（步骤 1）
- Alembic 已 upgrade（步骤 4）

---

## 1. 启动 API

在仓库根目录执行：

```bash
./bin/database-agent api --host 127.0.0.1 --port 18790
```

另开一个终端窗口继续下面的 curl。

---

## 2. 健康检查

```bash
curl -s http://127.0.0.1:18790/health
```

**期望：** 返回 `{"ok": true}`（或包含 ok=true）。

---

## 3. 注册 / 登录 / me

```bash
export ALLOW_REGISTER=1

# 1) 注册（201）
curl -s -i -X POST http://127.0.0.1:18790/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"test1","password":"password12"}'

# 2) 登录（200，返回 access_token）
TOKEN=$(curl -s -X POST http://127.0.0.1:18790/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"test1","password":"password12"}' | jq -r .access_token)
echo "$TOKEN" | head -c 20 && echo

# 3) me（200）
curl -s -i http://127.0.0.1:18790/api/v1/auth/me \
  -H "Authorization: Bearer $TOKEN"

# 4) me（无 token -> 401）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18790/api/v1/auth/me
```

---

## 4. 关闭注册（403）

```bash
export ALLOW_REGISTER=0
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:18790/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"test2","password":"password12"}'
```

**期望：** `403`

---

## 5. 旧 chat 回归（可选）

```bash
export DATASSA_TEST_MODE=1
curl -s -X POST http://127.0.0.1:18790/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"api:smoke","message":"users 表有多少用户？"}' | jq .
```

**期望：** `trace_id` 存在，且返回 `answer`（test-mode 下可稳定跑通）。

