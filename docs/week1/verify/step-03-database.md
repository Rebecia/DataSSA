# 步骤 3 验收手册

> 对应：[step-03-database.md](../step-03-database.md)  
> 本步只验证 **Python 模型与 get_db**；**不要求** MySQL 里已有 `users` 表（步骤 4 用 Alembic 建表）。

---

## 1. 前置

- 步骤 2 已通过（能 `from server.config import get_settings`）
- 已激活虚拟环境：`source .venv/bin/activate`
- 已安装依赖：`pip install -r requirements.txt`（至少含 `sqlalchemy`、`pymysql`）

---

## 2. 导入与表结构（必做）

```bash
cd "/Users/edy/Desktop/工作/LLM/database-agent"
source .venv/bin/activate

python -c "
from server.db.models import User
from server.db import Base, get_db, engine
print('User table:', User.__tablename__)
print('columns:', [c.name for c in User.__table__.columns])
print('engine url host ok:', 'mysql' in str(engine.url) or '127.0.0.1' in str(engine.url))
"
```

**期望输出要点：**

| 项 | 期望 |
|----|------|
| `User table` | `users` |
| `columns` | 含 `id`, `username`, `password_hash`, `role`, `created_at` |
| 无异常 | 无 `ImportError` / `ModuleNotFoundError` |

---

## 3. get_db 生成器（可选）

```bash
python -c "
from server.db.session import get_db
gen = get_db()
db = next(gen)
print('session:', type(db).__name__)
gen.close()
"
```

**期望：** 打印 `session: Session`（无需连库成功；若 MySQL 未起可能下一步才报错，本步以 import 为主）。

---

## 4. 明确本步不做

```bash
# 以下在步骤 4 才做，本步不应要求成功：
# alembic upgrade head
# mysql 里 SHOW TABLES 出现 users
```

---

## 5. 验收打勾

```text
[x] python -c "from server.db.models import User" 无报错
[x] User.__tablename__ == "users"
[x] 列含 id / username / password_hash / role / created_at
[x] 已知：JWT sub 将使用 username（见 models.py 顶部注释）
```

全部通过后回复：**步骤 3 通过**，或 **做步骤 4**。

---

## 6. 常见问题

| 现象 | 处理 |
|------|------|
| `No module named 'pymysql'` | `pip install pymysql` 或重装 `requirements.txt` |
| `from server.db` 失败 | 确认在项目根目录执行，且已 `source .venv/bin/activate` |
| 连库报错但 import 成功 | 本步可算通过；步骤 4 前确保 `docker compose up -d mysql` |
