# 步骤 6：Auth 路由与鉴权依赖

## 本步目标

暴露 3 个 HTTP 接口；`get_current_user` 能从 Bearer token 解析用户。

## 前置条件

- [步骤 5](./step-05-auth-service.md) 已完成

## 你要做什么

1. 新建 `server/deps.py`：`get_db`、`get_current_user`（失败 → 401）
2. 新建 `server/api/v1/auth.py`：
   - `POST /auth/register`（`ALLOW_REGISTER!=1` → 403）
   - `POST /auth/login`
   - `GET /auth/me`（需登录）
3. 新建 `server/api/v1/router.py`，`prefix=/api/v1`

接口细节见 [appendix-api.md](./appendix-api.md)。

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `server/deps.py` |
| 新建 | `server/api/v1/auth.py` |
| 新建 | `server/api/v1/router.py` |

## 本步完成标志

- [ ] 路由代码写完
- [ ] **尚未要求** `main.py` 已挂载（步骤 7）；可用 `TestClient` 临时挂载测

## 下一步

[步骤 7：main 装配](./step-07-main-wire.md)
