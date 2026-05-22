# 步骤 10：登录页 + axios + token

## 本步目标

浏览器里能登录，token 写入 `localStorage`，并跳转到 `/app`。

## 前置条件

- [步骤 9](./step-09-react-init.md) 已完成
- 后端 [步骤 8](./step-08-verify-api.md) 已通过

## 你要做什么

1. 新建 `web/src/api/client.ts`：
   - 请求自动带 `Authorization: Bearer <token>`
2. 新建 `web/src/pages/Login.tsx`：
   - 表单 → `POST /api/v1/auth/login`
   - 成功：存 token，跳转 `/app`

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `web/src/api/client.ts` |
| 新建 | `web/src/pages/Login.tsx` |
| 修改 | `web/src/App.tsx` |

## 本步完成标志

- [ ] 用 test1 账号能在页面上登录
- [ ] DevTools → Application → localStorage 能看到 token
- [ ] 登录后 URL 变为 `/app`（占位页可先空白）

## 下一步

[步骤 11：注册 + 守卫 + 占位页](./step-11-react-guard.md)
