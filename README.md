# DataSSA

  在实际生产中，数据相关部分门常常需要查询数据库做分析，但直接给每个人开数据库直连权限存在较大数据污染风险。
  所以我们需要一种安全的方式，这里我设计了基于 Nanobot 框架的 LLM Agent 的数据分析工具，它在帮团队查询数据的同时也能确保数据库安全。

---
## 项目特点

- **安全边界清晰**：设置四层安全边界：
  - SQL 语句解析，只允许 SELECT 和 WITH 开头的查询；
  - 危险关键字检测，拦截 DROP、DELETE、UPDATE 等 14 个危险关键字；
  - 注入模式检测，用正则匹配 UNION SELECT、OR 1=1 等 12 种常见注入模式；
  - 多语句检测，禁止分号分隔的多语句执行。
  
- **资源限制**：查询超时 30 秒自动中断，结果限制 1000 行，并发控制最多 5 个查询。

- **完整审计日志**：每次查询都记录 SQL、执行时间、返回行数；被拦截的危险查询也会记录，用于安全分析。

- **循最小权限原则**：只暴露 4 个工具——query_database、list_tables、describe_table、get_statistics，每个工具职责明确、边界清晰。

- **多模式设计**：项目提供 SQL + 自然语言双模式。SQL 模式用于快速检索，延迟低，减少不必要的 token 消耗；自然语言模式可以更好地用于探索性问题，生成分析报告等。

---

## 主要文件

- `config.json`：运行配置（模型、workspace、MCP server 等）
- `workspace/`：Agent 工作区
- `data/`：示例数据库/数据文件
- `reports/`：审计日志与分析输出（默认 `reports/audit.log`）

---

## 快速开始

### 1) 安装依赖

```bash
cd database-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 配置 LLM Key

在 `config.json` 里填写你要用的 provider 的 `apiKey`，：

```json
{
  "agents": { "defaults": { "provider": "deepseek" } },
  "providers": { "deepseek": { "apiKey": "sk-xxx", "apiBase": "https://api.deepseek.com" } }
}
```

### 3) 配置数据库连接

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

## Docker

Docker 下 DB 文件通过 volume 挂载到容器内 `/app/data`

```bash
cd database-agent
docker compose up -d --build
docker compose exec -it database-agent ./bin/database-agent agent --mode sql
```

---
