# 步骤 5：认证业务（哈希 + JWT）

## 本步目标

在 **Service 层** 实现：注册、校验密码、签发 token（尚未暴露 HTTP 路由）。

## 前置条件

- [步骤 4](./step-04-alembic.md) 已完成

## 你要做什么

1. 新建 `server/schemas/auth.py`：`RegisterIn`、`LoginIn`、`TokenOut`、`UserOut`
2. 新建 `server/services/auth_service.py`：
   - 注册：bcrypt 存 `password_hash`
   - 登录：校验密码
   - 签发 JWT：`sub` = username，`role` 写入 payload 或查库
3. 密码规则：最少 8 位（服务端校验）

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `server/schemas/auth.py` |
| 新建 | `server/services/auth_service.py` |

## 本步完成标志

- [ ] 能对测试用户调用 service 层注册/登录（单元测试或临时脚本均可）
- [ ] **尚未要求** 有 HTTP 接口（步骤 6）

## 下一步

[步骤 6：Auth API + deps](./step-06-auth-api.md)
