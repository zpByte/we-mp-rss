#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _split_mp_ids(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    for value in values:
        result.extend([item.strip() for item in value.split(",") if item.strip()])
    return result


def _load_archive_module():
    module_path = os.path.join(ROOT_DIR, "jobs", "archive_articles.py")
    spec = importlib.util.spec_from_file_location("archive_articles_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load archive module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive WeChat MP articles to local Markdown files.")
    parser.add_argument("--once", action="store_true", help="Run one archive pass and exit.")
    parser.add_argument("--days", type=int, default=None, help="Lookback window in days.")
    parser.add_argument("--mp-id", action="append", default=None, help="MP id to archive. Can be repeated or comma-separated.")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum pages to fetch per MP.")
    parser.add_argument("--output-dir", default=None, help="Archive output directory.")
    parser.add_argument("--no-download-images", action="store_true", help="Keep remote image URLs instead of downloading images.")
    args = parser.parse_args()

    if not args.once:
        parser.error("Only --once is supported by this command.")

    archive_module = _load_archive_module()

    manifest = archive_module.run_archive_once(
        days=args.days,
        mp_ids=_split_mp_ids(args.mp_id),
        max_pages=args.max_pages,
        output_dir=args.output_dir,
        download_images=not args.no_download_images,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
