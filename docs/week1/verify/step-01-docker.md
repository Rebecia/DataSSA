# 步骤 1 验收手册（新设备 / Docker 刚装好时用）

> 对应：[step-01-docker.md](../step-01-docker.md)
> 代码已在仓库中；本文仅记录**你怎么测**，安装 Docker 后按顺序执行即可。

---

## 0. 前置

- 已安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（或 Docker Engine + Compose v2）
- 终端执行 `docker --version`、`docker compose version` 有输出

---

## 1. 启动 MySQL + Redis

```bash
cd "/Users/edy/Desktop/工作/LLM/database-agent"

docker compose up -d mysql redis
```

首次会拉 `mysql:8.0`、`redis:7-alpine` 镜像，可能需几分钟。

---

## 2. 查看状态

```bash
docker compose ps
```

**期望：**

| 服务  | STATE                   |
| ----- | ----------------------- |
| mysql | running（healthy 更佳） |
| redis | running（healthy 更佳） |

若 `mysql` 长期 `starting`，再等 30–60 秒后重试 `docker compose ps`。

---

## 3. 连通性自检（可选）

```bash
# Redis
docker compose exec redis redis-cli ping
# 期望：PONG

# MySQL
docker compose exec mysql mysqladmin ping -h 127.0.0.1 -uroot -pdatasaa_root
# 期望：mysqld is alive
```

---

## 4. 常见问题

| 现象                                       | 处理                                                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `port is already allocated`（3306/6379） | 改 `docker-compose.yml` 端口映射，如 `"3307:3306"`、`"6380:6379"`                      |
| `command not found: docker`              | 安装 Docker 并重启终端                                                                       |
| mysql 反复重启                             | `docker compose logs mysql` 查看错误；删卷重来：`docker compose down -v`（会清空库数据） |

---

## 5. 验收打勾

全部通过后，在 [step-01-docker.md](../step-01-docker.md) 确认两项已为 `[x]`，并回复：**步骤 1 通过**。

```text
[ ] docker compose ps：mysql、redis 均为 running/healthy
[ ] redis-cli ping → PONG（可选）
[ ] mysqladmin ping → alive（可选）
```

---

## 6. 服务名说明

Compose 里 API 服务名为 **`api`**（原 `database-agent`）。后续：

```bash
docker compose up -d api          # 启动 API（依赖 mysql/redis 健康）
docker compose exec api ...       # 进入 API 容器
```

步骤 1 **不要求**启动 `api`，只测 mysql + redis。
