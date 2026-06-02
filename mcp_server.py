"""
we-mp-rss MCP Server
====================

把 we-mp-rss 的核心数据 / 操作能力暴露为 Model Context Protocol (MCP) 工具，
供 CodeFlicker / Claude Desktop / Cursor 等 MCP 客户端调用。

设计目标
--------
1. 复用现有 ``core.db`` / ``core.models`` / ``core.article_content`` 等业务逻辑，
   不重复实现一套 SQL。
2. 优先满足 ``.codeflicker/skills/industry-briefing`` 的所有数据需求：
   - 增量读取新增文章
   - 公众号 ID → 名称映射
   - HTML 正文剥离 / 摘要文本
   - 抓取状态读写（替代 ``data/last_fetch.json``）
3. 同时提供一些通用能力：搜索、统计、单文章详情、触发重新抓取。

安装依赖
--------
    pip3 install "mcp[cli]>=1.0.0"

运行方式
--------
- stdio（默认，给本机 MCP 客户端用）::

    python3 mcp_server.py

- SSE（HTTP 形态，给远端 / 容器部署用）::

    python3 mcp_server.py --transport sse --host 0.0.0.0 --port 8765

CodeFlicker / Claude Desktop 配置示例
-------------------------------------
``~/.codeflicker/mcp.json`` 或 Claude Desktop ``claude_desktop_config.json``::

    {
      "mcpServers": {
        "we-mp-rss": {
          "command": "python3",
          "args": ["/abs/path/to/we-mp-rss/mcp_server.py"],
          "env": {
            "WE_MP_RSS_CONFIG": "/abs/path/to/we-mp-rss/config.yaml"
          }
        }
      }
    }

SSE 模式则在客户端配置 URL ``http://host:8765/sse``。

AK-SK 认证（仅 SSE / streamable-http 模式）
-------------------------------------------
- 环境变量直配（推荐内网部署）::

    export WE_MP_RSS_MCP_AK="WK-skill-briefing"
    export WE_MP_RSS_MCP_SK="SK-skill-briefing-xxxxx"

- 复用项目自带 ``access_keys`` 表：在 Web 上正常 ``/api/auth/ak/create`` 出 AK/SK，
  直接拿来用即可。

- 临时关闭（仅调试）::

    export WE_MP_RSS_MCP_AUTH=disabled

客户端请求头::

    Authorization: AK-SK <ak>:<sk>

完整部署文档参见 ``docs/mcp.md``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 启动前准备：让 ``core.config`` 能在 MCP stdio 模式下不被 argparse 干扰，
# 并允许通过环境变量切换 config 文件。
# ---------------------------------------------------------------------------
if "WE_MP_RSS_CONFIG" in os.environ and "-config" not in sys.argv:
    sys.argv.extend(["-config", os.environ["WE_MP_RSS_CONFIG"]])

# 必须先 import core.config，这会触发 argparse；再 import DB 即完成数据库连接。
from core.config import cfg  # noqa: E402
from core.db import DB  # noqa: E402
from core.models import Article, Feed  # noqa: E402
from core.models.article import ArticleBase  # noqa: E402
from core.models.base import DATA_STATUS  # noqa: E402
from core.article_content import (  # noqa: E402
    build_article_url,
    sync_article_content,
)
from core.print import print_info, print_warning  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - 友好提示
    sys.stderr.write(
        "[we-mp-rss-mcp] 缺少依赖：请先执行 `pip3 install \"mcp[cli]>=1.0.0\"`\n"
    )
    raise

# ---------------------------------------------------------------------------
# 常量与小工具
# ---------------------------------------------------------------------------
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
LAST_FETCH_PATH = Path(cfg.get("mcp.last_fetch_path", "data/last_fetch.json"))


def _strip_html(html: Optional[str], max_chars: int = 800) -> str:
    """把 HTML 转换为纯文本，截断到 ``max_chars``，失败返回原字符串。"""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        text = html
    text = " ".join(text.split())
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text


def _first_image_url(html: Optional[str]) -> str:
    """从 HTML 内容里抽取第一张图片 URL，找不到返回空串。"""
    if not html or "<img" not in html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        if not img:
            return ""
        return (
            img.get("data-src")
            or img.get("src")
            or img.get("data-original")
            or ""
        ).strip()
    except Exception:
        return ""


def _serialize_article(
    article: Any,
    mp_name: str,
    *,
    include_content: bool = False,
    text_max_chars: int = 800,
) -> Dict[str, Any]:
    """把 Article ORM 对象转换成对 LLM 友好的 dict。"""
    article_url = build_article_url(article) or getattr(article, "url", "") or ""
    description = (getattr(article, "description", "") or "").strip()

    payload: Dict[str, Any] = {
        "id": getattr(article, "id", ""),
        "mp_id": getattr(article, "mp_id", ""),
        "mp_name": mp_name,
        "title": (getattr(article, "title", "") or "").strip(),
        "url": article_url,
        "pic_url": getattr(article, "pic_url", "") or "",
        "description": description,
        "publish_time": getattr(article, "publish_time", None),
        "create_time": getattr(article, "create_time", None),
        "status": getattr(article, "status", None),
        "show_type": getattr(article, "show_type", None),
        "has_content": int(getattr(article, "has_content", 0) or 0),
        "is_favorite": int(getattr(article, "is_favorite", 0) or 0),
        "is_read": int(getattr(article, "is_read", 0) or 0),
    }

    if include_content:
        content_html = getattr(article, "content_html", None) or getattr(
            article, "content", None
        )
        text = _strip_html(content_html, max_chars=text_max_chars)
        if not text:
            text = description
        payload["content_html"] = content_html or ""
        payload["text"] = text
        payload["first_image_url"] = _first_image_url(content_html)
        payload["is_image_only"] = bool(
            (not text or len(text) < 30) and payload["first_image_url"]
        )

    return payload


def _resolve_mp_names(session, mp_ids: List[str]) -> Dict[str, str]:
    """批量查询公众号 id → mp_name 映射。"""
    if not mp_ids:
        return {}
    unique = list({mp_id for mp_id in mp_ids if mp_id})
    if not unique:
        return {}
    feeds = session.query(Feed.id, Feed.mp_name).filter(Feed.id.in_(unique)).all()
    return {row.id: (row.mp_name or row.id) for row in feeds}


# ---------------------------------------------------------------------------
# MCP Server 实例
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="we-mp-rss",
    instructions=(
        "we-mp-rss 数据访问服务。你可以列出已订阅的公众号、增量拉取新文章、"
        "搜索文章、获取单篇正文、触发重新抓取，以及读写抓取状态。"
        "时间字段统一为 Unix 秒级时间戳。"
    ),
)


# ---------------------------------------------------------------------------
# 工具 1：公众号清单
# ---------------------------------------------------------------------------
@mcp.tool()
def list_feeds(only_active: bool = True) -> List[Dict[str, Any]]:
    """获取所有订阅的公众号。

    Args:
        only_active: 是否仅返回启用状态（``status == 1``）的公众号。

    Returns:
        每个元素包含 ``id`` / ``mp_name`` / ``mp_cover`` / ``mp_intro`` /
        ``status`` / ``sync_time`` / ``update_time``。
    """
    session = DB.get_session()
    try:
        query = session.query(Feed)
        if only_active:
            query = query.filter(Feed.status == 1)
        feeds = query.all()
        return [
            {
                "id": f.id,
                "mp_name": f.mp_name,
                "mp_cover": f.mp_cover,
                "mp_intro": f.mp_intro,
                "status": f.status,
                "sync_time": f.sync_time,
                "update_time": f.update_time,
                "faker_id": f.faker_id,
            }
            for f in feeds
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 2：增量拉取新文章
# ---------------------------------------------------------------------------
@mcp.tool()
def list_recent_articles(
    since_ts: int,
    until_ts: Optional[int] = None,
    mp_ids: Optional[List[str]] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    include_deleted: bool = False,
    order: str = "create_time_desc",
) -> Dict[str, Any]:
    """按 ``create_time`` 增量拉取文章，专为简报场景设计。

    Args:
        since_ts: 起始时间戳（严格大于），即 ``create_time > since_ts``。
        until_ts: 截止时间戳（包含），不传则到当前时间。
        mp_ids: 仅查询指定公众号 ID 列表，留空表示全部。
        limit: 单次最多返回多少篇，1~500，默认 100。
        offset: 分页偏移量。
        include_deleted: 是否包含已删除状态的文章。
        order: ``create_time_desc`` | ``create_time_asc`` | ``publish_time_desc``。

    Returns:
        ``{"total": N, "items": [...]}``，``items`` 元素是不含正文的轻量字段
        （减少 token 占用）。需要正文请再调用 ``get_article_content``。
    """
    limit = max(1, min(limit, MAX_LIMIT))
    session = DB.get_session()
    try:
        q = session.query(ArticleBase).filter(ArticleBase.create_time > since_ts)
        if until_ts is not None:
            q = q.filter(ArticleBase.create_time <= until_ts)
        if mp_ids:
            q = q.filter(ArticleBase.mp_id.in_(mp_ids))
        if not include_deleted:
            q = q.filter(ArticleBase.status != DATA_STATUS.DELETED)

        order_col = {
            "create_time_desc": ArticleBase.create_time.desc(),
            "create_time_asc": ArticleBase.create_time.asc(),
            "publish_time_desc": ArticleBase.publish_time.desc(),
        }.get(order, ArticleBase.create_time.desc())

        total = q.count()
        rows = q.order_by(order_col).offset(offset).limit(limit).all()

        mp_name_map = _resolve_mp_names(session, [r.mp_id for r in rows])
        items = [
            _serialize_article(r, mp_name_map.get(r.mp_id, r.mp_id or "未知公众号"))
            for r in rows
        ]
        return {"total": total, "items": items}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 3：搜索
# ---------------------------------------------------------------------------
@mcp.tool()
def search_articles(
    keyword: str,
    mp_ids: Optional[List[str]] = None,
    limit: int = 30,
    offset: int = 0,
    include_deleted: bool = False,
) -> Dict[str, Any]:
    """根据关键字在标题中模糊搜索文章。

    多个关键字可用空格 / ``|`` / ``-`` 分隔，命中任一即算匹配
    （对齐 ``apis.base.format_search_kw`` 的行为）。

    Args:
        keyword: 搜索关键字。
        mp_ids: 限定公众号。
        limit / offset: 分页参数。
        include_deleted: 是否包含已删除文章。

    Returns:
        ``{"total": N, "items": [...]}``，结构同 ``list_recent_articles``。
    """
    limit = max(1, min(limit, MAX_LIMIT))
    session = DB.get_session()
    try:
        from apis.base import format_search_kw

        q = session.query(ArticleBase)
        if keyword:
            q = q.filter(format_search_kw(keyword))
        if mp_ids:
            q = q.filter(ArticleBase.mp_id.in_(mp_ids))
        if not include_deleted:
            q = q.filter(ArticleBase.status != DATA_STATUS.DELETED)
        total = q.count()
        rows = (
            q.order_by(ArticleBase.publish_time.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        mp_name_map = _resolve_mp_names(session, [r.mp_id for r in rows])
        items = [
            _serialize_article(r, mp_name_map.get(r.mp_id, r.mp_id or "未知公众号"))
            for r in rows
        ]
        return {"total": total, "items": items}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 4：单篇详情 / 正文
# ---------------------------------------------------------------------------
@mcp.tool()
def get_article_content(
    article_id: str,
    text_max_chars: int = 2000,
    auto_fetch_if_missing: bool = False,
) -> Dict[str, Any]:
    """获取单篇文章的完整内容（含 HTML、纯文本、首图 URL）。

    Args:
        article_id: 文章主键（``feeds.id`` 拼接后的格式，如 ``MP_WXS_xxx-yyy``）。
        text_max_chars: 纯文本截断长度，默认 2000。设 0 表示不截断。
        auto_fetch_if_missing: 若本地未抓取到正文，是否在线触发抓取。

    Returns:
        文章 dict，若未找到则 ``{"error": "not_found"}``。
    """
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if article is None:
            return {"error": "not_found", "article_id": article_id}

        if auto_fetch_if_missing and not (article.content or "").strip():
            try:
                ok, mode = sync_article_content(session, article, force=False)
                if not ok:
                    # 即便失败也继续返回当前已有数据
                    pass
            except Exception as exc:  # noqa: BLE001
                # 不阻断查询，仅在返回中标注
                fetch_error = str(exc)
            else:
                fetch_error = None
        else:
            fetch_error = None

        feed = (
            session.query(Feed.mp_name).filter(Feed.id == article.mp_id).first()
            if article.mp_id
            else None
        )
        mp_name = feed.mp_name if feed and feed.mp_name else (article.mp_id or "未知公众号")

        payload = _serialize_article(
            article,
            mp_name,
            include_content=True,
            text_max_chars=text_max_chars,
        )
        if fetch_error:
            payload["fetch_error"] = fetch_error
        return payload
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 5：批量取多篇正文（简报场景常用）
# ---------------------------------------------------------------------------
@mcp.tool()
def get_articles_text_bulk(
    article_ids: List[str],
    text_max_chars: int = 500,
) -> List[Dict[str, Any]]:
    """批量获取多篇文章的标题 + 纯文本片段 + 首图 URL。

    专为「分类 / 摘要」场景设计：减少多次 MCP 往返。

    Args:
        article_ids: 文章 ID 列表，最多 50 个。
        text_max_chars: 每篇正文截断长度。

    Returns:
        与输入顺序一致的列表，每个元素含 ``id`` / ``title`` / ``mp_name`` /
        ``text`` / ``first_image_url`` / ``is_image_only`` / ``url``。
    """
    if not article_ids:
        return []
    article_ids = article_ids[:50]
    session = DB.get_session()
    try:
        rows = session.query(Article).filter(Article.id.in_(article_ids)).all()
        mp_name_map = _resolve_mp_names(session, [r.mp_id for r in rows])

        rows_by_id = {r.id: r for r in rows}
        results: List[Dict[str, Any]] = []
        for aid in article_ids:
            r = rows_by_id.get(aid)
            if r is None:
                results.append({"id": aid, "error": "not_found"})
                continue
            mp_name = mp_name_map.get(r.mp_id, r.mp_id or "未知公众号")
            payload = _serialize_article(
                r,
                mp_name,
                include_content=True,
                text_max_chars=text_max_chars,
            )
            results.append(
                {
                    "id": payload["id"],
                    "mp_id": payload["mp_id"],
                    "mp_name": payload["mp_name"],
                    "title": payload["title"],
                    "url": payload["url"],
                    "publish_time": payload["publish_time"],
                    "create_time": payload["create_time"],
                    "text": payload["text"],
                    "first_image_url": payload["first_image_url"],
                    "is_image_only": payload["is_image_only"],
                }
            )
        return results
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 6：触发重新抓取
# ---------------------------------------------------------------------------
@mcp.tool()
def refresh_article(article_id: str, force: bool = True) -> Dict[str, Any]:
    """对单篇文章触发在线重新抓取正文（同步执行）。

    Args:
        article_id: 文章主键。
        force: 是否强制重新抓取（即使已有内容）。

    Returns:
        ``{"ok": bool, "mode": "...", "article_id": "..."}``。
    """
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if article is None:
            return {"ok": False, "error": "not_found", "article_id": article_id}
        try:
            ok, mode = sync_article_content(session, article, force=force)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc),
                "article_id": article_id,
            }
        return {"ok": ok, "mode": mode, "article_id": article_id}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 7：统计
# ---------------------------------------------------------------------------
@mcp.tool()
def count_articles(
    since_ts: Optional[int] = None,
    until_ts: Optional[int] = None,
    mp_ids: Optional[List[str]] = None,
    include_deleted: bool = False,
    group_by_mp: bool = False,
) -> Dict[str, Any]:
    """统计指定时间窗口内的文章数量。

    Args:
        since_ts / until_ts: 时间窗口（基于 ``create_time``，左开右闭）。
        mp_ids: 限定公众号。
        include_deleted: 是否计入已删除。
        group_by_mp: 是否按公众号分组返回。

    Returns:
        - ``group_by_mp=False``: ``{"total": N}``。
        - ``group_by_mp=True``: ``{"total": N, "groups": [{mp_id, mp_name, count}, ...]}``。
    """
    from sqlalchemy import func

    session = DB.get_session()
    try:
        q = session.query(ArticleBase)
        if since_ts is not None:
            q = q.filter(ArticleBase.create_time > since_ts)
        if until_ts is not None:
            q = q.filter(ArticleBase.create_time <= until_ts)
        if mp_ids:
            q = q.filter(ArticleBase.mp_id.in_(mp_ids))
        if not include_deleted:
            q = q.filter(ArticleBase.status != DATA_STATUS.DELETED)
        total = q.count()

        if not group_by_mp:
            return {"total": total}

        group_q = (
            q.with_entities(ArticleBase.mp_id, func.count(ArticleBase.id).label("cnt"))
            .group_by(ArticleBase.mp_id)
            .order_by(func.count(ArticleBase.id).desc())
        )
        rows = group_q.all()
        mp_name_map = _resolve_mp_names(session, [r.mp_id for r in rows])
        groups = [
            {
                "mp_id": mp_id,
                "mp_name": mp_name_map.get(mp_id, mp_id or "未知公众号"),
                "count": cnt,
            }
            for mp_id, cnt in rows
        ]
        return {"total": total, "groups": groups}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 工具 8：抓取状态读写（替代 data/last_fetch.json 文件操作）
# ---------------------------------------------------------------------------
@dataclass
class FetchState:
    last_success_at: Optional[int]
    last_attempt_at: Optional[int]


def _read_fetch_state() -> FetchState:
    if not LAST_FETCH_PATH.exists():
        return FetchState(None, None)
    try:
        data = json.loads(LAST_FETCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return FetchState(None, None)
    return FetchState(
        last_success_at=data.get("last_success_at"),
        last_attempt_at=data.get("last_attempt_at"),
    )


def _write_fetch_state(state: FetchState) -> None:
    LAST_FETCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_FETCH_PATH.write_text(
        json.dumps(
            {
                "last_success_at": state.last_success_at,
                "last_attempt_at": state.last_attempt_at,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@mcp.tool()
def get_fetch_state() -> Dict[str, Any]:
    """读取上次抓取状态（位于 ``data/last_fetch.json``）。

    Returns:
        ``{"last_success_at": int | None, "last_attempt_at": int | None,
        "path": str, "exists": bool}``
    """
    state = _read_fetch_state()
    return {
        "last_success_at": state.last_success_at,
        "last_attempt_at": state.last_attempt_at,
        "path": str(LAST_FETCH_PATH),
        "exists": LAST_FETCH_PATH.exists(),
    }


@mcp.tool()
def update_fetch_state(
    last_success_at: Optional[int] = None,
    last_attempt_at: Optional[int] = None,
    touch_attempt: bool = True,
) -> Dict[str, Any]:
    """更新抓取状态。规则：

    - 传入 ``last_success_at`` 时直接覆盖。
    - ``last_attempt_at`` 不传且 ``touch_attempt=True`` 时，
      自动写入当前时间戳。

    Args:
        last_success_at: 本批文章最大 ``create_time``。
        last_attempt_at: 本次尝试时间戳。
        touch_attempt: 当 ``last_attempt_at`` 未提供时是否自动写入 ``now``。

    Returns:
        最新状态。
    """
    current = _read_fetch_state()
    new_success = (
        last_success_at if last_success_at is not None else current.last_success_at
    )
    if last_attempt_at is not None:
        new_attempt = last_attempt_at
    elif touch_attempt:
        new_attempt = int(time.time())
    else:
        new_attempt = current.last_attempt_at
    new_state = FetchState(last_success_at=new_success, last_attempt_at=new_attempt)
    _write_fetch_state(new_state)
    return {
        "last_success_at": new_state.last_success_at,
        "last_attempt_at": new_state.last_attempt_at,
        "path": str(LAST_FETCH_PATH),
    }


# ---------------------------------------------------------------------------
# 资源：暴露 OpenAPI 文档地址 / DB 状态等元信息
# ---------------------------------------------------------------------------
@mcp.resource("wemp://meta/server-info")
def server_info() -> str:
    """返回服务器元信息：连接的数据库路径、版本号、可用公众号数量。"""
    session = DB.get_session()
    try:
        feed_count = session.query(Feed).count()
        article_count = session.query(ArticleBase).count()
    finally:
        session.close()

    info = {
        "name": "we-mp-rss",
        "db_connection": DB.connection_str,
        "app_name": cfg.get("app_name", "WeRSS"),
        "feed_count": feed_count,
        "article_count": article_count,
        "last_fetch_path": str(LAST_FETCH_PATH),
    }
    return json.dumps(info, indent=2, ensure_ascii=False)


@mcp.resource("wemp://meta/feeds")
def feeds_resource() -> str:
    """以资源形式返回所有启用的公众号清单（JSON）。"""
    return json.dumps(list_feeds(only_active=True), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Prompt：内嵌一份「行业简报」工作流，方便客户端一键复用
# ---------------------------------------------------------------------------
@mcp.prompt()
def industry_briefing_prompt(lookback_days: int = 7) -> str:
    """生成一份「公众号行业简报」工作流的提示词模板。

    用法（在 MCP 客户端调用此 prompt 后，会注入到对话）：

        /industry_briefing_prompt lookback_days=7
    """
    return (
        "你是行业简报助手。请按以下步骤工作：\n"
        "1. 调用 get_fetch_state 获取上次抓取时间，若不存在则取 "
        f"{lookback_days} 天前的时间戳。\n"
        "2. 调用 list_recent_articles(since_ts=<上次时间>) 拿增量文章列表。\n"
        "3. 对其中需要正文的文章，调用 get_articles_text_bulk 批量取文本。\n"
        "4. 按 金融 / 教育 / 汽车 / 家居建材 / AI / 通信 / 其他 分类，"
        "并生成 ≤50 字摘要。\n"
        "5. 按分类输出 Markdown 表格（公众号 | 发布时间 | 标题 | 摘要）。\n"
        "6. 调用 update_fetch_state(last_success_at=<本批最大 create_time>) "
        "更新状态。"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _parse_cli_args() -> argparse.Namespace:
    """解析 MCP 自己的 CLI 参数，剥离掉传给 core.config 的 ``-config`` 等。"""
    parser = argparse.ArgumentParser(
        description="we-mp-rss MCP Server",
        add_help=True,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("WE_MP_RSS_MCP_TRANSPORT", "stdio"),
        help="MCP 传输方式，默认 stdio。",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("WE_MP_RSS_MCP_HOST", "127.0.0.1"),
        help="SSE / streamable-http 模式下监听地址。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WE_MP_RSS_MCP_PORT", "8765")),
        help="SSE / streamable-http 模式下监听端口。",
    )
    # 透传给 core.config 的参数，已在 sys.argv 里解析过；这里仅做 known_args。
    args, _ = parser.parse_known_args()
    return args


# ---------------------------------------------------------------------------
# AK-SK 认证中间件（仅 SSE / streamable-http 模式生效）
# ---------------------------------------------------------------------------
# 认证规则（按优先级，命中任一即放行）：
#   1. `WE_MP_RSS_MCP_AUTH=disabled` 时整体放行（仅供受信网络 / 调试）
#   2. 环境变量 `WE_MP_RSS_MCP_AK` / `WE_MP_RSS_MCP_SK` 明文匹配
#      （可用 `;` 分隔多组：`AK1;AK2`、`SK1;SK2`，按位对应）
#   3. 走 `core.auth.authenticate_ak`，复用 we-mp-rss 数据库里的 access_keys 表
#
# 请求侧：客户端在 HTTP 头里加 `Authorization: AK-SK <ak>:<sk>`
# ---------------------------------------------------------------------------


def _load_env_aksk_pairs() -> List[tuple]:
    aks = (os.environ.get("WE_MP_RSS_MCP_AK") or "").strip()
    sks = (os.environ.get("WE_MP_RSS_MCP_SK") or "").strip()
    if not aks or not sks:
        return []
    ak_list = [a for a in aks.split(";") if a]
    sk_list = [s for s in sks.split(";") if s]
    return list(zip(ak_list, sk_list))


def _verify_aksk(authorization: str) -> Optional[Dict[str, Any]]:
    """校验 ``Authorization: AK-SK ak:sk``，命中返回 principal dict。"""
    if not authorization or not authorization.startswith("AK-SK "):
        return None
    creds = authorization[6:].strip()
    if ":" not in creds:
        return None
    ak, sk = creds.split(":", 1)
    ak, sk = ak.strip(), sk.strip()
    if not ak or not sk:
        return None

    # 路径 1：环境变量明文匹配
    for env_ak, env_sk in _load_env_aksk_pairs():
        if ak == env_ak and sk == env_sk:
            return {"auth_type": "env", "ak": ak}

    # 路径 2：数据库 access_keys 校验
    try:
        from core.auth import authenticate_ak

        info = authenticate_ak(ak, sk)
        if info:
            return {
                "auth_type": "ak",
                "ak": ak,
                "user_id": info.get("user_id"),
                "username": info.get("username"),
            }
    except Exception as exc:  # noqa: BLE001
        print_warning(f"[mcp] AK-SK DB 校验异常：{exc}")

    return None


# 不需要鉴权的路径（健康检查 / OPTIONS 预检 / SSE 心跳建立时的 GET）
_PUBLIC_PATHS = {"/healthz", "/livez", "/readyz"}


class AKSKAuthMiddleware:
    """ASGI 中间件：校验 Authorization: AK-SK ak:sk。"""

    def __init__(self, app: Any, enabled: bool = True) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path in _PUBLIC_PATHS or method == "OPTIONS":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        principal = _verify_aksk(headers.get("authorization", ""))
        if principal is None:
            await _send_401(send)
            return

        # 把 principal 挂到 scope.state，工具内部可通过 ctx 拿到
        scope.setdefault("state", {})["principal"] = principal
        return await self.app(scope, receive, send)


async def _send_401(send) -> None:
    body = json.dumps(
        {
            "error": "unauthorized",
            "message": "missing or invalid `Authorization: AK-SK <ak>:<sk>` header",
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"www-authenticate", b'AK-SK realm="we-mp-rss-mcp"'),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _healthz(scope, receive, send):
    body = b'{"status":"ok"}'
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _wrap_with_health(app):
    """在原 ASGI app 前面挂一个 ``/healthz`` 简易健康检查。"""

    async def composed(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in _PUBLIC_PATHS:
            return await _healthz(scope, receive, send)
        return await app(scope, receive, send)

    return composed


def _auth_enabled() -> bool:
    mode = (os.environ.get("WE_MP_RSS_MCP_AUTH") or "enabled").strip().lower()
    return mode not in {"disabled", "off", "none", "0", "false"}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    args = _parse_cli_args()

    if args.transport == "stdio":
        # stdio 走本机进程通信，不强制鉴权
        mcp.run(transport="stdio")
        return

    # HTTP 类传输：取 FastMCP 暴露的 ASGI app，再包一层 AK-SK 中间件
    if args.transport == "sse":
        inner_app = mcp.sse_app()
    else:
        inner_app = mcp.streamable_http_app()

    auth_enabled = _auth_enabled()
    has_env_creds = bool(_load_env_aksk_pairs())
    print_info(
        f"[mcp] transport={args.transport} host={args.host} port={args.port} "
        f"auth={'on' if auth_enabled else 'off'} env_creds={has_env_creds}"
    )
    if auth_enabled and not has_env_creds:
        print_warning(
            "[mcp] 未检测到 WE_MP_RSS_MCP_AK/SK 环境变量，将仅依赖 access_keys "
            "数据库表做鉴权。若数据库中也无可用 AK，所有请求都会被拒绝。"
        )

    app = AKSKAuthMiddleware(inner_app, enabled=auth_enabled)
    app = _wrap_with_health(app)

    import uvicorn  # FastAPI/Starlette 已是项目依赖

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
