# 步骤 10 验收手册（登录页 + token）

> 对应：[step-10-react-login.md](../step-10-react-login.md)

---

## 1. 启动后端

```bash
export ALLOW_REGISTER=1
./bin/database-agent api --host 127.0.0.1 --port 18790
```

---

## 2. 启动前端

另开一个终端：

```bash
cd web
npm install
npm run dev
```

打开 `http://localhost:5173`。

---

## 3. 验收（页面操作）

1. 先用 curl 注册一个账号（避免你不知道库里有哪些用户）：

```bash
curl -s -i -X POST http://127.0.0.1:18790/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"test1","password":"password12"}'
```

2. 在网页 `/login` 用 `test1/password12` 登录。

**期望：**

- 登录成功后跳到 `/app`
- DevTools → Application → Local Storage 里存在 `datasaa_token`

