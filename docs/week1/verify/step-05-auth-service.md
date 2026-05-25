# 步骤 5 验收手册

> 对应：[step-05-auth-service.md](../step-05-auth-service.md)
>
> 说明：本步验收以 **进程内 SQLite 内存库** 跑通 service 逻辑（不依赖 MySQL/Redis/容器网络）。

---

## 1. 运行验证脚本

在仓库根目录执行：

```bash
./.venv/bin/python scripts/verify_step05_auth_service.py
```

**期望：**

- 输出包含 `OK: register/login/jwt`

---

## 2. 验收打勾

```text
[ ] AuthService.register 能创建用户（bcrypt hash）
[ ] AuthService.login 能返回 bearer token
[ ] JWT 可解码，payload 含 sub/role/exp
```

通过后回复：**步骤 5 通过**，继续 **步骤 6**。

