# DataSSA（数据库分析助手）

  数据团队经常需要查询数据库做数据分析，但给每个人都开数据库直连权限风险太大，会存在误执行误操作导致数据被改的事故。
  需要一种安全的方式让 AI Agent 能帮团队查数据，同时确保数据库安全。因此我设计了一个面向数据分析/排障的工具提供两种交互方式：

- **SQL 模式**：直接执行 SQL 命令，快速探索数据。
- **自然语言模式**：用自然语言描述问题，Agent 自动调用数据库工具并输出解读报告。

---
## 项目特点

- **多模式交互**：SQL 模式低延迟、输出更稳定；自然语言模式接收，实现 Agent 自动调用数据库工具并输出解读报告。
- **安全边界清晰**：设置四层安全边界：
  - SQL 语句解析，只允许 SELECT 和 WITH 开头的查询；
  - 危险关键字检测，拦截 DROP、DELETE、UPDATE 等 14 个危险关键字；
  - 注入模式检测，用正则匹配 UNION SELECT、OR 1=1 等 12 种常见注入模式；
  - 多语句检测，禁止分号分隔的多语句执行。
  
- **可追溯**：无论成功查询还是被拦截，都会写入 `reports/audit.log`（JSONL），便于复盘与合规。
- **工程化可落地**：Docker 启动 + 并发压测示例，目录划分清晰（`data/`、`workspace/`、`reports/`）。

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

### 2) 配置 LLM Key（可选）

如果你只用 **SQL 模式**（不调用 LLM），这一步可以先跳过。

如果要用 **自然语言模式**，直接在 `config.json` 里填写你要用的 provider 的 `apiKey`（不依赖 `.env`），例如只改这一小段：

```json
{
  "agents": { "defaults": { "provider": "deepseek" } },
  "providers": { "deepseek": { "apiKey": "sk-xxx", "apiBase": "https://api.deepseek.com" } }
}
```

### 3) 配置数据库连接（把 database MCP 接进来）

数据库访问通过 `mcp_server/db_server.py` 提供的 MCP 工具完成，你需要在 `config.json` 中配置：

- `command`：启动命令（通常 `python3`）
- `args`：MCP server 脚本路径
- `env.DB_PATH`：SQLite DB 文件路径

示例：

```json
{
  "tools": {
    "mcpServers": {
      "database": {
        "command": "python3",
        "args": ["mcp_server/db_server.py"],
        "env": {
          "DB_PATH": "./data/business.db",
          "QUERY_TIMEOUT": "30",
          "MAX_ROWS": "1000",
          "DB_READONLY": "true"
        },
        "description": "安全的数据库查询服务"
      }
    }
  }
}
```

> 审计日志默认写入 `./reports/audit.log`（JSONL，每行一条）。

### 4) 启动交互（会话内选择模式）

```bash
./bin/database-agent agent
```

也可以强制指定模式：

```bash
./bin/database-agent agent --mode sql
./bin/database-agent agent --mode nl
```

### 5) SQL 模式常用命令（示例）

进入 SQL 模式后可用：

- `/tables`：列出表
- `/desc users`：查看表结构
- `/stats users`：查看基础统计
- 直接输入 `SELECT ...`：执行查询（只读）

---

## Docker（并发测试）

```bash
cd database-agent
docker compose up -d --build
docker compose exec -it database-agent ./bin/database-agent agent --mode sql
```

Docker 下 DB 文件通过 volume 挂载到容器内 `/app/data`（见 `docker-compose.yml`），因此：

- `DB_PATH` 推荐使用 `./data/business.db`（相对项目根目录），或在容器内改成 `/app/data/business.db`。

并行压测示例（10 并发、50 次）：

```bash
docker compose exec -it database-agent sh -lc 'seq 50 | xargs -P 10 -I{} ./bin/database-agent agent --mode sql -m "SELECT COUNT(*) AS total_users FROM users"'
```

---
