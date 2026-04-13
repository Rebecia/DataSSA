# Database Agent（数据库分析助手）

一个面向数据分析/排障的本地 CLI 工具，提供两种交互方式：

- **SQL 模式（只读、无需 LLM）**：直接执行 `SELECT/WITH`，或用 `/tables`、`/desc`、`/stats` 等命令快速探索数据。
- **自然语言模式（LLM + MCP 工具）**：用自然语言描述问题，Agent 自动调用数据库工具并输出解读报告。

本项目的数据库访问通过 **MCP Server** 暴露的只读工具完成，并带有安全拦截与审计日志（`audit.log`）。

> 基于开源项目 Nanobot 的引擎能力进行集成（vendored），并在此基础上增加了：只读 SQL 模式、数据库 MCP Server、安全拦截与审计、项目化 CLI 封装。

---

## 功能特性

- 只读数据库查询工具（MCP）：
  - `list_tables` / `describe_table` / `get_statistics` / `query_database(SELECT only)`
- SQL 安全防护：
  - 禁止非 SELECT、禁止多语句、拦截常见注入模式（UNION、OR 1=1、注释、延时等）
- 审计日志：
  - 每次查询/拦截都会写入 `reports/audit.log`（JSONL）
- 两种交互模式：
  - SQL 模式不调用 LLM，速度快、可预测
  - 自然语言模式适合业务提问与自动报告

---

## 目录结构

- `bin/database-agent`：项目 CLI 入口（封装引擎、默认注入 `--config`、支持 `--mode`）
- `config.json`：运行配置（模型、workspace、MCP server 等）
- `workspace/`：Agent 工作区（`AGENTS.md` / `MEMORY.md` 等）
- `mcp_server/db_server.py`：数据库查询 MCP Server（SQLite + 安全拦截 + 审计）
- `data/`：示例数据库/数据文件（默认 `data/business.db`）
- `reports/`：审计日志与分析输出（默认 `reports/audit.log`）
- `vendor/nanobot-main/`：vendored 的 Nanobot 引擎源码（保留上游 LICENSE）
- `sql_mode.py`：SQL 模式 REPL（不调用 LLM，直连 MCP）

---

## 快速开始（本机）

### 1) 安装依赖

```bash
cd database-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 配置密钥

复制并填写 `.env`：

```bash
cp .env.example .env
```

> 自然语言模式需要 LLM API Key。SQL 模式不需要。

### 3) 启动交互（会话内选择模式）

```bash
./bin/database-agent agent
```

也可以强制指定模式：

```bash
./bin/database-agent agent --mode sql
./bin/database-agent agent --mode nl
```

---

## Docker（并发测试）

```bash
cd database-agent
docker compose up -d --build
docker compose exec -it database-agent ./bin/database-agent agent --mode sql
```

并行压测示例（10 并发、50 次）：

```bash
docker compose exec -it database-agent sh -lc 'seq 50 | xargs -P 10 -I{} ./bin/database-agent agent --mode sql -m "SELECT COUNT(*) AS total_users FROM users"'
```

---

## License & Attribution

- 本项目包含 vendored 的 Nanobot 源码与其 LICENSE：`vendor/nanobot-main/LICENSE`。
