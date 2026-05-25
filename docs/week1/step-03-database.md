# 步骤 3：SQLAlchemy 与 users 模型

## 本步目标

能用代码描述 `users` 表结构；提供 `get_db` 依赖。

## 前置条件

- [步骤 2](./step-02-config.md) 已完成

## 你要做什么

1. 新建 `server/db/session.py`：`engine`、`SessionLocal`、`get_db`
2. 新建 `server/db/models.py`：`User` 模型  
   字段：`id`、`username`（唯一）、`password_hash`、`role`（`user`/`admin`）、`created_at`
3. 约定：**JWT 的 `sub` = `username`**（第 2 周 chat 会用到）

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `server/db/session.py` |
| 新建 | `server/db/models.py` |
| 新建 | `server/db/__init__.py`（如需要） |

## 本步完成标志

**验收手册：** [verify/step-03-database.md](./verify/step-03-database.md)

- [x] 本地能 `from server.db.models import User` 无报错（见 `server/db/models.py`）
- [x] **尚未要求** MySQL 里已有表（步骤 4 迁移）

## 下一步

[步骤 4：Alembic 迁移](./step-04-alembic.md)
