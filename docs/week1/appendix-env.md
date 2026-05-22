# 附录：环境变量

复制为本地 `.env`（勿提交）。详见 `.env.example`（步骤 2 创建）。

```bash
# 认证
ALLOW_REGISTER=1
JWT_SECRET=change-me-in-production
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=1440

# MySQL（应用库）
DATABASE_URL=mysql+pymysql://datasaa:datasaa@127.0.0.1:3306/datasaa

# Redis（第1周仅配置，业务第2周用）
REDIS_URL=redis://127.0.0.1:6379/0

# 既有 DataSSA
DATASSA_WORKSPACE=./workspace
DATASSA_TEST_MODE=1
```
