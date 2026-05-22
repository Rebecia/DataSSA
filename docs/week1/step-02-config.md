# 步骤 2：依赖、环境变量、Settings

## 本步目标

项目能读取统一配置；依赖装齐；提供 `.env.example`。

## 前置条件

- [步骤 1](./step-01-docker.md) 已完成

## 你要做什么

1. 更新 `requirements.txt`，增加：
   - `sqlalchemy`、`alembic`、`pymysql`
   - `redis`
   - `python-jose[cryptography]` 或 `PyJWT`
   - `passlib[bcrypt]`
   - `pydantic-settings`
2. 新建 `server/config.py`（`Settings` 读环境变量）
3. 新建 `.env.example`（见 [appendix-env.md](./appendix-env.md)）
4. 在 `docker-compose.yml` 的 `api` 服务里注入：
   - `DATABASE_URL`（指向 compose 内 `mysql`）
   - `REDIS_URL`（指向 compose 内 `redis`）
   - `JWT_SECRET`、`ALLOW_REGISTER` 等

## 涉及文件

| 操作 | 路径 |
|------|------|
| 修改 | `requirements.txt` |
| 新建 | `server/config.py` |
| 新建 | `.env.example` |
| 修改 | `docker-compose.yml` |

## 本步完成标志

```bash
pip install -r requirements.txt
python -c "from server.config import get_settings; print(get_settings().database_url)"
```

**验收手册：** [verify/step-02-config.md](./verify/step-02-config.md)

- [x] 无 import 错误（`server/config.py` 已添加；本地 `pip install` 后执行验收命令）
- [x] `DATABASE_URL`、`REDIS_URL` 已写入 compose 的 `api` 环境（本地 `docker compose config` 核对）

## 下一步

[步骤 3：数据库模型](./step-03-database.md)
