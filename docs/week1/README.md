# 第 1 周 — 执行索引

> 总计划：[ROADMAP_6W.md](../ROADMAP_6W.md)  
> **用法：严格按步骤顺序做，做完一步验收一步，再开下一步。**

---

## 本周一句话

起 **MySQL + Redis + API**，做完 **JWT 登录**，React 能登录进占位页；**无 token → 401**。旧 `/chat` 仍能跑。

---

## 步骤列表（按顺序）

| 步骤 | 文档 | 做什么 |
|------|------|--------|
| 1 | [step-01-docker.md](./step-01-docker.md) | docker-compose 增加 MySQL、Redis |
| 2 | [step-02-config.md](./step-02-config.md) | 依赖 + `.env.example` + `Settings` |
| 3 | [step-03-database.md](./step-03-database.md) | SQLAlchemy + `users` 模型 |
| 4 | [step-04-alembic.md](./step-04-alembic.md) | Alembic 迁移建表 |
| 5 | [step-05-auth-service.md](./step-05-auth-service.md) | 注册/登录业务 + 密码哈希 + JWT |
| 6 | [step-06-auth-api.md](./step-06-auth-api.md) | auth 路由 + `deps` 鉴权 |
| 7 | [step-07-main-wire.md](./step-07-main-wire.md) | `main.py` 挂载 v1 + CORS + 保留旧路由 |
| 8 | [step-08-verify-api.md](./step-08-verify-api.md) | **停步验收**：curl 测 login/me/401 |
| 9 | [step-09-react-init.md](./step-09-react-init.md) | 初始化 `web/` 工程 |
| 10 | [step-10-react-login.md](./step-10-react-login.md) | 登录页 + token + axios |
| 11 | [step-11-react-guard.md](./step-11-react-guard.md) | 注册页 + `/app` 占位 + 路由守卫 |
| 12 | [step-12-final.md](./step-12-final.md) | **本周总验收** + 学习笔记 |

---

## 本周不做

见 [step-00-scope.md](./step-00-scope.md)。

---

## 给 AI 时怎么说

一次只做一步，例如：

```text
按 docs/week1/step-03-database.md 执行，只做这一步，做完给出验收结果。
```

---

## 验收手册（新设备 / Docker 后补测）

| 步骤 | 文档 |
|------|------|
| 1 | [verify/step-01-docker.md](./verify/step-01-docker.md) |
| 2 | [verify/step-02-config.md](./verify/step-02-config.md) |
| 3 | [verify/step-03-database.md](./verify/step-03-database.md) |

## 附录

| 文件 | 内容 |
|------|------|
| [appendix-api.md](./appendix-api.md) | 认证 API 请求/响应约定 |
| [appendix-env.md](./appendix-env.md) | 环境变量说明 |
| [../learning/week1.md](../learning/week1.md) | 学习笔记（步骤 12 填写） |
