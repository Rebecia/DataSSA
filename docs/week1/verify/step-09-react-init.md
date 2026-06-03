# 步骤 9 验收手册（React 初始化）

> 对应：[step-09-react-init.md](../step-09-react-init.md)

---

## 1. 安装依赖并启动

在仓库根目录执行：

```bash
cd web
npm install
npm run dev
```

**期望：**

- 终端提示类似 `Local: http://localhost:5173/`
- 浏览器打开 `http://localhost:5173` 能看到页面（登录占位页）
- 点击按钮能跳转到 `/app` 占位页

---

## 2. 验证 proxy（可选）

后端启动在 `127.0.0.1:18790` 后，打开：

```text
http://localhost:5173/api/v1/auth/me
```

**期望：** 返回 401（因为没带 token），说明 `vite` 的 `/api` 代理生效。

