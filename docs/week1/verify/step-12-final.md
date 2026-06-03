# 步骤 12 验收手册（本周总验收）

> 对应：[step-12-final.md](../step-12-final.md)

---

## 1. 基础服务

```bash
docker compose ps
docker compose exec redis redis-cli ping
docker compose exec mysql mysqladmin ping -h 127.0.0.1 -uroot -pdatasaa_root
```

**期望：** mysql/redis 健康，redis 返回 `PONG`，mysql 返回 `mysqld is alive`。

---

## 2. Alembic / users 表

```bash
alembic current
docker compose exec mysql mysql -udatasaa -pdatasaa datasaa -e "SHOW TABLES LIKE 'users';"
```

**期望：** 有 `users` 表。

---

## 3. 后端认证链路（HTTP）

确保后端运行在 `127.0.0.1:18790`，并开启注册：

```bash
export ALLOW_REGISTER=1
curl -s -i -X POST http://127.0.0.1:18790/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"final1","password":"password12"}'

TOKEN=$(curl -s -X POST http://127.0.0.1:18790/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"final1","password":"password12"}' | jq -r .access_token)

curl -s -i http://127.0.0.1:18790/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18790/api/v1/auth/me
```

**期望：**

- register 为 201
- login 返回 `access_token`
- me 有 token 为 200
- me 无 token 为 401

---

## 4. 前端链路（手工）

```text
登录 → /app 显示用户信息 → 退出登录（清 token）→ 刷新 /app 回到 /login
```

---

## 5. 旧 /chat 回归（可选）

```bash
export DATASSA_TEST_MODE=1
curl -s -X POST http://127.0.0.1:18790/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"api:smoke","message":"users 表有多少用户？"}' | jq .
```

