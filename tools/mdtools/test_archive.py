import json
import tempfile
import unittest
from pathlib import Path

from tools.mdtools.archive import (
    infer_image_extension,
    localize_html_images,
    markdown_image_urls,
    write_article_markdown_file,
)


class FakeResponse:
    def __init__(self, content=b"image", content_type="image/png"):
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "fail" in url:
            raise RuntimeError("download failed")
        return FakeResponse()


class ArticleStub:
    id = "MP_TEST-1"
    mp_id = "MP_TEST"
    title = "Test Article"
    url = "https://mp.weixin.qq.com/s/test"
    pic_url = ""
    description = "desc"
    publish_time = 1_700_000_000
    create_time = 1_700_000_000
    content = """
    <p>Hello</p>
    <img data-src="https://res.wx.qq.com/a?wx_fmt=png">
    """
    content_html = ""


class MarkdownArchiveTest(unittest.TestCase):
    def test_infer_extension_prefers_wx_fmt(self):
        self.assertEqual(infer_image_extension("https://example.com/a?wx_fmt=webp", "image/png"), "webp")
        self.assertEqual(infer_image_extension("https://example.com/a.jpeg", None), "jpg")
        self.assertEqual(infer_image_extension("https://example.com/a", "image/gif"), "gif")

    def test_localize_images_supports_data_src_dedup_and_failure(self):
        html = """
        <p>Images</p>
        <img data-src="https://res.wx.qq.com/a?wx_fmt=png">
        <img src="https://res.wx.qq.com/a?wx_fmt=png">
        <img src="https://res.wx.qq.com/fail.jpg">
        """
        fake_session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            article_dir = Path(tmp)
            localized, result = localize_html_images(
                html,
                article_dir / "assets",
                article_dir,
                session=fake_session,
            )
            self.assertEqual(len(fake_session.calls), 2)
            self.assertEqual(len(result["images"]), 1)
            self.assertEqual(len(result["failures"]), 1)
            self.assertTrue((article_dir / "assets" / "img_001.png").exists())
            self.assertEqual(localized.count("assets/img_001.png"), 2)
            self.assertIn("https://res.wx.qq.com/fail.jpg", localized)

    def test_write_article_markdown_file_writes_relative_images_and_meta(self):
        fake_session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "index.md"
            result = write_article_markdown_file(
                ArticleStub(),
                target,
                localize_images=True,
                download_images=True,
                write_meta=True,
                mp_title="MP",
                session=fake_session,
            )
            markdown = target.read_text(encoding="utf-8")
            meta = json.loads((Path(tmp) / "meta.json").read_text(encoding="utf-8"))
            self.assertIn("assets/img_001.png", markdown_image_urls(markdown))
            self.assertEqual(meta["title"], "Test Article")
            self.assertEqual(result["image_failures"], [])


if __name__ == "__main__":
    unittest.main()
