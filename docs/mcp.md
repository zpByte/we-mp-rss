# we-mp-rss MCP Server 部署 & 接入指南

把 we-mp-rss 暴露为 [Model Context Protocol](https://modelcontextprotocol.io/) 服务，
让 CodeFlicker / Claude Desktop / Cursor / 自研 Agent 等 MCP 客户端可以像调用本地工具
一样，访问公众号订阅、文章、抓取状态等数据。

> 源码位置：[`mcp_server.py`](../mcp_server.py)

---

## 目录

- [1. 能力概览](#1-能力概览)
- [2. 安装](#2-安装)
- [3. 三种部署形态](#3-三种部署形态)
  - [3.1 stdio：本机 MCP 客户端](#31-stdio本机-mcp-客户端)
  - [3.2 SSE：远端 HTTP 服务（推荐）](#32-sse远端-http-服务推荐)
  - [3.3 Streamable HTTP](#33-streamable-http)
- [4. AK-SK 认证](#4-ak-sk-认证)
  - [4.1 三档认证模式](#41-三档认证模式)
  - [4.2 用环境变量配置静态 AK-SK](#42-用环境变量配置静态-ak-sk)
  - [4.3 用数据库 access_keys 表](#43-用数据库-access_keys-表)
  - [4.4 客户端如何带 AK-SK](#44-客户端如何带-ak-sk)
- [5. 客户端接入示例](#5-客户端接入示例)
- [6. Docker 部署](#6-docker-部署)
- [7. 反向代理（HTTPS + Nginx）](#7-反向代理https--nginx)
- [8. 运维 & 排障](#8-运维--排障)
- [9. 安全建议](#9-安全建议)

---

## 1. 能力概览

| Tool | 用途 |
|------|------|
| `list_feeds` | 列出所有订阅的公众号 |
| `list_recent_articles` | 按 `create_time` 增量拉取新文章（简报核心） |
| `search_articles` | 标题关键字模糊搜索 |
| `get_article_content` | 单篇完整正文 + 首图 |
| `get_articles_text_bulk` | 批量取 ≤50 篇正文片段（用于分类 / 摘要） |
| `refresh_article` | 触发单篇在线重新抓取 |
| `count_articles` | 文章数量统计（可按公众号分组） |
| `get_fetch_state` / `update_fetch_state` | 抓取状态读写（替代 `data/last_fetch.json`） |

资源：

| URI | 内容 |
|-----|------|
| `wemp://meta/server-info` | 数据库路径、版本、公众号/文章数量 |
| `wemp://meta/feeds` | 所有启用公众号 JSON |

Prompt：

| 名称 | 作用 |
|------|------|
| `industry_briefing_prompt(lookback_days)` | 注入「行业简报」工作流到对话 |

---

## 2. 安装

```bash
# 1. 进入项目目录
cd /path/to/we-mp-rss

# 2. 复用已有 requirements.txt
pip3 install -r requirements.txt

# 3. 单独安装 MCP SDK
pip3 install "mcp[cli]>=1.0.0"

# 4. 准备配置文件（数据库、抓取等）
cp config.example.yaml config.yaml
# 或：export WE_MP_RSS_CONFIG=/abs/path/to/config.yaml
```

> 项目内已包含 `fastapi` / `uvicorn` / `sqlalchemy` / `beautifulsoup4`，
> MCP Server 直接复用，不需要额外安装这些包。

---

## 3. 三种部署形态

### 3.1 stdio：本机 MCP 客户端

MCP 客户端会自动以子进程方式拉起 `mcp_server.py`，通过 stdin/stdout 通信。

```bash
python3 mcp_server.py                 # 默认 stdio
# 或显式指定
python3 mcp_server.py --transport stdio
```

> **stdio 模式下默认不强制 AK-SK 认证**，因为通信对端就是本机进程，
> 客户端拉起谁、谁就是可信主体。

### 3.2 SSE：远端 HTTP 服务（推荐）

```bash
# 监听公网/容器入口
export WE_MP_RSS_MCP_AK="WK-internal-001"
export WE_MP_RSS_MCP_SK="SK-internal-001"

python3 mcp_server.py \
  --transport sse \
  --host 0.0.0.0 \
  --port 8765
```

启动后：

- MCP 入口：`http://<host>:8765/sse`
- 健康检查：`GET /healthz` → `{"status":"ok"}`

### 3.3 Streamable HTTP

新一代 MCP 传输（无需长连接 SSE），目前已被多数客户端支持。

```bash
python3 mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8765
```

入口路径：`http://<host>:8765/mcp/`（取决于 FastMCP 版本，可在启动日志确认）。

---

## 4. AK-SK 认证

> **认证只在 SSE / streamable-http 模式下生效。stdio 模式默认放行。**

### 4.1 三档认证模式

| 优先级 | 来源 | 触发条件 | 用途 |
|--------|------|----------|------|
| 0 | 关闭 | `WE_MP_RSS_MCP_AUTH=disabled` | 受信内网 / 调试 |
| 1 | 环境变量 | `WE_MP_RSS_MCP_AK/SK` | 简单部署，无需数据库 |
| 2 | `access_keys` 表 | `core.auth.authenticate_ak` | 多租户 / 已用现有 AK 体系 |

只要任一档命中即放行，且会把 `principal` 挂到 ASGI scope 上。

### 4.2 用环境变量配置静态 AK-SK

```bash
# 单组
export WE_MP_RSS_MCP_AK="WK-skill-briefing"
export WE_MP_RSS_MCP_SK="SK-skill-briefing-xxxxxxxxxxxxxxxxxxxxxxx"

# 多组（位置对应，分号分隔）
export WE_MP_RSS_MCP_AK="WK-skill-a;WK-skill-b"
export WE_MP_RSS_MCP_SK="SK-skill-a-xxxxx;SK-skill-b-yyyyy"
```

环境变量的 AK 是明文比对，**仅用于内部信任环境**。对外暴露务必走 4.3。

### 4.3 用数据库 access_keys 表

we-mp-rss 自带的用户 AK 体系（`apis/auth.py:/ak/create` 等）可直接复用：

```bash
# Web UI 或 API 创建一个 AK
curl -X POST http://localhost:8001/api/auth/ak/create \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的JWT>" \
  -d '{"name": "mcp-skill", "permissions": []}'
# 响应里 access_key + secret_key 只展示一次，落库的是 secret 的 hash
```

随后 MCP 客户端用这个 AK/SK 即可，不需要再配 `WE_MP_RSS_MCP_AK`。

### 4.4 客户端如何带 AK-SK

请求头：

```
Authorization: AK-SK <access_key>:<secret_key>
```

- 大多数 MCP 客户端配置项里有 `headers` 字段，直接填即可。
- 健康检查路径 `/healthz`、`/livez`、`/readyz` 以及 `OPTIONS` 预检会跳过认证。
- 鉴权失败返回 `401` + `WWW-Authenticate: AK-SK realm="we-mp-rss-mcp"`。

---

## 5. 客户端接入示例

### 5.1 CodeFlicker / Claude Desktop（stdio）

`~/.codeflicker/mcp.json` 或 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "we-mp-rss": {
      "command": "python3",
      "args": [
        "/abs/path/to/we-mp-rss/mcp_server.py"
      ],
      "env": {
        "WE_MP_RSS_CONFIG": "/abs/path/to/we-mp-rss/config.yaml"
      }
    }
  }
}
```

### 5.2 CodeFlicker / Claude Desktop（远端 SSE）

```json
{
  "mcpServers": {
    "we-mp-rss": {
      "type": "sse",
      "url": "https://wemp.example.com/sse",
      "headers": {
        "Authorization": "AK-SK WK-skill-briefing:SK-skill-briefing-xxxxx"
      }
    }
  }
}
```

### 5.3 Cursor / 其他

参考各客户端文档配置 `url` + `headers`；MCP 协议是统一的。

### 5.4 在 skill 里直接 curl 调用（兜底）

如果 skill 跑在没有 MCP 能力的环境，可以走 streamable-http endpoint：

```bash
curl -N -X POST https://wemp.example.com/mcp/ \
  -H "Authorization: AK-SK $AK:$SK" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0",
    "id":1,
    "method":"tools/call",
    "params":{"name":"list_feeds","arguments":{"only_active":true}}
  }'
```

---

## 6. Docker 部署

最小化在现有镜像基础上叠加 MCP 端口暴露。`compose/docker-compose.dev.yaml` 已有 main 服务，
新增一个 `mcp` 服务复用同一份代码和数据卷：

```yaml
# compose/docker-compose.mcp.yaml
services:
  wemp-mcp:
    image: we-mp-rss:latest          # 同 main.py 用的镜像
    container_name: wemp-mcp
    restart: unless-stopped
    command: >
      python3 mcp_server.py
      --transport sse
      --host 0.0.0.0
      --port 8765
    environment:
      WE_MP_RSS_CONFIG: /app/config.yaml
      WE_MP_RSS_MCP_AUTH: enabled
      WE_MP_RSS_MCP_AK: ${MCP_AK}
      WE_MP_RSS_MCP_SK: ${MCP_SK}
    volumes:
      - ../config.yaml:/app/config.yaml:ro
      - ../data:/app/data
    ports:
      - "8765:8765"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8765/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
```

`.env` 同时放：

```
MCP_AK=WK-prod-skill
MCP_SK=SK-prod-skill-xxxxxxxxxxxxxxxxxxxxxxx
```

启动：

```bash
docker compose -f compose/docker-compose.dev.yaml \
               -f compose/docker-compose.mcp.yaml up -d --force-recreate
```

> ⚠️ MCP Server 与主服务共享 `data/db.db`。MCP Server 默认只读多、只在
> `refresh_article` / `update_fetch_state` 时写，对锁影响很小；SQLite WAL 即可。

---

## 7. 反向代理（HTTPS + Nginx）

```nginx
# /etc/nginx/conf.d/wemp-mcp.conf
upstream wemp_mcp {
    server 127.0.0.1:8765;
    keepalive 64;
}

server {
    listen 443 ssl http2;
    server_name wemp.example.com;

    ssl_certificate     /etc/ssl/wemp.crt;
    ssl_certificate_key /etc/ssl/wemp.key;

    # SSE 必备：禁用 buffering、拉长超时
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 1h;
    proxy_send_timeout 1h;
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    # 透传客户端 Authorization
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location / {
        proxy_pass http://wemp_mcp;
    }
}
```

---

## 8. 运维 & 排障

| 现象 | 排查 |
|------|------|
| 启动报 `ModuleNotFoundError: mcp` | `pip3 install "mcp[cli]>=1.0.0"` |
| 启动报 `sqlite3.OperationalError: unable to open database file` | 检查 `WE_MP_RSS_CONFIG` 指向的 yaml 中 `db` 路径；Docker 内常见的是路径不存在或挂载错 |
| 客户端连接后立刻 401 | `Authorization` 头是否带上、格式是否为 `AK-SK ak:sk` |
| 客户端 401 且日志显示 `[mcp] 未检测到 WE_MP_RSS_MCP_AK/SK` | 没配环境变量也没在 access_keys 里建 AK |
| SSE 连接 1 分钟后断开 | 检查反向代理是否禁用了 buffering / 加了 `proxy_read_timeout` |
| 工具调用返回 `not_found` | 文章 ID 格式：`<mp_id 去掉 MP_WXS_ 前缀>-<原始 aid>`，可以先用 `list_recent_articles` 取到再拿来调用 |
| 想看 server 元信息 | 客户端读取资源 `wemp://meta/server-info` |
| 想关闭鉴权（仅内网调试） | `export WE_MP_RSS_MCP_AUTH=disabled` |

健康检查：

```bash
curl -fsS http://127.0.0.1:8765/healthz
# {"status":"ok"}
```

日志：MCP Server 通过 `core.print` 输出到 stdout，Docker / systemd 直接 `journalctl` / `docker logs` 即可。

---

## 9. 安全建议

1. **永远不要在公网裸跑**：至少加上 4.2 / 4.3 的 AK-SK，最好再叠上 Nginx + HTTPS + IP 白名单。
2. **AK-SK 不要写进代码仓库**：建议放 `.env`、`docker secrets` 或 Vault；本仓 `.gitignore` 已忽略 `config.yaml` / `.env`。
3. **MCP 默认无写隔离**：`refresh_article`、`update_fetch_state` 是写操作，若担心客户端误用，
   可在 ASGI 中间件里基于 `principal["ak"]` 做白名单（修改 `mcp_server.py` 的 `AKSKAuthMiddleware`）。
4. **数据库共享**：MCP 与主服务共用 SQLite 时确保用 WAL 模式（项目默认即开启）。如果换 MySQL/PostgreSQL，
   建议给 MCP 单独一个只读账号（除 `articles.has_content` 与 `last_fetch.json` 文件外，其它都是读）。
5. **资源配额**：建议在反向代理层做 QPS / 并发限制，避免 LLM 风暴打爆 DB。

---

## 附：与现有 industry-briefing skill 的衔接

`.codeflicker/skills/industry-briefing/SKILL.md` 中：

| 旧步骤 | 新调用 |
|--------|--------|
| `cat data/last_fetch.json` | `get_fetch_state()` |
| `sqlite3 data/db.db "SELECT ..."` | `list_recent_articles(since_ts=...)` |
| 查询 `feeds` 表做 mp_id → mp_name 映射 | `list_feeds()` |
| Python heredoc 剥 HTML + 截前 500 字 | `get_articles_text_bulk(article_ids, text_max_chars=500)` |
| `python3 -c "json.dump(...)"` 写 last_fetch.json | `update_fetch_state(last_success_at=...)` |

这样 skill 就可以从「必须跑在同机 + 直读 SQLite」升级为
「任何带 MCP 的 Agent + 通过认证即可调用」，
也方便接入到日报 / 周报 / boss-weekly 等其它 skill 中。
