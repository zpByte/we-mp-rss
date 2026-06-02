#!/usr/bin/env python3
"""导出近一周公众号文章简报"""
import sys
import time
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, '/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss')

DB_PATH = '/Users/zhangpeng28/work/extra提效/外部信息收集/we-mp-rss/data/db.db'
OUTPUT_PATH = '/Users/zhangpeng28/work/extra提效/外部信息收集/近一周公众号资讯简报.md'

def export():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    one_week_ago = int(time.time()) - 7 * 24 * 3600

    cursor.execute('''
        SELECT a.title, f.mp_name, a.publish_time, a.url, a.description,
               a.has_content, a.content
        FROM articles a
        JOIN feeds f ON a.mp_id = f.id
        WHERE a.publish_time > ?
        ORDER BY f.mp_name, a.publish_time DESC
    ''', (one_week_ago,))

    rows = cursor.fetchall()
    conn.close()

    # 按公众号分组
    grouped = defaultdict(list)
    for row in rows:
        title, mp_name, pub_time, url, desc, has_content, content = row
        grouped[mp_name].append({
            'title': title,
            'pub_time': pub_time,
            'url': url,
            'desc': desc,
            'has_content': has_content,
            'content': content,
        })

    # 生成 Markdown
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = []
    lines.append(f'# 公众号最新资讯简报')
    lines.append(f'> 生成时间：{now_str}')
    lines.append(f'> 统计周期：{datetime.fromtimestamp(one_week_ago).strftime("%Y-%m-%d %H:%M")} ~ {now_str}')
    lines.append(f'> 共覆盖 **{len(grouped)}** 个公众号，**{len(rows)}** 篇文章')
    lines.append('')

    # 目录
    lines.append('## 目录')
    lines.append('')
    for i, mp_name in enumerate(sorted(grouped.keys()), 1):
        count = len(grouped[mp_name])
        lines.append(f'{i}. [{mp_name}](#{mp_name.replace(" ", "-")})（{count}篇）')
    lines.append('')

    # 详细内容
    for mp_name in sorted(grouped.keys()):
        articles = grouped[mp_name]
        lines.append(f'## {mp_name}')
        lines.append('')
        lines.append(f'> 近一周共 {len(articles)} 篇文章')
        lines.append('')

        for a in articles:
            pub_date = datetime.fromtimestamp(a['pub_time']).strftime('%m-%d')
            lines.append(f'### {a["title"]}')
            lines.append('')
            lines.append(f'- 发布日期：{pub_date}')
            lines.append(f'- 原文链接：{a["url"]}')
            if a['desc']:
                desc = a['desc'][:200].replace('\n', ' ')
                lines.append(f'- 摘要：{desc}')
            if a['has_content']:
                lines.append(f'- 状态：已抓取全文')
            lines.append('')

    output = '\n'.join(lines)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(output)

    print(f'简报已导出到: {OUTPUT_PATH}')
    print(f'共 {len(grouped)} 个公众号，{len(rows)} 篇文章')
    for mp_name in sorted(grouped.keys()):
        print(f'  {mp_name}: {len(grouped[mp_name])}篇')

if __name__ == '__main__':
    export()
