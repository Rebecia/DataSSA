# 步骤 11 验收手册（注册 + 守卫 + /me）

> 对应：[step-11-react-guard.md](../step-11-react-guard.md)

---

## 1. 未登录访问 /app → 跳回 /login

打开（无 token 的情况下）：

```text
http://localhost:5173/app
```

**期望：** 自动跳到 `/login`。

---

## 2. 登录后 /app 显示 /auth/me 信息

用有效账号登录后进入 `/app`。

**期望：**

- 页面显示当前 `username` 与 `role`

---

## 3. 清 token 后刷新 /app → 回到 /login

点击页面上的 “退出登录（清 token）”，或手动清除 localStorage 的 `datasaa_token`。

然后刷新：

```text
http://localhost:5173/app
```

**期望：** 回到 `/login`。

---

## 4. 401 自动登出（可选）

在后端终端执行：

```bash
export JWT_SECRET=wrong-secret
```

重启后端，再在前端刷新 `/app`。

**期望：** 后端返回 401 后，前端清 token 并跳到 `/login`。

