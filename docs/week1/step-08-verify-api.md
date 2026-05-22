# 步骤 8：后端 API 验收（_checkpoint）

## 本步目标

**只测后端**，确认认证链路正确；**通过后才能做 React**。

## 前置条件

- [步骤 7](./step-07-main-wire.md) 已完成

## 你要做什么

按顺序执行下面命令（用户名密码按你注册的为准）：

```bash
export ALLOW_REGISTER=1

# 1. 注册
curl -s -X POST http://127.0.0.1:18790/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"test1","password":"password12"}'

# 2. 登录
TOKEN=$(curl -s -X POST http://127.0.0.1:18790/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"test1","password":"password12"}' | jq -r .access_token)

# 3. me（有 token）
curl -s http://127.0.0.1:18790/api/v1/auth/me \
  -H "Authorization: Bearer $TOKEN"

# 4. me（无 token）→ 必须是 401
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18790/api/v1/auth/me

# 5. 旧 chat 回归（可选）
# DATASSA_TEST_MODE=1 下 POST /chat 仍能 200
```

## 本步完成标志

- [ ] 注册成功
- [ ] 登录返回 `access_token`
- [ ] `/auth/me` 带 token 返回 username、role
- [ ] `/auth/me` 无 token 为 **401**
- [ ] `ALLOW_REGISTER=0` 时 register 为 **403**

## 未通过时

**不要进入步骤 9**；回到步骤 5–7 排查。

## 下一步

[步骤 9：初始化 React](./step-09-react-init.md)
