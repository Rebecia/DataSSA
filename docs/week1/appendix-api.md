# 附录：第 1 周认证 API

实施步骤 6 时对照本文。

| 方法 | 路径 | 鉴权 | 请求体 | 成功响应 |
|------|------|------|--------|----------|
| POST | `/api/v1/auth/register` | 无 | `username`, `password` | 201 + 用户信息 |
| POST | `/api/v1/auth/login` | 无 | `username`, `password` | 200 + `access_token`, `token_type` |
| GET | `/api/v1/auth/me` | Bearer | — | 200 + `username`, `role` |

**错误：**

| 场景 | 状态码 |
|------|--------|
| 未开启注册 | 403 |
| 用户名已存在 | 409 或 400 |
| 用户名或密码错误 | 401 |
| 无 token / token 无效 | 401 |

**JWT：** `sub` = `username`。
