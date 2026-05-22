# 步骤 11：注册页 + 占位页 + 路由守卫

## 本步目标

未登录不能进 `/app`；`/app` 显示 `/auth/me` 用户信息；可选注册页。

## 前置条件

- [步骤 10](./step-10-react-login.md) 已完成

## 你要做什么

1. 新建 `web/src/pages/Register.tsx` → `POST /api/v1/auth/register`
2. 新建 `web/src/pages/AppPlaceholder.tsx` → `GET /api/v1/auth/me` 展示 username、role
3. 路由守卫：无 token 访问 `/app` → 重定向 `/login`
4. axios 响应 401：清 token，跳转 `/login`

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `web/src/pages/Register.tsx` |
| 新建 | `web/src/pages/AppPlaceholder.tsx` |
| 修改 | `web/src/App.tsx` |

## 本步完成标志

- [ ] 未登录打开 `/app` 会跳到 `/login`
- [ ] 登录后 `/app` 显示当前用户名、角色
- [ ] 清除 token 后刷新 `/app` 回到 `/login`

## 下一步

[步骤 12：本周总验收](./step-12-final.md)
