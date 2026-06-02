from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.print import print_error, print_info, print_success, print_warning


DEFAULT_OUTPUT_DIR = "./data/article_archive"
DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAX_PAGES = 20


archive_scheduler = None


def _cfg():
    from core.config import cfg

    return cfg


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def calculate_since_ts(last_success_at: int | str | None, window_days: int = DEFAULT_WINDOW_DAYS, now_ts: int | None = None) -> int:
    now_ts = int(now_ts if now_ts is not None else time.time())
    window_start = now_ts - int(window_days) * 86400
    try:
        last_success = int(last_success_at) if last_success_at not in ("", None) else 0
    except (TypeError, ValueError):
        last_success = 0
    if last_success <= 0:
        return window_start
    return min(last_success, window_start)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _parse_mp_ids(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value
    return [str(item).strip() for item in items if str(item).strip()]


def archive_settings(
    days: int | None = None,
    mp_ids: list[str] | str | None = None,
    max_pages: int | None = None,
    output_dir: str | None = None,
    download_images: bool | None = None,
) -> dict[str, Any]:
    config = _cfg()
    return {
        "window_days": int(days if days is not None else config.get("archive.window_days", DEFAULT_WINDOW_DAYS)),
        "mp_ids": _parse_mp_ids(mp_ids if mp_ids is not None else config.get("archive.mp_ids", [])),
        "max_pages": int(max_pages if max_pages is not None else config.get("archive.max_pages", DEFAULT_MAX_PAGES)),
        "output_dir": output_dir or config.get("archive.output_dir", DEFAULT_OUTPUT_DIR),
        "download_images": _as_bool(
            download_images if download_images is not None else config.get("archive.download_images", True),
            default=True,
        ),
    }


def state_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / "state.json"


def manifest_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / "manifest.json"


def load_archive_state(output_dir: str | Path) -> dict[str, Any]:
    path = state_path(output_dir)
    if not path.exists():
        return {"mps": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"mps": {}}
        data.setdefault("mps", {})
        return data
    except Exception as exc:
        print_warning(f"Failed to load archive state {path}: {exc}")
        return {"mps": {}}


def save_archive_state(output_dir: str | Path, state: dict[str, Any]) -> None:
    path = state_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def write_manifest(output_dir: str | Path, manifest: dict[str, Any]) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    with open(manifest_path(output_path), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    history_dir = output_path / "manifests"
    history_dir.mkdir(parents=True, exist_ok=True)
    run_id = manifest.get("run_id") or datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with open(history_dir / f"{run_id}.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def _get_archive_feeds(mp_ids: list[str]) -> list[dict[str, Any]]:
    from core.db import DB
    from core.models import Feed

    session = DB.get_session()
    try:
        query = session.query(Feed).filter(Feed.status == 1)
        if mp_ids:
            query = query.filter(Feed.id.in_(mp_ids))
        feeds = query.order_by(Feed.mp_name.asc()).all()
        return [
            {
                "id": feed.id,
                "mp_name": feed.mp_name,
                "faker_id": feed.faker_id,
            }
            for feed in feeds
        ]
    finally:
        session.close()


def _fetch_feed(feed: dict[str, Any], since_ts: int, max_pages: int) -> int:
    from core.wx.base import WxGather
    from jobs.article import UpdateArticle

    wx = WxGather().Model()
    wx.get_Articles(
        feed.get("faker_id"),
        Mps_id=feed.get("id"),
        Mps_title=feed.get("mp_name") or "",
        CallBack=UpdateArticle,
        MaxPage=max_pages,
        Gather_Content=False,
        since_ts=since_ts,
    )
    return wx.all_count()


def _archive_feed_articles(
    feed: dict[str, Any],
    since_ts: int,
    output_dir: str,
    download_images: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from core.article_content import sync_article_content
    from core.db import DB
    from core.models import Article
    from core.models.base import DATA_STATUS
    from tools.mdtools.archive import archive_article_to_markdown

    config = _cfg()
    session = DB.get_session()
    exported: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        articles = (
            session.query(Article)
            .filter(
                Article.mp_id == feed.get("id"),
                Article.status == DATA_STATUS.ACTIVE,
                Article.publish_time >= since_ts,
            )
            .order_by(Article.publish_time.desc(), Article.id.desc())
            .all()
        )
        for article in articles:
            try:
                if not (getattr(article, "content", "") or "").strip():
                    updated, mode = sync_article_content(session, article, preferred_mode=config.get("gather.content_mode", "web"))
                    if not (getattr(article, "content", "") or "").strip():
                        failures.append({"article_id": article.id, "title": article.title, "error": f"content fetch failed: {mode}"})
                        continue

                result = archive_article_to_markdown(
                    article,
                    output_dir=output_dir,
                    mp_title=feed.get("mp_name") or "",
                    download_images=download_images,
                )
                exported.append(
                    {
                        "article_id": article.id,
                        "title": article.title,
                        "publish_time": article.publish_time,
                        "path": result.get("markdown_path"),
                        "image_count": len(result.get("images", [])),
                        "image_failures": result.get("image_failures", []),
                    }
                )
            except Exception as exc:
                session.rollback()
                failures.append({"article_id": getattr(article, "id", ""), "title": getattr(article, "title", ""), "error": str(exc)})
        return exported, failures
    finally:
        session.close()


def run_archive_once(
    days: int | None = None,
    mp_ids: list[str] | str | None = None,
    max_pages: int | None = None,
    output_dir: str | None = None,
    download_images: bool | None = None,
) -> dict[str, Any]:
    config = _cfg()
    config.reload()
    settings = archive_settings(
        days=days,
        mp_ids=mp_ids,
        max_pages=max_pages,
        output_dir=output_dir,
        download_images=download_images,
    )
    output = settings["output_dir"]
    Path(output).mkdir(parents=True, exist_ok=True)

    state = load_archive_state(output)
    feeds = _get_archive_feeds(settings["mp_ids"])
    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "settings": settings,
        "feeds": [],
        "summary": {
            "feed_count": len(feeds),
            "fetched_count": 0,
            "exported_count": 0,
            "failure_count": 0,
        },
    }

    for feed in feeds:
        mp_id = feed["id"]
        now_ts = int(time.time())
        mp_state = state.setdefault("mps", {}).setdefault(mp_id, {})
        mp_state["last_attempt_at"] = now_ts
        since_ts = calculate_since_ts(mp_state.get("last_success_at"), settings["window_days"], now_ts=now_ts)
        feed_result: dict[str, Any] = {
            "mp_id": mp_id,
            "mp_title": feed.get("mp_name"),
            "since_ts": since_ts,
            "started_at": utc_now_iso(),
            "fetched_count": 0,
            "exported_count": 0,
            "articles": [],
            "failures": [],
        }

        try:
            print_info(f"Starting archive for {feed.get('mp_name')} since {since_ts}")
            fetched_count = _fetch_feed(feed, since_ts=since_ts, max_pages=settings["max_pages"])
            exported, failures = _archive_feed_articles(
                feed,
                since_ts=since_ts,
                output_dir=output,
                download_images=settings["download_images"],
            )

            feed_result["fetched_count"] = fetched_count
            feed_result["exported_count"] = len(exported)
            feed_result["articles"] = exported
            feed_result["failures"] = failures
            manifest["summary"]["fetched_count"] += fetched_count
            manifest["summary"]["exported_count"] += len(exported)
            manifest["summary"]["failure_count"] += len(failures)

            mp_state["last_success_at"] = int(time.time())
            mp_state["last_error"] = ""
            print_success(f"Archive finished for {feed.get('mp_name')}: {len(exported)} articles")
        except Exception as exc:
            error = str(exc)
            feed_result["failures"].append({"error": error})
            manifest["summary"]["failure_count"] += 1
            mp_state["last_error"] = error
            print_error(f"Archive failed for {feed.get('mp_name')}: {error}")
        finally:
            feed_result["finished_at"] = utc_now_iso()
            manifest["feeds"].append(feed_result)
            save_archive_state(output, state)

    manifest["finished_at"] = utc_now_iso()
    write_manifest(output, manifest)
    print_success(f"Archive manifest written: {manifest_path(output)}")
    return manifest


def enqueue_archive_once() -> None:
    from core.queue import TaskQueue

    TaskQueue.add_task(run_archive_once, task_name="article archive")


def start_archive_job() -> None:
    global archive_scheduler
    from core.task import TaskScheduler

    config = _cfg()
    if not config.get("archive.enabled", False):
        print_warning("Article archive job is disabled")
        return

    if archive_scheduler is None:
        archive_scheduler = TaskScheduler()

    cron_expr = config.get("archive.cron", "0 3 * * *")
    archive_scheduler.clear_all_jobs()
    job_id = archive_scheduler.add_cron_job(
        enqueue_archive_once,
        cron_expr=cron_expr,
        job_id="article_archive",
        tag="article archive",
    )
    archive_scheduler.start()
    print_success(f"Article archive job scheduled: {job_id} ({cron_expr})")
