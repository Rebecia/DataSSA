# 步骤 1：Docker 增加 MySQL、Redis

## 本步目标

`docker compose up` 能同时启动 **mysql**、**redis**、**api**（原 `database-agent` 服务可改名为 `api` 或保留）。

## 前置条件

- 已读 [step-00-scope.md](./step-00-scope.md)

## 你要做什么

1. 编辑 `docker-compose.yml`：
   - 新增服务 `mysql`（8.x）、`redis`（7.x）
   - `api` 服务 `depends_on`: mysql、redis
2. 为 MySQL 配置：`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD`、端口 `3306`
3. 为 Redis 配置：端口 `6379`
4. **先不要**在应用代码里连库（步骤 2 再做）

## 涉及文件

| 操作 | 路径 |
|------|------|
| 修改 | `docker-compose.yml` |

## 本步完成标志

**验收手册（新设备/Docker 未装好时先保存）：** [verify/step-01-docker.md](./verify/step-01-docker.md)

```bash
cd database-agent
docker compose up -d mysql redis
docker compose ps   # mysql、redis 为 running/healthy
```

- [x] `mysql` 容器运行中（compose 已配置；本地按验收手册确认）
- [x] `redis` 容器运行中（compose 已配置；本地按验收手册确认）

## 下一步

[步骤 2：配置与依赖](./step-02-config.md)
