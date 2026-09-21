from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from lxml import etree

from ebook_export import OPF_NS, XHTML_NS, write_epub


class EpubExportTests(unittest.TestCase):
    def test_epub_has_valid_layout_metadata_assets_and_safe_xhtml(self) -> None:
        png = b"\x89PNG\r\n\x1a\nminimal-image-bytes"
        chapters = [{
            "title": "Глава & первая",
            "html": "<p onclick='bad()'>Текст &amp; <strong>выделение</strong>.</p>"
                    "<h1 id='chapter'>Исходный заголовок</h1><h2 id='part'>Раздел</h2><table><tr><td>ячейка</td></tr></table>"
                    "<img src='images/picture.png' alt='Рисунок'><section class='footnotes'><aside id='fn-1'>Сноска</aside></section><sup><a href='#fn-1'>[1]</a></sup><script>alert(1)</script>"
                    "<iframe src='https://bad.invalid'></iframe><a href='#part'>сюда</a>",
        }]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "book.epub"
            write_epub(target, "Тест & книга", "Автор <А>", chapters, {"images/picture.png": png})
            with zipfile.ZipFile(target) as archive:
                infos = archive.infolist()
                self.assertEqual(infos[0].filename, "mimetype")
                self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
                self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
                self.assertEqual(archive.read("OEBPS/images/picture.png"), png)
                self.assertIn("OEBPS/nav.xhtml", archive.namelist())
                self.assertIn("OEBPS/styles/book.css", archive.namelist())
                self.assertIsNone(archive.testzip())

                opf = etree.fromstring(archive.read("OEBPS/content.opf"))
                ns = {"opf": OPF_NS, "dc": "http://purl.org/dc/elements/1.1/"}
                self.assertEqual(opf.get("unique-identifier"), "book-id")
                self.assertEqual(opf.xpath("string(opf:metadata/dc:language)", namespaces=ns), "ru")
                self.assertEqual(opf.xpath("string(opf:metadata/dc:title)", namespaces=ns), "Тест & книга")
                self.assertEqual(opf.xpath("count(opf:manifest/opf:item[@properties='nav'])", namespaces=ns), 1.0)
                self.assertEqual(opf.xpath("string(opf:manifest/opf:item[@href='images/picture.png']/@media-type)", namespaces=ns), "image/png")
                self.assertEqual(opf.xpath("string(opf:spine/opf:itemref/@idref)", namespaces=ns), "chapter-1")

                chapter = etree.fromstring(archive.read("OEBPS/text/chapter-001.xhtml"))
                xns = {"x": XHTML_NS}
                self.assertEqual(chapter.xpath("count(.//x:script | .//x:iframe)", namespaces=xns), 0.0)
                self.assertEqual(chapter.xpath("count(.//@onclick)", namespaces=xns), 0.0)
                self.assertEqual(chapter.xpath("string(.//x:img/@src)", namespaces=xns), "../images/picture.png")
                self.assertEqual(chapter.xpath("string(.//x:a[@href='#part']/@href)", namespaces=xns), "#part")
                self.assertEqual(chapter.xpath("string(.//x:strong)", namespaces=xns), "выделение")
                self.assertEqual(chapter.xpath("string(.//x:td)", namespaces=xns), "ячейка")
                self.assertEqual(chapter.xpath("count(.//x:h1)", namespaces=xns), 1.0)
                self.assertEqual(chapter.xpath("string(.//x:h1)", namespaces=xns), "Исходный заголовок")
                self.assertEqual(chapter.xpath("string(.//x:aside/@id)", namespaces=xns), "fn-1")

                nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
                self.assertEqual(nav.xpath("string(.//x:a/@href)", namespaces=xns), "text/chapter-001.xhtml")

    def test_rejects_traversal_assets_before_writing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad.epub"
            with self.assertRaises(ValueError):
                write_epub(target, "x", "y", [{"title": "x", "html": "<p>x</p>"}], {"../escape.png": b"x"})
            self.assertFalse(target.exists())

    def test_rejects_absolute_windows_and_empty_book_inputs(self) -> None:
        chapter = [{"title": "x", "html": "<p>x</p>"}]
        with tempfile.TemporaryDirectory() as directory:
            for bad_path in ("/cover.png", "C:\\cover.png", "images/\x00cover.png"):
                with self.subTest(bad_path=bad_path):
                    with self.assertRaises(ValueError):
                        write_epub(Path(directory) / "bad.epub", "x", "y", chapter, {bad_path: b"x"})
            with self.assertRaises(ValueError):
                write_epub(Path(directory) / "empty.epub", "x", "y", [], {})

    def test_rejects_unresolved_same_chapter_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unresolved internal anchor"):
                write_epub(Path(directory) / "bad-anchor.epub", "x", "y", [
                    {"title": "x", "html": "<p><a href='#missing'>ссылка</a></p>"}
                ], {})


if __name__ == "__main__":
    unittest.main()
