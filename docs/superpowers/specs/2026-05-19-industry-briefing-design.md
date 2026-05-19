# Industry Briefing Skill 设计文档

## 目标

新增一个 Claude Code Skill (`industry-briefing`)，一键完成：增量读取 → AI 内容分类 → 输出 Markdown 简报。

## 架构

```
用户触发 "/industry-briefing" 或 "帮我拉取本周信息" 或 "生成行业简报"
         │
         ▼
   ① 读取 data/last_fetch.json → 获取上次成功时间
         │
         ▼
   ② 查 SQLite articles 表：WHERE create_time > last_success_at ORDER BY create_time DESC
      （首次运行回看 7 天）
         │
         ▼
   ③ Claude 批量读取文章，每次处理 ≤10 篇，按标题+摘要+正文前 500 字归类到 6 行业 + 其他
         │
         ▼
   ④ 确保 data/briefings/ 目录存在（不存在则创建）
         按行业分组输出 Markdown 表格 → 先写临时文件，成功后再 rename 到最终路径
         │
         ▼
   ⑤ 仅在步骤④成功后更新 last_fetch.json：
      last_success_at = 本批文章的最大 create_time，last_attempt_at = 当前时间
```

## AI 分类机制

分类由 Claude Code 自身完成，不依赖外部 API。Skill 内嵌分类 prompt（见下方"分类规则"），Claude 读取文章数据后直接判断归属。

**批处理流程**：

1. 用 SQL `LIMIT 10 OFFSET 0` 查询前 10 篇，Claude 逐篇分类后，用 `jq` 安全序列化写入临时文件 `data/briefings/.tmp-classification.jsonl`：
   ```
   jq -n --arg title "$TITLE" --arg url "$URL" '{title: $title, url: $url, category: "金融"}' >> data/briefings/.tmp-classification.jsonl
   ```
   使用 `jq -n --arg` 避免标题中的双引号、换行等特殊字符破坏 JSON 格式
2. 用 `OFFSET 10` 取下一批 10 篇，追加写入同一 JSONL 文件（每行一个 JSON 对象）
3. 重复直到 `OFFSET >= 总篇数`
4. 全部分类完成后，读 JSONL 文件，按行业分组生成最终 Markdown 简报
5. 删除临时 JSONL 文件

传给分类器的文章正文需先剥离 HTML 标签，取纯文本前 500 字。由 Skill 通过 Bash 调用 Python 脚本来完成：

```
python3 -c "
import sys
from bs4 import BeautifulSoup
html = sys.stdin.read()
text = BeautifulSoup(html, 'html.parser').get_text()[:500]
print(text)
" <<'PYEOF'
<HTML内容>
PYEOF
```

使用 heredoc 传内容，避免命令行参数长度限制。bs4 已在项目依赖中，无需新增。

## 数据来源

- **数据库文件**：`data/db.db`（相对于项目根目录 `we-mp-rss/`）
- **articles 表**：读取 `title`, `url`, `content`, `content_html`, `description`, `publish_time`, `create_time`
  - 正文取用优先级：`content_html`（适合 HTML 剥离）→ `content` → `description` → 均空则仅标题分类（低置信度）
- **所有文件路径**（`data/last_fetch.json`、`data/briefings/` 等）均相对于项目根目录
- **查询条件**：`WHERE create_time > ? ORDER BY create_time DESC`，参数为 `last_success_at` 的值（无额外偏移）
  - `create_time` 是 INTEGER 类型的 Unix 时间戳，记录文章首次入库时间，适合做增量查询
  - 使用严格大于 `>` 而非 `>=`：`last_success_at` 取的是上一批文章的最大 `create_time`，严格大于确保同一批不会被重复收录
- **状态文件**：`data/last_fetch.json`

### 状态文件格式

```json
{
  "last_success_at": 1766025000,
  "last_attempt_at": 1766100000
}
```

字段说明：
- `last_success_at`：本批已处理文章的最大 `create_time`（Unix 秒），仅在简报文件写入成功后更新。作为下次查询的起点，使用 `WHERE create_time > ?` 可确保不漏文章也不重复收录
- `last_attempt_at`：上次**尝试**运行的时间戳，无论成功与否都更新。可用于判断脚本是否在正常运行、排查定时任务是否卡住

首次运行时文件不存在或 `last_success_at` 为 null，默认回看 7 天（即 `now_ts - 604800`）。

## 错误处理

| 场景 | 行为 |
|------|------|
| `last_fetch.json` 不存在 | 视为首次运行，回看 7 天 |
| `last_fetch.json` 格式损坏 | 告知用户文件损坏，请手动修复或删除后重试，终止执行 |
| SQLite DB 无法访问 | 告知用户检查数据库路径和权限，终止执行 |
| `data/briefings/` 目录不存在 | 自动创建 |
| 文件写入失败（磁盘满、权限不足） | 告知用户具体错误原因，终止执行，**不更新** `last_success_at` |
| 查询结果为空（0 篇新文章） | 输出空结果模板，更新 `last_attempt_at`，不更新 `last_success_at` |
| 单篇文章正文为空（content、content_html 和 description 均为 null） | 仅根据标题分类，标题后加 `†` 标记低置信度 |
| 文章数量 > 100 篇 | 提示用户文章数量较多（显示具体篇数），建议手动缩小时间窗口后重试。用户确认后继续执行；用户拒绝则终止 |

关于空结果时 `last_success_at` 不更新的行为：这是刻意设计的——不跳过空白期，下次运行继续从同一时间点查询，直到有新文章入库。避免因为抓取延迟导致漏掉刚好在两次运行之间入库的文章。

关于并发：两次快速连续调用会处理同一批文章并生成两份简报文件。与重试行为一致，不做去重。如果未来接入定时器，需考虑加锁。

## 中途失败的重试行为

如果步骤③完成后步骤④写入失败，下次运行从同一 `last_success_at` 重新查询并重新处理。由于 `last_success_at` 未更新，同一批文章会被重新分类。这是可接受的行为：不会丢失数据，重复的简报文件可由用户自行清理。JSONL 中间文件不做断点恢复——每次从头处理整批。

## 低置信度标记

内容为空的文章在输出表格中标题后加 `†` 后缀，并在该行业分类表格下方添加脚注：

```
> † 仅根据标题分类，置信度较低
```

## 分类规则

Skill 内嵌以下 system prompt（供 Claude 自身使用）：

```
你是行业分类助手。将微信公众号文章分类到以下类目之一：

| 类目 | 典型主题关键词 |
|------|---------------|
| 金融 | 银行、保险、证券、基金、支付、信贷、理财、数字货币、央行 |
| 教育 | K12、高等教育、职业教育、在线教育、留学、考研、培训 |
| 汽车 | 新能源车、智能驾驶、车联网、汽车营销、出行服务 |
| 家居建材 | 家装、建材、家具、家电、地产、家居零售、智能家居 |
| AI | 大模型、AGI、AIGC、算力、GPU、深度学习、具身智能 |
| 通信 | 5G、6G、运营商、光通信、基站、频谱、物联网连接 |
| 其他 | 无法归入以上任何类目的文章 |

规则：
1. 优先看标题，标题不清晰时读正文前 500 字
2. 一篇涉及多个类目的，选最核心的一个
3. 实在无法判断的归入"其他"
4. 仅输出结构化结果，不给额外解释

输出格式（每篇文章一行）：
标题: <文章标题> | 类目: <类目名称>
```

## 输出格式

**文件名**：`data/briefings/YYYY-MM-DD-行业简报.md`
- 同一天多次运行时，扫描 `data/briefings/` 目录下匹配 `YYYY-MM-DD-行业简报*.md` 的文件，用正则 `_(\d+)\.md$` 提取序号，取最大值 +1。无序号文件视为第 1 份，下一份为 `_2`：`2026-05-19-行业简报_2.md`
- 写入方式：先写临时文件（同目录 `.tmp` 后缀），写入成功后 `mv` 到最终路径，避免磁盘满导致半截文件

**输出头部日期范围**：取本次查询返回文章的最小和最大 `create_time`，格式化为 `YYYY.MM.DD`。与增量查询字段一致，反映本次简报覆盖的入库时间段。上次抓取时间从 `last_success_at` 格式化。

```markdown
# 公众号行业简报
> 2026.05.13 - 2026.05.19 | 共 23 篇 | 上次抓取 2026.05.12

## 金融 (8篇)
| 标题 | 原文链接 |
|------|---------|
| 央行降息0.25个百分点 | [链接](https://mp.weixin.qq.com/s/xxx) |
| 某文章标题 † | [链接](https://mp.weixin.qq.com/s/yyy) |
> † 仅根据标题分类，置信度较低

## AI (7篇)
| 标题 | 原文链接 |
|------|---------|
| DeepSeek发布新模型 | [链接](https://mp.weixin.qq.com/s/zzz) |

## 汽车 (5篇)
...

## 其他 (3篇)
...
```

- 分类按文章数量降序排列（篇数多的在前）
- 标题中的篇数计数包含该分类下的全部文章（含 `†` 低置信度文章）
- 分类下无文章时不显示该分类标题（例如本次无教育类文章则跳过"教育"）
- 只有存在低置信度文章的分类才显示 `†` 脚注
- "其他"分类始终排在最后

**空结果输出**（0 篇时）：
```markdown
# 公众号行业简报
> 2026.05.19 | 本期无新文章（上次抓取 2026.05.12）
```

## Skill 文件

- **位置**：`./.claude/skills/industry-briefing.md`（项目根目录为 `we-mp-rss/`；`.claude/skills/` 目录需在创建 skill 文件时一并创建）

**YAML 前页**：

```yaml
---
name: industry-briefing
description: 增量拉取公众号文章，按行业分类输出简报（金融、教育、汽车、家居建材、AI、通信）
allowed-tools: [Bash, Read, Write, Edit]
triggers:
  - /industry-briefing
  - 帮我拉取本周信息
  - 生成行业简报
  - 拉取公众号简报
---
```

**权限**：需允许 Bash（查 SQLite、HTML 剥离）、Read/Write（`data/last_fetch.json`、`data/briefings/`）。

**行为**：Skill 被触发后，Claude Code 直接按本文档流程执行：读状态 → 查 DB → 逐批分类 → 写文件 → 更新状态。不通过独立的 Python 脚本驱动。

## 不涉及

- 不修改数据库 schema
- 不新增 Python 依赖
- 不改变现有抓取/导出逻辑
- 不处理视频、音频
- 不执行文章抓取（仅读取 DB 中已有数据；抓取由现有定时任务完成）

## 已知局限

- 无。`create_time` 列已有索引 `ix_articles_create_time`，性能可满足当前及未来规模
