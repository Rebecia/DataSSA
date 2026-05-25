# 步骤 4 验收手册

> 对应：[step-04-alembic.md](../step-04-alembic.md)  
> 本步在 MySQL 中**真实创建** `users` 表。

---

## 1. 前置

```bash
cd "/Users/edy/Desktop/工作/LLM/database-agent"
source .venv/bin/activate

# 依赖齐全（含 alembic、pymysql、passlib）
pip install -r requirements.txt

# MySQL 已启动
docker compose up -d mysql
docker compose ps   # mysql 为 Up / healthy
```

确认 `.env` 中 `DATABASE_URL` 指向本机映射端口（默认即可）：

```text
DATABASE_URL=mysql+pymysql://datasaa:datasaa@127.0.0.1:3306/datasaa
```

---

## 2. 执行迁移（必做）

```bash
alembic upgrade head
```

**期望：**

- 命令退出码 `0`
- 日志出现 `Running upgrade  -> 20260525_0001` 或类似成功信息
- **无** `Can't connect to MySQL`、`Access denied`

---

## 3. 检查表结构（必做）

```bash
docker compose exec mysql mysql -udatasaa -pdatasaa datasaa -e "SHOW TABLES;"
docker compose exec mysql mysql -udatasaa -pdatasaa datasaa -e "DESCRIBE users;"
```

**期望：**

| 检查 | 期望 |
|------|------|
| `SHOW TABLES` | 列表含 `users` |
| `DESCRIBE users` | 含 `id`, `username`, `password_hash`, `role`, `created_at` |

---

## 4. 可选：种子管理员

```bash
python -m server.db.seed
```

默认创建：`admin` / `admin12345`（仅开发环境；步骤 5 仍可用 register 注册普通用户）。

自定义：

```bash
SEED_ADMIN_USER=myadmin SEED_ADMIN_PASSWORD='secret1234' python -m server.db.seed
```

验证：

```bash
docker compose exec mysql mysql -udatasaa -pdatasaa datasaa \
  -e "SELECT id, username, role FROM users;"
```

---

## 5. 回滚（仅排查时用）

```bash
alembic downgrade base
# 会删除 users 表；需要时再 upgrade head
```

---

## 6. 验收打勾

```text
[ ] alembic upgrade head 成功
[ ] SHOW TABLES 能看到 users
[ ] DESCRIBE users 字段正确
[ ] （可选）seed 后 users 表有 admin 行
```

通过后回复：**步骤 4 通过**，或 **做步骤 5**。

---

## 7. 常见问题

| 现象 | 处理 |
|------|------|
| `Can't connect to MySQL server on '127.0.0.1'` | `docker compose up -d mysql`，等 healthy |
| `Unknown database 'datasaa'` | 重启 mysql 容器让 `MYSQL_DATABASE` 生效，或手动 `CREATE DATABASE datasaa` |
| `No module named 'alembic'` | `pip install alembic` |
| `Target database is not up to date` | `alembic current` / `alembic history` 查看；慎用 `alembic stamp head` |
