# 项目交接文档（Handoff）

> 生成时间：2026-06-02
> 适用对象：接手本项目的 coding agent / 开发者
> 仓库地址：https://github.com/zpByte/we-mp-rss.git（fork: zpByte/we-mp-rss）

---

## 一、项目是什么

**we-mp-rss** 是一个微信公众号 RSS 聚合系统，核心功能：

1. 用微信 Web 协议 / Playwright 抓取公众号文章，存入本地数据库
2. 对外提供 RSS / Atom Feed，供 RSS 阅读器订阅
3. 提供 Web UI 管理页面（Vue3 前端），进行公众号管理、文章阅读、过滤规则等
4. 支持级联部署（父节点 / 子节点任务分发）
5. **新增（本次）**：通过 MCP Server 把核心数据能力暴露给 AI Agent / Skill 调用

---

## 二、项目架构

### 目录结构

```
we-mp-rss/
├── main.py               # 启动入口：FastAPI + 后台任务 + 初始化
├── web.py                # FastAPI 应用定义，注册所有路由
├── mcp_server.py         # 【新增】MCP Server，暴露工具给 AI Agent（详见第四节）
├── config.yaml           # 运行时配置（git-ignored，从 config.example.yaml 复制）
├── config.example.yaml   # 配置模板
├── requirements.txt      # Python 依赖
│
├── apis/                 # HTTP 接口层（FastAPI Router）
│   ├── article.py        # 文章 CRUD
│   ├── mps.py            # 公众号管理
│   ├── rss.py            # RSS/Atom Feed 生成
│   ├── auth.py           # 认证（JWT + AK-SK）
│   ├── cascade.py        # 级联节点管理
│   ├── filter_rule.py    # 过滤规则
│   ├── tools.py          # 工具接口（导出等）
│   └── ...               # 其他接口
│
├── core/                 # 核心业务逻辑
│   ├── db.py             # 数据库连接（SQLAlchemy，全局单例 DB）
│   ├── config.py         # 配置读取（YAML）
│   ├── auth.py           # 认证逻辑（JWT + AK-SK 校验 authenticate_ak）
│   ├── article_content.py# 文章正文抓取 / 同步（sync_article_content）
│   ├── models/           # SQLAlchemy ORM 模型
│   │   ├── article.py    # Article / ArticleBase（含 content/content_html）
│   │   ├── feed.py       # Feed（公众号订阅）
│   │   ├── access_key.py # AccessKey（AK-SK 认证）
│   │   └── ...
│   └── ...
│
├── driver/               # 微信 / 浏览器驱动
│   ├── wxarticle.py      # Playwright 抓取文章正文（Web 模式）
│   └── wx/               # 微信 API 模式
│
├── jobs/                 # 后台定时任务
│   ├── article.py        # 公众号文章更新任务（UpdateArticle）
│   └── ...
│
├── web_ui/               # Vue3 前端（npm 构建后输出到 static/）
├── static/               # 前端构建产物（git-tracked）
│
├── data/                 # 运行时数据（git-ignored）
│   ├── db.db             # SQLite 数据库
│   ├── last_fetch.json   # skill 抓取状态
│   └── briefings/        # 简报输出目录
│
├── .codeflicker/
│   └── skills/
│       └── industry-briefing/
│           └── SKILL.md  # 【已升级 v2.0】公众号行业简报 Skill
│
└── docs/
    ├── mcp.md            # 【新增】MCP Server 部署 & 接入指南
    └── HANDOFF.md        # 本文件
```

### 数据模型

| 表 | 说明 |
|----|------|
| `articles` | 文章主表，含 `id`、`mp_id`、`title`、`url`、`publish_time`、`create_time`、`content`、`content_html`、`status`、`has_content`、`is_favorite`、`is_read` 等 |
| `feeds` | 公众号订阅表，含 `id`（mp_id）、`mp_name`、`status`（1=启用）、`sync_time` |
| `access_keys` | AK-SK 认证表，含 `key`、`secret`（哈希）、`is_active` |
| `users` | 用户表 |
| `cascade_nodes` | 级联节点 |

### 技术栈

| 层 | 技术 |
|----|------|
| Web 框架 | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.x |
| 数据库 | SQLite（默认）/ MySQL / PostgreSQL |
| 前端 | Vue3 + Vite |
| 抓取 | Playwright（Web 模式）+ 微信 API 模式 |
| MCP | `mcp[cli]>=1.0.0`（FastMCP） |
| 认证 | JWT + AK-SK |

---

## 三、用户需求背景（本次对话）

### 核心诉求

> "我订阅了 13 个公众号（巨量引擎、小红书商业动态、腾讯营销等平台营销账号），
> 每周希望能自动汇总成行业简报，供我快速了解竞品动态。"

### 已有但待改善的状况

- we-mp-rss 已部署在本机，有 Web UI
- 原有 `industry-briefing` skill（CodeFlicker）通过**直读本机 SQLite 文件**生成简报
  - 缺点：skill 必须和数据库同机运行；路径写死；不适合远程部署
- 抓取任务从 **2026-05-26 停止运行**（原因未排查），导致近一周数据空白

### 本次会话做了什么

1. 分析了把 we-mp-rss 改造成 MCP 工具的可行性，确认方向
2. 实现了 `mcp_server.py`（MCP Server）
3. 给 MCP Server 加了 AK-SK 认证中间件
4. 写了 `docs/mcp.md` 部署指南
5. 把 `industry-briefing` skill 从 v1.x（直读 SQLite）改写为 v2.0（调 MCP 工具）
6. 摸底了数据库现状，发现抓取停摆

---

## 四、MCP Server（核心新增内容）

### 文件：`mcp_server.py`

暴露的工具（`@mcp.tool`）：

| 工具 | 说明 |
|------|------|
| `list_feeds(only_active)` | 列出所有订阅公众号 |
| `list_recent_articles(since_ts, mp_ids?, limit, order)` | 增量拉取文章列表（不含正文） |
| `search_articles(keyword, mp_ids?, limit)` | 标题关键字搜索 |
| `get_article_content(article_id, auto_fetch_if_missing)` | 单篇完整正文 |
| `get_articles_text_bulk(article_ids[≤50], text_max_chars)` | 批量取正文片段（含首图 URL / 是否纯图片） |
| `refresh_article(article_id, force)` | 触发单篇在线重新抓取 |
| `count_articles(since_ts, group_by_mp)` | 文章数量统计 |
| `get_fetch_state()` | 读取 `data/last_fetch.json` |
| `update_fetch_state(last_success_at, touch_attempt)` | 写入 `data/last_fetch.json` |

暴露的资源（`@mcp.resource`）：

| URI | 内容 |
|-----|------|
| `wemp://meta/server-info` | DB 路径 / 版本 / 公众号数 / 文章数 |
| `wemp://meta/feeds` | 所有启用公众号 JSON |

### 认证设计

- **stdio 模式**：默认不强制认证（本机进程通信）
- **SSE / HTTP 模式**：开启 `AKSKAuthMiddleware`

三档优先级（命中任一放行）：
1. `WE_MP_RSS_MCP_AUTH=disabled` → 全部放行（仅调试）
2. 环境变量 `WE_MP_RSS_MCP_AK` / `WE_MP_RSS_MCP_SK`（明文，分号分隔多组）
3. 数据库 `access_keys` 表 → 复用 `core.auth.authenticate_ak`

请求头格式：`Authorization: AK-SK <ak>:<sk>`

### 启动命令

```bash
# 安装依赖（只需一次）
pip3 install "mcp[cli]>=1.0.0"

# stdio 模式（本机 CodeFlicker / Claude Desktop 用）
WE_MP_RSS_CONFIG=/path/to/config.yaml python3 mcp_server.py

# SSE 模式（远端 / Docker）
export WE_MP_RSS_MCP_AK="WK-xxx"
export WE_MP_RSS_MCP_SK="SK-xxx"
python3 mcp_server.py --transport sse --host 0.0.0.0 --port 8765

# 健康检查
curl http://127.0.0.1:8765/healthz
```

### CodeFlicker 配置示例（`~/.codeflicker/mcp.json`）

```json
{
  "mcpServers": {
    "we-mp-rss": {
      "command": "python3",
      "args": ["/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss/mcp_server.py"],
      "env": {
        "WE_MP_RSS_CONFIG": "/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss/config.yaml"
      }
    }
  }
}
```

---

## 五、industry-briefing Skill v2.0

**文件**：`.codeflicker/skills/industry-briefing/SKILL.md`

**触发词**："帮我拉取本周信息" / "生成行业简报" / `/industry-briefing`

**工作流（MCP 版）**：

```
get_fetch_state()                    # 1. 读状态
  → count_articles(since_ts)         # 2. 预检数量（0 则跳 5）
  → list_recent_articles(since_ts)   # 3. 拉列表（含 mp_name）
  → get_articles_text_bulk(ids×50)   # 4. 批量取正文片段
  → LLM 分类（7 类目）               # 5. AI 分类
  → LLM 摘要（≤50 字）               # 6. AI 摘要
  → 写 data/briefings/竞品信息简报（YY.MM.DD-YY.MM.DD）.md  # 7. 输出
  → update_fetch_state(max_ts)       # 8. 回写状态
```

输出格式：Markdown 表格（公众号 / 发布时间 / 标题链接 / 摘要），按 AI / 金融 / 教育 / 汽车 / 家居建材 / 通信 / 其他 分类。

**降级**：MCP 不可用时回退到直读 `data/db.db`（见 skill 附录 A）。

---

## 六、13 个活跃公众号（截至 5/26）

| 公众号 | 最近一篇文章时间 |
|--------|-----------------|
| 腾讯研究院 | 2026-05-26 00:12 |
| 巨量引擎营销观察 | 2026-05-25 20:57 |
| 小红书商业动态 | 2026-05-25 20:21 |
| 腾讯营销 | 2026-05-25 18:29 |
| 巨懂车商业动态 | 2026-05-25 18:15 |
| 抖音生活服务商业观察 | 2026-05-25 17:55 |
| 百度营销观 | 2026-05-25 14:55 |
| 百度营销 | 2026-05-25 14:13 |
| 腾讯营销品牌观察 | 2026-05-25 10:58 |
| 巨量本地推 | 2026-05-21 21:20 |
| 百度营销中心 | 2026-05-18 19:29 |
| 巨量引擎营销科学 | 2026-04-09 11:27 |
| 巨量营销平台 | 2026-04-01 14:30 |

> ⚠️ 从 **2026-05-26 11:52** 开始抓取任务停止运行，近一周数据空白，待排查。

---

## 七、未完成的待办事项

### 🔥 P0（阻塞生成简报）

- [ ] **T1**：排查 `main.py -job True` 后台任务为何从 5/26 后停止（看 `jobs/article.py:UpdateArticle`、日志、是否进程挂了）
- [ ] **T2**：重启抓取，补齐 5/26 → 今日的文章
- [ ] **T3**：T2 完成后跑一次 v2.0 skill 验证端到端链路，确认简报能正常输出

### 🟡 P1（MCP 闭环）

- [ ] **T4**：`requirements.txt` 加一行 `mcp[cli]>=1.0.0`
- [ ] **T5**：`compose/docker-compose.mcp.yaml` 落地（模板已在 `docs/mcp.md` 第 6 节）
- [ ] **T6**：创建 MCP 专用 AK（调 `POST /api/auth/ak/create`），写入 `.env`
- [ ] **T7**：配 `~/.codeflicker/mcp.json`，CodeFlicker 端到端验证（健康检查 → 工具调用）

### 🟢 P2（体验优化）

- [ ] **T8**：在 `.codeflicker/skills/industry-briefing/` 加 `MY_FEEDS.yml`，填入"常关注公众号"白名单，skill 自动加载后传 `mp_ids`
- [ ] **T9**：给 `mcp_server.py` 加 `trigger_mp_sync(mp_id)` 工具，让 skill 能主动补抓（包 `jobs/article.py:UpdateArticle`）
- [ ] **T10**：加 `tests/test_mcp_server.py`，覆盖：AK-SK 鉴权 / `list_feeds` / `list_recent_articles` / `get_articles_text_bulk`
- [ ] **T11**：把 v2.0 skill 接入 `daily-ai-report` / `boss-weekly-report` skill，做日报 / 周报联动

### 🔵 P3（生产化 / 安全）

- [ ] **T12**：MCP 加只读 AK 白名单（`refresh_article` / `update_fetch_state` 是写操作，应限制）
- [ ] **T13**：Nginx + HTTPS + IP 白名单（详见 `docs/mcp.md` 第 7 节）
- [ ] **T14**：`SECURITY.md` 补 MCP 暴露面说明

---

## 八、运行 & 开发常用命令

```bash
# 后端启动（含后台任务 + 初始化）
python3 main.py -job True -init True

# 仅启动 FastAPI（不跑定时任务）
python3 main.py

# MCP Server（本机调试）
python3 mcp_server.py

# 前端开发
cd web_ui && npm install && npm run dev

# 前端构建
cd web_ui && npm run build

# 查看 API 文档
open http://localhost:8001/api/docs

# 查看数据库
sqlite3 data/db.db ".tables"
sqlite3 data/db.db "SELECT COUNT(*) FROM articles"

# 检查近 7 天是否有新文章
python3 -c "
import sqlite3, time
c = sqlite3.connect('data/db.db').cursor()
n = c.execute('SELECT COUNT(*) FROM articles WHERE create_time > ?', (int(time.time())-604800,)).fetchone()[0]
print(f'近 7 天新增文章: {n}')
"
```

---

## 九、注意事项

1. **`config.yaml` 不提交**：包含数据库路径、微信 Cookie、代理配置等敏感信息，从 `config.example.yaml` 复制后本地填写
2. **SQLite WAL 模式已开启**：MCP Server 和主进程可以并发读写
3. **微信 Cookie 时效**：抓取依赖微信登录态，Cookie 过期会导致抓取失败，需重新扫码登录（Web UI 有引导）
4. **中国大陆 IP 问题**：微信服务器可能屏蔽数据中心 IP，建议用 `compose/` 的 `singbox` sidecar 配置代理
5. **`python3` / `pip3`**：此机器环境需显式使用 `python3` 和 `pip3`，不要用无后缀版本
