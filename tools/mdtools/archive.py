from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from core.common.file_tools import sanitize_filename
from tools.mdtools.html2doc import html_to_markdown


IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}


def normalize_image_url(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return url


def infer_image_extension(url: str, content_type: str | None = None) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    wx_fmt = query.get("wx_fmt", [""])[0].lower().strip(".")
    if wx_fmt in IMAGE_EXTENSIONS:
        return "jpg" if wx_fmt == "jpeg" else wx_fmt

    path = unquote(parsed.path or "")
    suffix = Path(path).suffix.lower().strip(".")
    if suffix in IMAGE_EXTENSIONS:
        return "jpg" if suffix == "jpeg" else suffix

    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_EXTENSIONS.get(media_type, "jpg")


def _article_attr(article: Any, name: str, default: Any = None) -> Any:
    if isinstance(article, dict):
        return article.get(name, default)
    return getattr(article, name, default)


def _article_timestamp(article: Any) -> int:
    for key in ("publish_time", "create_time", "updated_at"):
        value = _article_attr(article, key)
        if value in ("", None):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return int(datetime.now(tz=timezone.utc).timestamp())


def _archive_dir_name(article: Any) -> str:
    published_at = datetime.fromtimestamp(_article_timestamp(article), tz=timezone.utc)
    title = sanitize_filename(str(_article_attr(article, "title", "") or "untitled"))
    article_id = sanitize_filename(str(_article_attr(article, "id", "") or "article"))
    name = sanitize_filename(f"{published_at.strftime('%Y%m%d')}_{title}") or article_id
    return name[:160] or article_id


def article_archive_dir(article: Any, output_dir: str | Path) -> Path:
    published_at = datetime.fromtimestamp(_article_timestamp(article), tz=timezone.utc)
    mp_id = sanitize_filename(str(_article_attr(article, "mp_id", "") or "unknown_mp"))
    return Path(output_dir) / "articles" / mp_id / published_at.strftime("%Y") / _archive_dir_name(article)


def article_metadata(article: Any, mp_title: str = "") -> dict[str, Any]:
    publish_time = _article_attr(article, "publish_time")
    create_time = _article_attr(article, "create_time")
    return {
        "id": _article_attr(article, "id"),
        "mp_id": _article_attr(article, "mp_id"),
        "mp_title": mp_title,
        "title": _article_attr(article, "title"),
        "url": _article_attr(article, "url"),
        "pic_url": _article_attr(article, "pic_url"),
        "description": _article_attr(article, "description"),
        "publish_time": publish_time,
        "create_time": create_time,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _image_source(img) -> str:
    for key in ("data-src", "src", "data-original", "data-backsrc"):
        value = normalize_image_url(img.get(key))
        if value:
            return value
    return ""


def _download_image(
    url: str,
    index: int,
    assets_dir: Path,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> tuple[str, Path]:
    client = session or requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; we-mp-rss-archive/1.0)",
        "Referer": "https://mp.weixin.qq.com/",
    }
    response = client.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    ext = infer_image_extension(url, response.headers.get("content-type"))
    target = assets_dir / f"img_{index:03d}.{ext}"
    with open(target, "wb") as f:
        f.write(response.content)
    return target.name, target


def localize_html_images(
    html_content: str,
    assets_dir: str | Path,
    relative_to: str | Path,
    download_images: bool = True,
    session: requests.Session | None = None,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, list[dict[str, Any]]] = {"images": [], "failures": []}
    if not html_content:
        return "", result

    soup = BeautifulSoup(html_content, "html.parser")
    assets_path = Path(assets_dir)
    relative_root = Path(relative_to)
    if download_images:
        assets_path.mkdir(parents=True, exist_ok=True)

    seen: dict[str, str] = {}
    image_index = 1
    for img in soup.find_all("img"):
        source = _image_source(img)
        if not source:
            continue

        final_src = source
        if source in seen:
            final_src = seen[source]
        elif download_images and source.startswith(("http://", "https://")):
            try:
                _, target = _download_image(source, image_index, assets_path, session=session)
                final_src = os.path.relpath(target, relative_root).replace(os.sep, "/")
                seen[source] = final_src
                result["images"].append({"url": source, "path": final_src})
                image_index += 1
            except Exception as exc:
                result["failures"].append({"url": source, "error": str(exc)})
        else:
            seen[source] = final_src

        img["src"] = final_src
        for attr in ("data-src", "data-original", "data-backsrc"):
            if attr in img.attrs:
                del img.attrs[attr]

    return str(soup), result


def article_markdown_content(
    article: Any,
    add_title: bool = True,
    remove_links: bool = False,
    localize_images: bool = False,
    download_images: bool = True,
    article_dir: str | Path | None = None,
    session: requests.Session | None = None,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    html_content = _article_attr(article, "content") or _article_attr(article, "content_html") or ""
    image_result: dict[str, list[dict[str, Any]]] = {"images": [], "failures": []}

    if localize_images:
        if article_dir is None:
            raise ValueError("article_dir is required when localize_images=True")
        article_path = Path(article_dir)
        html_content, image_result = localize_html_images(
            html_content,
            article_path / "assets",
            article_path,
            download_images=download_images,
            session=session,
        )

    markdown = html_to_markdown(
        html_content,
        {
            "remove_images": False,
            "remove_links": remove_links,
        },
    )
    if add_title and (_article_attr(article, "title") or "").strip():
        markdown = f"# {_article_attr(article, 'title')}\n\n{markdown}"
    return markdown.strip() + "\n", image_result


def write_article_markdown_file(
    article: Any,
    markdown_path: str | Path,
    add_title: bool = True,
    remove_links: bool = False,
    localize_images: bool = False,
    download_images: bool = True,
    write_meta: bool = False,
    mp_title: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    target = Path(markdown_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    markdown, image_result = article_markdown_content(
        article,
        add_title=add_title,
        remove_links=remove_links,
        localize_images=localize_images,
        download_images=download_images,
        article_dir=target.parent,
        session=session,
    )
    with open(target, "w", encoding="utf-8") as f:
        f.write(markdown)

    meta_path = None
    if write_meta:
        meta_path = target.parent / "meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(article_metadata(article, mp_title=mp_title), f, ensure_ascii=False, indent=2)

    return {
        "markdown_path": str(target),
        "meta_path": str(meta_path) if meta_path else None,
        "images": image_result["images"],
        "image_failures": image_result["failures"],
    }


def archive_article_to_markdown(
    article: Any,
    output_dir: str | Path,
    mp_title: str = "",
    download_images: bool = True,
) -> dict[str, Any]:
    article_dir = article_archive_dir(article, output_dir)
    return write_article_markdown_file(
        article,
        article_dir / "index.md",
        add_title=True,
        remove_links=False,
        localize_images=True,
        download_images=download_images,
        write_meta=True,
        mp_title=mp_title,
    )


def markdown_image_urls(markdown: str) -> list[str]:
    return re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown or "")
