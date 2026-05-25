# 步骤 2 验收手册

> 对应：[step-02-config.md](../step-02-config.md)

---

## 1. 安装 Python 依赖

```bash
cd "/Users/edy/Desktop/工作/LLM/database-agent"

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. 加载配置（本地）

```bash
cp .env.example .env
# 可按需改 .env，默认即可用于本地开发

python -c "from server.config import get_settings; s=get_settings(); print(s.database_url); print(s.redis_url); print(s.allow_register)"
```

**期望：**

- 无 `ImportError`
- 打印出 `mysql+pymysql://...` 与 `redis://...`
- `allow_register` 与 `.env` 中 `ALLOW_REGISTER` 一致

---

## 3. 核对 compose 中 api 环境变量（Docker 装好以后）

```bash
docker compose config | grep -E 'DATABASE_URL|REDIS_URL|JWT_SECRET|ALLOW_REGISTER'
```

**期望（容器内主机名）：**

- `DATABASE_URL` 含 `@mysql:3306`
- `REDIS_URL` 含 `redis:6379`

---

## 4. 验收打勾

```text
[x] pip install 成功
[x] python -c "from server.config import get_settings" 成功
[x] docker compose config 能看到 api 的 DATABASE_URL / REDIS_URL（Docker 可用时）
```

通过后回复：**步骤 2 通过**，或继续 **步骤 3**。
