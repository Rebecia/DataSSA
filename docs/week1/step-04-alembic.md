# 步骤 4：Alembic 建 users 表

## 本步目标

MySQL 里真实存在 `users` 表。

## 前置条件

- [步骤 3](./step-03-database.md) 已完成
- MySQL 容器已启动

## 你要做什么

1. 初始化 `alembic/`（`alembic init` 或等价结构）
2. 配置 `alembic/env.py` 使用 `server.db.models` 的 metadata
3. 生成并执行首版迁移：`users` 表
4. （可选）种子脚本：创建一个 `role=admin` 测试账号

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `alembic.ini`、`alembic/env.py`、`alembic/versions/*.py` |

## 本步完成标志

```bash
docker compose up -d mysql
alembic upgrade head
# 进入 mysql 客户端
# SHOW TABLES;  → 能看到 users
```

- [ ] `alembic upgrade head` 成功
- [ ] `users` 表存在

## 下一步

[步骤 5：认证业务层](./step-05-auth-service.md)
