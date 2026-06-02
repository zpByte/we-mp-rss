---
name: industry-briefing
description: 增量拉取公众号文章，按行业分类输出 Markdown 简报（金融、教育、汽车、家居建材、AI、通信）
allowed-tools: [Bash, Read, Write, Edit]
triggers:
  - /industry-briefing
  - 帮我拉取本周信息
  - 生成行业简报
  - 拉取公众号简报
---

# 公众号行业简报生成器

增量读取 SQLite 中新增文章，AI 分类后输出结构化 Markdown 简报。

## 工作流程

### 1. 读取状态

```
cat data/last_fetch.json
```

文件不存在则视为首次运行，回看 7 天（`now_ts - 604800`）。有效状态格式：

```json
{"last_success_at": 1766025000, "last_attempt_at": 1766100000}
```

### 2. 查询增量文章

SQLite 文件：`data/db.db`。读取字段：`title`, `url`, `mp_id`, `publish_time`, `content`, `content_html`, `description`, `create_time`。

```sql
SELECT id, title, url, mp_id, publish_time, content, content_html, description, create_time
FROM articles WHERE create_time > ? ORDER BY create_time DESC
```

参数为 `last_success_at`（严格大于，不重复收录）。首次运行用 7 天前的时间戳。

同时查询 feeds 表获取公众号名称映射：

```sql
SELECT id, mp_name FROM feeds
```

将 `mp_id` 映射为 `mp_name`，未知公众号显示 `mp_id` 本身。

### 3. 预处理正文

对每篇文章，按优先级取正文并按需剥离 HTML：`content_html` → `content` → `description` → 仅标题。

HTML 剥离使用 Python one-liner（heredoc 传内容）：

```bash
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

取纯文本前 500 字用于分类。正文全部为空的文章标记为低置信度（输出时在标题或涉及行业中加 `†`）。

### 4. AI 分类

按以下 prompt 逐篇判断归属：

```
你是行业分类助手。将微信公众号文章分类到以下类目之一：

| 类目 | 典型主题关键词 |
|------|---------------|
| 金融 | 银行、保险、证券、基金、支付、信贷、理财、数字货币、央行 |
| 教育 | K12、高等教育、职业教育、在线教育、留学、考研、培训 |
| 汽车 | 新能源车、智能驾驶、车联网、汽车营销、出行服务 |
| 家居建材 | 家装、建材、家具、家电、地产、家居零售、智能家居 |
| AI | 大模型、AGI、AIGC、算力、GPU、深度学习、具身智能、智能体 |
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

分类的中间结果写入临时 JSONL：`data/briefings/.tmp-classification.jsonl`（每行一个 JSON 对象，使用 `jq` 安全序列化）。

对其他类目下的文章，进一步标注"涉及行业"标签，用简短的子类目标签概括文章主题，如：电商、游戏、文娱、旅游、本地生活、食品饮料、健康、宠物、B2B、广告、营销、内容创作 等。

### 5. 生成输出

输出目录 `data/briefings/`，不存在则创建。写入方式：先写 `.tmp` 临时文件，成功后 rename 到最终路径。

**文件名**：`YYYY-MM-DD-行业简报.md`

同一天多次运行时，扫描已有文件，用正则 `_(\d+)\.md$` 提取序号，取最大值 +1。无序号文件视为第 1 份。

**排序规则**：分类内按「公众号名称 → 发布时间从晚到早」排序。

**表格格式**：

六大核心行业（AI / 教育 / 家居建材 / 汽车 / 金融 / 通信）使用 4 列表格，标题右侧追加摘要列：

```
## 行业名 (N篇)
| 公众号 | 发布时间 | 标题 | 摘要 |
|--------|---------|------|------|
| 公众号A | 2026-05-19 15:30 | [文章标题](https://mp.weixin.qq.com/s/xxx) | 45-80 字高信息密度摘要，提炼正文里的新增信息或影响 |
| 公众号B | 2026-05-18 10:00 | [文章标题](https://mp.weixin.qq.com/s/yyy) | 45-80 字高信息密度摘要，避免复述标题 |
```

摘要生成规则：
- 对每篇归入六大核心行业的文章，基于正文内容（`content_html` / `content` / `description`），用一句话概括核心观点或关键信息，控制在 45-80 个中文字符
- 摘要用于帮助读者判断是否点开原文，不能复述标题；连续复用标题原文超过 12 字，或摘要与标题重合度明显超过 70%，必须重写
- 每条摘要尽量回答「主体/场景 + 动作/变化 + 价值/影响」中的至少两项，例如谁发布了什么、带来什么增长/趋势/启示
- 优先提炼新增信息、数据、结论、案例结果、方法论；不要只写「某活动/报告发布」，不要写「本文」「文章」「一图读懂」「点击查看」「速戳」等空话
- 保留关键品牌、平台、行业、数字和方法论名称；删除口号、emoji、导流语、寒暄
- 正文含多件事时，只概括最主要的一件；合集/速递类概括动态范围和共同趋势
- 正文不足但 `description` 可用时，用 `description` 提炼，不原样照搬
- 仅标题可用时，摘要写 `† 正文未取到，仅可确认主题：<从标题提炼出的主题>`，同时触发低置信度标记规则

「其他」类目使用 4 列表格，在发布时间后追加"涉及行业"列（无摘要列）：

```
## 其他 (N篇)
| 公众号 | 发布时间 | 涉及行业 | 标题 |
|--------|---------|---------|------|
| 公众号A | 2026-05-19 15:30 | 游戏 | [文章标题](https://mp.weixin.qq.com/s/xxx) |
```

**标题链接**：标题直接用 `[文章标题](原文URL)` 形式嵌入。URL 中的特殊字符可能影响 Markdown 渲染，使用标准 `[text](url)` 语法即可。

**低置信度标记**：内容为空的文章，在"涉及行业"标签后加 `†`（其他类目）或摘要列写 `† 正文未取到，仅可确认主题：...`（六大行业类目）。每个存在低置信度文章的分类在其表格下方加脚注：

```
> † 仅根据标题分类，置信度较低
```

**排序**：分类按文章数量降序排列。「其他」始终排最后。「通信」始终显示（即使 0 篇）。

**空类目**：无文章的分类不展示标题行（通信除外）。

**头部**：

```markdown
# 公众号行业简报
> 2026.05.12 - 2026.05.19 | 共 54 篇 | 上次抓取 2026.05.12 15:30
```

日期范围取自 `create_time`（本次查询结果的最小值 ~ 最大值）；上次抓取时间取自 `last_success_at`。

**空结果**（0 篇时）：

```markdown
# 公众号行业简报
> 2026.05.19 | 本期无新文章（上次抓取 2026.05.19）
```

### 6. 更新状态

写入 `data/last_fetch.json`：

- `last_success_at` = 本批文章最大 `create_time`（仅在简报文件写入成功后更新）
- `last_attempt_at` = 当前时间戳（无论成功与否都更新）

```bash
python3 -c "
import json, time
with open('data/last_fetch.json', 'r') as f:
    state = json.load(f)
# last_success_at 设为本次处理文章的最大 create_time
# last_attempt_at 设为当前时间
state['last_success_at'] = <max_create_time>
state['last_attempt_at'] = int(time.time())
with open('data/last_fetch.json', 'w') as f:
    json.dump(state, f, indent=2)
"
```

## 错误处理

| 场景 | 行为 |
|------|------|
| `last_fetch.json` 不存在 | 视为首次运行，回看 7 天 |
| `last_fetch.json` 格式损坏 | 告知用户修复或删除后重试，终止 |
| SQLite DB 无法访问 | 告知用户检查路径和权限，终止 |
| 查询结果为空（0 篇） | 输出空结果模板，更新 `last_attempt_at` |
| 正文为空 | 仅根据标题分类，标记 `†` |
| 文章 > 100 篇 | 提示用户数量较多，确认后再继续 |

## 完整示例

见 `data/briefings/2026-05-19-行业简报.md`（本次会话生成的参考输出）。
