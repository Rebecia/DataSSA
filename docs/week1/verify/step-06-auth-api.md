# 步骤 6 验收手册

> 对应：[step-06-auth-api.md](../step-06-auth-api.md)
>
> 说明：用 `TestClient` 在 **SQLite 内存库** 下验证 HTTP 路由与鉴权依赖（不依赖 MySQL/Redis/容器网络）。

---

## 1. 运行验证指令（无脚本）

在仓库根目录执行（会直接退出，成功会打印 `OK: auth api`）：

```bash
./.venv/bin/python - <<'PY'
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(".").resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["JWT_SECRET"] = "dev-secret"
os.environ["JWT_ALGORITHM"] = "HS256"
os.environ["JWT_EXPIRE_MINUTES"] = "5"
os.environ["ALLOW_REGISTER"] = "1"

from server.api.v1.router import router as api_v1_router  # noqa: E402
from server.config import get_settings  # noqa: E402
from server.db.models import Base  # noqa: E402
from server.deps import get_db  # noqa: E402

settings = get_settings()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

app = FastAPI()
app.include_router(api_v1_router)

def _override_get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = _override_get_db

client = TestClient(app)
username = f"u_{int(time.time())}"
password = "password12"

r = client.post("/api/v1/auth/register", json={"username": username, "password": password})
assert r.status_code == 201, r.text

r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
assert r.status_code == 200, r.text
token = r.json()["access_token"]

r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
assert r.status_code == 200, r.text
assert r.json()["username"] == username

r = client.get("/api/v1/auth/me")
assert r.status_code == 401, r.text

os.environ["ALLOW_REGISTER"] = "0"
get_settings.cache_clear()

app2 = FastAPI()
app2.include_router(api_v1_router)
app2.dependency_overrides[get_db] = _override_get_db
client2 = TestClient(app2)
r = client2.post("/api/v1/auth/register", json={"username": "x", "password": "password12"})
assert r.status_code == 403, r.text

print("OK: auth api")
PY
```

**期望：**

- 输出包含 `OK: auth api`
