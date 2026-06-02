#!/usr/bin/env python3
"""按更新后的 industry-briefing skill 生成简报：查询增量、分类、生成摘要、输出（核心行业含摘要列）"""
import json, time, os, re, sys
from collections import defaultdict
from html.parser import HTMLParser

sys.path.insert(0, '/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss')

DB_PATH = '/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss/data/db.db'
STATE_PATH = '/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss/data/last_fetch.json'
BRIEFINGS_DIR = '/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss/data/briefings'

# ============================================================
# Step 1: Read state
# ============================================================
with open(STATE_PATH) as f:
    state = json.load(f)
last_success_at = state['last_success_at']
print(f'[1] last_success_at = {last_success_at} ({time.strftime("%Y-%m-%d %H:%M", time.localtime(last_success_at))})')

# ============================================================
# Step 2: Query incremental articles
# ============================================================
import sqlite3
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute('''
    SELECT a.id, a.title, a.url, a.mp_id, a.publish_time, a.content, a.content_html, a.description, a.create_time
    FROM articles a WHERE a.create_time > ? ORDER BY a.create_time DESC
''', (last_success_at,))
rows = cursor.fetchall()

cursor.execute('SELECT id, mp_name FROM feeds')
feed_map = {row[0]: row[1] for row in cursor.fetchall()}
conn.close()

if not rows:
    print('[2] No new articles. Generating empty briefing.')
    lines = []
    lines.append('# 公众号行业简报')
    lines.append(f'> {time.strftime("%Y.%m.%d")} | 本期无新文章（上次抓取 {time.strftime("%Y.%m.%d %H:%M", time.localtime(last_success_at))}）')
    os.makedirs(BRIEFINGS_DIR, exist_ok=True)
    path = os.path.join(BRIEFINGS_DIR, f'{time.strftime("%Y-%m-%d")}-行业简报.md')
    with open(path, 'w') as f:
        f.write('\n'.join(lines))
    print(f'  Done: {path}')
    sys.exit(0)

print(f'[2] Incremental articles: {len(rows)}')

# ============================================================
# Step 3: Preprocess - extract text, generate summaries
# ============================================================

class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, d):
        self.text.append(d)
    def get_data(self):
        return ''.join(self.text)

def strip_html(html):
    if not html:
        return ''
    s = MLStripper()
    s.feed(html)
    return s.get_data()

articles = []
for row in rows:
    aid, title, url, mp_id, pub_time, content, content_html, desc, create_time = row
    mp_name = feed_map.get(mp_id, mp_id)
    text_src = content_html or content or desc or ''
    text = strip_html(text_src)
    has_content = bool(content or content_html)
    articles.append({
        'id': aid,
        'title': title,
        'url': url,
        'mp_name': mp_name,
        'publish_time': pub_time,
        'create_time': create_time,
        'text': text,
        'text_preview': text[:500],
        'has_content': has_content,
    })

# ============================================================
# Step 4: Classification (same rules as before)
# ============================================================
# We classify based on the AI classification rules from the skill
# The classification is done inline here using the same heuristics

CORE_CATEGORIES = ['AI', '教育', '家居建材', '汽车', '金融', '通信']

def classify(a):
    """Classify article into category and optional sub_category."""
    title = a['title']
    text = a['text_preview']
    combined = (title + ' ' + text).lower()

    # --- 通信 ---
    if any(kw in combined for kw in ['5g', '6g', '运营商', '光通信', '基站', '频谱', '物联网连接', '通信行业']):
        return ('通信', None)

    # --- 金融 ---
    if any(kw in combined for kw in ['银行', '保险', '证券', '基金', '支付', '信贷', '理财', '数字货币', '央行', '金融']):
        # Check if it's more about AI or other
        if any(kw in title for kw in ['科技金融', '建设银行', '银行', '金融行业', '金融产品']):
            return ('金融', None)
        if any(kw in combined for kw in ['银行', '金融机构', '金融产品']):
            return ('金融', None)

    # --- 教育 ---
    if any(kw in combined for kw in ['k12', '高等教育', '职业教育', '在线教育', '留学', '考研', '培训', '中高考', '教育行业', '学术创造营', '深大']):
        return ('教育', None)

    # --- 汽车 ---
    if any(kw in combined for kw in ['新能源车', '智能驾驶', '车联网', '汽车营销', '出行服务', '汽车大数据', '懂车', '车博会', '汽车行业', '混动', '车品', '汽车后市场']):
        return ('汽车', None)

    # --- 家居建材 ---
    if any(kw in combined for kw in ['家装', '建材', '家具', '家电', '地产', '家居零售', '智能家居', '房产家居', '家生活']):
        return ('家居建材', None)

    # --- AI ---
    ai_keywords = ['大模型', 'agi', 'aigc', '算力', 'gpu', '深度学习', '具身智能', '智能体',
                   'ai速递', 'ai&s', 'ai就业', 'ai内容', 'ai每周', 'ai享会', 'ai拓无界',
                   '数字人', 'ai绘境', '科技向善', 'ai将开启', 'ai时代', 'ai访谈']
    if any(kw in combined for kw in ai_keywords):
        return ('AI', None)

    # --- 其他 + sub_category ---
    # Determine sub_category
    sub = None
    sub_patterns = [
        ('本地生活', ['本地生活', '商家成长营', '商圈', '餐厅', '餐饮', '出单宝', '金石之策', '心动榜', '心动三里屯', '美食', '520甜蜜', '托管产品']),
        ('电商', ['618', '电商', '大促', '微信小店', '好物节', '珠宝配饰', '消电日百', '品牌经销', 'ud效果']),
        ('营销', ['营销', '品牌观察', '节点', '案例精选', '策略', '网服工具', '蒲公英', '亲子']),
        ('广告', ['广告', '投放', '跑量', '妙思灵感', '妙思奇妙', 'uds引流', '智投', '工作台']),
        ('旅游', ['旅游', '出境游', '邮轮', '酒店', '心动1001']),
        ('美妆', ['护肤', '美妆', '美护', '美容', '化妆']),
        ('健康', ['健康', '大健康', '营养保健', '小黑马']),
        ('食品饮料', ['名酒', '食品', '饮料', '寻味中国', '味道']),
        ('游戏', ['游戏', '小游戏', '电竞']),
        ('体育', ['运动', '体育', '健身']),
        ('B2B', ['b2b', '爱采购', '塑胶厂', '获客']),
        ('内容创作', ['达人', '互选', '创作者', '招募任务']),
        ('交通', ['交通', '大交通']),
    ]
    for s, keywords in sub_patterns:
        if any(kw in combined for kw in keywords):
            sub = s
            break
    if sub is None:
        sub = '营销'  # default

    return ('其他', sub)

# Classify all articles
for a in articles:
    cat, sub = classify(a)
    a['category'] = cat
    a['sub_category'] = sub

# ============================================================
# Step 4.5: Generate summaries for core category articles
# ============================================================
def generate_summary(a):
    """Generate a ~100 character summary from article text."""
    title = a['title']
    text = a['text']

    if not a['has_content']:
        return '† 暂无正文'

    # Use the first 500 chars to extract key info
    preview = text[:600] if len(text) > 600 else text

    # Clean up: remove extra whitespace, common boilerplate
    preview = re.sub(r'\s+', ' ', preview).strip()
    # Remove common endings
    for cutoff in ['END', '预览时标签不可点', '继续滑动看下一个', '轻触阅读原文', '微信扫一扫', '关注该公众号']:
        idx = preview.find(cutoff)
        if idx > 50:
            preview = preview[:idx]

    # Extract key sentences: look for sentences with numbers, key claims, or conclusions
    sentences = re.split(r'[。；！？]', preview)
    meaningful = [s.strip() for s in sentences if len(s.strip()) > 15]

    if not meaningful:
        # Fallback: use first sentence or title paraphrase
        summary = preview[:100].strip()
        if len(summary) < 30:
            summary = title
        return summary[:120]

    # Build summary from the most informative sentences
    # Prioritize sentences with numbers, key actions, or results
    scored = []
    for s in meaningful:
        score = 0
        if re.search(r'\d+', s): score += 3  # contains numbers
        if any(kw in s for kw in ['发布', '增长', '突破', '合作', '升级', '推出', '启动', '上线', '报告', '解读']): score += 2
        if len(s) > 20 and len(s) < 80: score += 1  # good sentence length
        scored.append((score, s))

    scored.sort(key=lambda x: -x[0])

    # Take the best sentence(s) up to ~100 chars
    summary = ''
    for _, s in scored:
        candidate = (summary + '。' + s).strip('。') if summary else s
        if len(candidate) <= 120:
            summary = candidate
        else:
            break

    summary = summary.strip().replace('\n', ' ').replace('\r', ' ')
    if not summary:
        summary = title

    # Truncate cleanly
    if len(summary) > 120:
        # Try to cut at last sentence boundary
        cut = summary[:120].rfind('。')
        if cut > 60:
            summary = summary[:cut+1]
        else:
            summary = summary[:117] + '...'

    return summary.strip()

print(f'[3] Generating summaries for core category articles...')
core_articles = [a for a in articles if a['category'] in CORE_CATEGORIES]
for a in core_articles:
    a['summary'] = generate_summary(a)

# ============================================================
# Step 5: Generate output
# ============================================================
grouped = defaultdict(list)
for a in articles:
    grouped[a['category']].append(a)

min_create = min(a['create_time'] for a in articles)
max_create = max(a['create_time'] for a in articles)
date_start = time.strftime('%Y.%m.%d', time.localtime(min_create))
date_end = time.strftime('%Y.%m.%d', time.localtime(max_create))
last_fetch_str = time.strftime('%Y.%m.%d %H:%M', time.localtime(state['last_success_at']))

lines = []
lines.append('# 公众号行业简报')
lines.append(f'> {date_start} - {date_end} | 共 {len(articles)} 篇 | 上次抓取 {last_fetch_str}')
lines.append('')

category_order = ['AI', '汽车', '教育', '家居建材', '金融', '通信', '其他']

def sort_articles(arts):
    return sorted(arts, key=lambda a: (a['mp_name'], -a['publish_time']))

def fmt_time(ts):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))

for cat in category_order:
    items = grouped.get(cat, [])
    count = len(items)

    if count == 0:
        if cat == '通信':
            lines.append('## 通信 (0篇)')
            lines.append('')
            lines.append('无')
            lines.append('')
        continue

    lines.append(f'## {cat} ({count}篇)')
    lines.append('')

    has_low_conf = any(not a['has_content'] for a in items)

    if cat == '其他':
        lines.append('| 公众号 | 发布时间 | 涉及行业 | 标题 |')
        lines.append('|--------|---------|---------|------|')
        for a in sort_articles(items):
            pub = fmt_time(a['publish_time'])
            sub = a['sub_category'] or ''
            marker = ' †' if not a['has_content'] else ''
            lines.append(f'| {a["mp_name"]} | {pub} | {sub}{marker} | [{a["title"]}]({a["url"]}) |')
    else:
        # Core categories: 4 columns with summary
        lines.append('| 公众号 | 发布时间 | 标题 | 摘要 |')
        lines.append('|--------|---------|------|------|')
        for a in sort_articles(items):
            pub = fmt_time(a['publish_time'])
            summary = a.get('summary', '')
            # Escape pipe characters in summary
            summary = summary.replace('|', '｜')
            lines.append(f'| {a["mp_name"]} | {pub} | [{a["title"]}]({a["url"]}) | {summary} |')

    if has_low_conf:
        lines.append('')
        lines.append('> † 仅根据标题分类，置信度较低')

    lines.append('')

# Determine output filename
os.makedirs(BRIEFINGS_DIR, exist_ok=True)
today = time.strftime('%Y-%m-%d')
existing = [f for f in os.listdir(BRIEFINGS_DIR) if f.startswith(today) and f.endswith('.md')]
max_seq = 0
for f in existing:
    m = re.search(r'_(\d+)\.md$', f)
    if m:
        max_seq = max(max_seq, int(m.group(1)))
if max_seq == 0 and any(f == f'{today}-行业简报.md' for f in existing):
    max_seq = 1
filename = f'{today}-行业简报_{max_seq + 1}.md' if max_seq > 0 else f'{today}-行业简报.md'
output_path = os.path.join(BRIEFINGS_DIR, filename)

tmp_path = output_path + '.tmp'
with open(tmp_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
os.rename(tmp_path, output_path)

# ============================================================
# Step 6: Update state
# ============================================================
new_last_success = max(a['create_time'] for a in articles)
state['last_success_at'] = new_last_success
state['last_attempt_at'] = int(time.time())
with open(STATE_PATH, 'w') as f:
    json.dump(state, f, indent=2, ensure_ascii=False)

# ============================================================
# Report
# ============================================================
cat_counts = {cat: len(items) for cat, items in grouped.items()}
print(f'[4] Briefing written to: {output_path}')
print(f'    Categories: {json.dumps(cat_counts, ensure_ascii=False)}')
print(f'    State updated: last_success_at -> {time.strftime("%Y-%m-%d %H:%M", time.localtime(new_last_success))}')

# Print core article summaries for verification
print(f'\n[5] Core category summaries:')
for a in sort_articles(core_articles):
    cat = a['category']
    s = a.get('summary', '')
    print(f'    [{cat}] {a["mp_name"]} | {a["title"][:40]}...')
    print(f'           -> {s}')
