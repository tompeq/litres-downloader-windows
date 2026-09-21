"""Small, offline EPUB3 writer for text already obtained by an authorised reader.

The module deliberately accepts bytes for illustrations: it never downloads a
resource or follows a URL while creating an EPUB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import mimetypes
import re
import uuid
import zipfile

from lxml import etree, html


XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
NSMAP = {None: XHTML_NS}
_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


def _safe_path(value: str) -> str:
    """Return a ZIP-safe, relative POSIX path or raise ValueError."""
    if "\x00" in value:
        raise ValueError("Asset path must not contain a NUL byte")
    path = PurePosixPath(value.replace("\\", "/"))
    if (not value or path.is_absolute() or path.parts[0].endswith(":")
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError(f"Asset path must be a relative path without '..': {value!r}")
    return path.as_posix()


def _media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname.lower() if isinstance(element.tag, str) else ""


def _asset_href(value: str, assets: set[str]) -> str | None:
    """Map an input asset reference to its chapter-relative EPUB URL."""
    if not value or "://" in value or value.startswith(("//", "data:", "javascript:")):
        return None
    clean = value.split("#", 1)[0].split("?", 1)[0]
    try:
        clean = _safe_path(clean)
    except ValueError:
        return None
    return "../" + clean if clean in assets else None


def _clean_style(style: str, assets: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        target = _asset_href(match.group(2), assets)
        return f"url('{target}')" if target else "none"

    return _URL_RE.sub(replace, style)


def _to_xhtml(node: etree._Element, assets: set[str]) -> etree._Element | None:
    tag = _local_name(node)
    if tag in {"script", "iframe"}:
        return None
    if not tag:  # Comments and processing instructions are not chapter content.
        return None

    # An unavailable remotely hosted visual must not cause a reader to fetch it.
    if tag in {"img", "image", "audio", "video", "source", "track", "object", "embed"}:
        source = node.get("src") or node.get("href") or node.get("data")
        target = _asset_href(source or "", assets)
        if source and target is None:
            replacement = etree.Element(f"{{{XHTML_NS}}}span")
            replacement.set("class", "missing-asset")
            replacement.text = node.get("alt") or "[Иллюстрация недоступна]"
            return replacement

    clean = etree.Element(f"{{{XHTML_NS}}}{tag}")
    for key, value in node.attrib.items():
        name = _local_name(etree.Element(key)) if key.startswith("{") else key.lower()
        if name.startswith("on"):
            continue
        if name == "style":
            clean.set(key, _clean_style(value, assets))
            continue
        if name in {"src", "data"} and tag in {"img", "image", "audio", "video", "source", "track", "object", "embed"}:
            target = _asset_href(value, assets)
            if target:
                clean.set(key, target)
            continue
        if name == "srcset":
            continue
        if name == "href" and tag in {"img", "image", "source", "link"}:
            target = _asset_href(value, assets)
            if target:
                clean.set(key, target)
            continue
        if name == "href" and value.strip().lower().startswith("javascript:"):
            continue
        clean.set(key, value)

    clean.text = node.text
    for child in node:
        converted = _to_xhtml(child, assets)
        if converted is not None:
            clean.append(converted)
            converted.tail = child.tail
        elif child.tail:
            if len(clean):
                clean[-1].tail = (clean[-1].tail or "") + child.tail
            else:
                clean.text = (clean.text or "") + child.tail
    return clean


def _chapter_document(title: str, fragment: str, assets: set[str]) -> bytes:
    root = etree.Element(f"{{{XHTML_NS}}}html", nsmap=NSMAP)
    root.set("lang", "ru")
    head = etree.SubElement(root, f"{{{XHTML_NS}}}head")
    etree.SubElement(head, f"{{{XHTML_NS}}}meta", charset="utf-8")
    etree.SubElement(head, f"{{{XHTML_NS}}}title").text = title
    link = etree.SubElement(head, f"{{{XHTML_NS}}}link", rel="stylesheet", type="text/css")
    link.set("href", "../styles/book.css")
    body = etree.SubElement(root, f"{{{XHTML_NS}}}body")
    try:
        fragments = html.fragments_fromstring(fragment or "")
    except (etree.ParserError, ValueError):
        fragments = [fragment or ""]
    for item in fragments:
        if isinstance(item, str):
            paragraph = etree.SubElement(body, f"{{{XHTML_NS}}}p")
            paragraph.text = item
        else:
            clean = _to_xhtml(item, assets)
            if clean is not None:
                body.append(clean)
    identifiers = {value for value in body.xpath(".//*[@id]/@id")}
    for href in body.xpath(".//x:a[starts-with(@href, '#')]/@href", namespaces={"x": XHTML_NS}):
        if href[1:] not in identifiers:
            raise ValueError(f"Chapter {title!r} contains an unresolved internal anchor: {href!r}")
    return etree.tostring(root, encoding="utf-8", xml_declaration=True, doctype="<!DOCTYPE html>")


def _xml_bytes(element: etree._Element) -> bytes:
    return etree.tostring(element, encoding="utf-8", xml_declaration=True, pretty_print=True)


def write_epub(output: Path, title: str, author: str, chapters: list[dict], assets: dict[str, bytes]) -> None:
    """Write an EPUB3 archive to *output* from chapters and already-loaded assets.

    ``chapters`` entries require ``title`` and ``html``.  Asset keys are paths
    relative to ``OEBPS`` (for example ``images/cover.jpg``); source URLs in a
    chapter may use the same paths.  Invalid paths and empty chapter lists are
    rejected before an output archive is created.
    """
    if not chapters:
        raise ValueError("At least one chapter is required")
    checked_assets: dict[str, bytes] = {}
    for name, data in assets.items():
        safe = _safe_path(name)
        if safe in checked_assets:
            raise ValueError(f"Duplicate asset path: {safe}")
        if not isinstance(data, bytes):
            raise TypeError(f"Asset {name!r} must contain bytes")
        checked_assets[safe] = data
    for chapter in chapters:
        if not isinstance(chapter, dict) or "title" not in chapter or "html" not in chapter:
            raise ValueError("Every chapter must contain title and html")

    chapter_rows = [(str(item["title"]), str(item["html"])) for item in chapters]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    asset_names = set(checked_assets)

    container = etree.Element(f"{{{CONTAINER_NS}}}container", version="1.0", nsmap={None: CONTAINER_NS})
    rootfiles = etree.SubElement(container, f"{{{CONTAINER_NS}}}rootfiles")
    etree.SubElement(rootfiles, f"{{{CONTAINER_NS}}}rootfile", **{
        "full-path": "OEBPS/content.opf", "media-type": "application/oebps-package+xml"
    })

    package = etree.Element(f"{{{OPF_NS}}}package", attrib={"version": "3.0", "unique-identifier": "book-id"},
                            nsmap={None: OPF_NS, "dc": DC_NS})
    metadata = etree.SubElement(package, f"{{{OPF_NS}}}metadata")
    etree.SubElement(metadata, f"{{{DC_NS}}}identifier", id="book-id").text = f"urn:uuid:{uuid.uuid4()}"
    etree.SubElement(metadata, f"{{{DC_NS}}}title").text = title
    etree.SubElement(metadata, f"{{{DC_NS}}}creator").text = author
    etree.SubElement(metadata, f"{{{DC_NS}}}language").text = "ru"
    etree.SubElement(metadata, f"{{{OPF_NS}}}meta", property="dcterms:modified").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = etree.SubElement(package, f"{{{OPF_NS}}}manifest")
    etree.SubElement(manifest, f"{{{OPF_NS}}}item", attrib={"id": "nav", "href": "nav.xhtml", "media-type": "application/xhtml+xml", "properties": "nav"})
    etree.SubElement(manifest, f"{{{OPF_NS}}}item", attrib={"id": "css", "href": "styles/book.css", "media-type": "text/css"})
    for index, _ in enumerate(chapter_rows, 1):
        etree.SubElement(manifest, f"{{{OPF_NS}}}item", attrib={"id": f"chapter-{index}", "href": f"text/chapter-{index:03d}.xhtml", "media-type": "application/xhtml+xml"})
    for index, name in enumerate(checked_assets, 1):
        etree.SubElement(manifest, f"{{{OPF_NS}}}item", attrib={"id": f"asset-{index}", "href": name, "media-type": _media_type(name)})
    spine = etree.SubElement(package, f"{{{OPF_NS}}}spine")
    for index, _ in enumerate(chapter_rows, 1):
        etree.SubElement(spine, f"{{{OPF_NS}}}itemref", idref=f"chapter-{index}")

    nav = etree.Element(f"{{{XHTML_NS}}}html", nsmap=NSMAP)
    nav.set("lang", "ru")
    nav_head = etree.SubElement(nav, f"{{{XHTML_NS}}}head")
    etree.SubElement(nav_head, f"{{{XHTML_NS}}}meta", charset="utf-8")
    etree.SubElement(nav_head, f"{{{XHTML_NS}}}title").text = "Содержание"
    nav_body = etree.SubElement(nav, f"{{{XHTML_NS}}}body")
    nav_node = etree.SubElement(nav_body, f"{{{XHTML_NS}}}nav", attrib={f"{{http://www.idpf.org/2007/ops}}type": "toc", "id": "toc"})
    listing = etree.SubElement(nav_node, f"{{{XHTML_NS}}}ol")
    for index, (chapter_title, _) in enumerate(chapter_rows, 1):
        li = etree.SubElement(listing, f"{{{XHTML_NS}}}li")
        etree.SubElement(li, f"{{{XHTML_NS}}}a", href=f"text/chapter-{index:03d}.xhtml").text = chapter_title

    css = """@namespace html 'http://www.w3.org/1999/xhtml';
body { font-family: serif; line-height: 1.55; margin: 5%; color: #1e1e1e; }
h1, h2, h3 { line-height: 1.2; margin-top: 1.5em; }
h1 { text-align: center; page-break-before: always; }
p { margin: 0 0 .8em; text-indent: 1.2em; }
table { border-collapse: collapse; max-width: 100%; margin: 1em auto; }
th, td { border: 1px solid #777; padding: .35em .5em; vertical-align: top; }
img, svg, video { display: block; height: auto; max-width: 100%; margin: 1em auto; }
.epigraph { margin: 1.5em 0 1.5em 18%; font-style: italic; }
.subscription { margin: .5em 18% 1.5em 0; text-align: right; }
.poem { margin: 1.2em 0; text-indent: 0; }
.stanza { margin: 0 0 1em 1.5em; text-indent: 0; }
.image-block { margin: 1.2em auto; text-align: center; }
.footnotes { border-top: 1px solid #aaa; font-size: .9em; margin-top: 2em; }
.footnotes aside { margin: .6em 0; }
.missing-asset { color: #666; font-style: italic; }
""".encode("utf-8")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", _xml_bytes(container))
        archive.writestr("OEBPS/content.opf", _xml_bytes(package))
        archive.writestr("OEBPS/nav.xhtml", etree.tostring(nav, encoding="utf-8", xml_declaration=True, doctype="<!DOCTYPE html>"))
        archive.writestr("OEBPS/styles/book.css", css)
        for index, (chapter_title, chapter_html) in enumerate(chapter_rows, 1):
            archive.writestr(f"OEBPS/text/chapter-{index:03d}.xhtml", _chapter_document(chapter_title, chapter_html, asset_names))
        for name, data in checked_assets.items():
            archive.writestr(f"OEBPS/{name}", data)
