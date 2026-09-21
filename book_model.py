"""Parse the plain FB3 JSON supplied to the authorized Litres text reader."""
from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import pyjson5


def parse_json(text: str):
    # JSON5 parser, deliberately no eval of downloaded JavaScript.
    if len(text) > 12_000_000:
        raise ValueError('Reader resource exceeds the size limit')
    return pyjson5.decode(text.lstrip('\ufeff'))


def resource_name(name: str) -> str:
    parsed = urlsplit(name)
    path = PurePosixPath(name)
    if (not name or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or path.is_absolute() or '..' in path.parts or '\\' in name
            or ':' in name or '%' in name):
        raise ValueError('Unsafe reader resource path')
    return name


def walk(node):
    if isinstance(node, dict):
        yield node
        for child in node.get('c', []):
            yield from walk(child)
        if isinstance(node.get('f'), dict):
            yield from walk(node['f'])


def validate_parts(toc: dict, chunks: list[list]) -> list[dict]:
    parts = toc.get('Parts', [])
    if not parts or len(parts) != len(chunks):
        raise ValueError('Missing reader parts')
    blocks = []
    position = 0
    for part, chunk in zip(parts, chunks):
        if (part['s'] != position or part['e'] < part['s']
                or not isinstance(chunk, list) or len(chunk) != part['e'] - part['s'] + 1):
            raise ValueError('Incomplete or overlapping reader part: ' + str(part.get('url')))
        if any(not isinstance(item, dict) or 't' not in item for item in chunk):
            raise ValueError('Unexpected reader block')
        blocks.extend(chunk)
        position = part['e'] + 1
    if not toc.get('Body') or max(section['e'] for section in toc['Body']) != position - 1:
        raise ValueError('Reader data does not cover the complete table of contents')
    return blocks


def plain_text(node) -> str:
    if isinstance(node, str):
        return node.replace('\u00ad', '')
    return ''.join(plain_text(child) for child in node.get('c', []))


class Renderer:
    def __init__(self):
        self.notes = []
        self.images = set()

    def node(self, node, *, major=False):
        if isinstance(node, str):
            return escape(node.replace('\u00ad', ''))
        tag = node['t']
        xp = node.get('xp', [])
        identity = 'x-' + '-'.join(str(int(value)) for value in xp) if xp else ''
        id_attr = f' id="{identity}"' if identity else ''
        if tag == 'img':
            name = resource_name(node['s'])
            self.images.add(name)
            return f'<img{id_attr} src="images/{escape(name, quote=True)}" alt="" />'
        if tag == 'note':
            if not isinstance(node.get('f'), dict):
                raise ValueError('Footnote has no content')
            note_id = 'fn-' + identity
            self.notes.append((note_id, node['f']))
            label = ''.join(self.node(child) for child in node.get('c', []))
            return f'<sup{id_attr}><a href="#{note_id}">{label}</a></sup>'
        if tag == 'title':
            children = []
            for child in node.get('c', []):
                if isinstance(child, dict) and child.get('t') == 'p':
                    children.append(''.join(self.node(c) for c in child.get('c', [])))
                else:
                    children.append(self.node(child))
            level = 'h1' if major else 'h2'
            return f'<{level}{id_attr}>' + '<br />'.join(children) + f'</{level}>'
        children = ''.join(self.node(child) for child in node.get('c', []))
        if tag == 'br':
            return '<br />'
        if tag == 'a':
            href = node.get('href', '')
            if isinstance(href, str) and urlsplit(href).scheme in ('https', 'http', 'mailto'):
                return f'<a{id_attr} href="{escape(href, quote=True)}">{children}</a>'
            return f'<span{id_attr}>{children}</span>'
        if tag == 'subtitle':
            return f'<h3{id_attr}>{children}</h3>'
        supported = {'p','em','strong','b','i','u','s','sub','sup','table','tr','td','th','ul','ol','li','blockquote','div','span'}
        custom = {'epigraph','subscription','poem','stanza','footnote','section','cite','code','pre','v','empty-line','strikethrough'}
        if tag in supported:
            cls = ' class="image-block"' if tag == 'div' and node.get('fl') == 'center' else ''
            attrs = ''
            for key in ('colspan', 'rowspan'):
                if key in node:
                    attrs += f' {key}="{int(node[key])}"'
            return f'<{tag}{id_attr}{cls}{attrs}>{children}</{tag}>'
        if tag in custom:
            mapped = {'code':'code','pre':'pre','v':'p','strikethrough':'s'}.get(tag,'div')
            return f'<{mapped}{id_attr} class="{tag}">{children}</{mapped}>'
        raise ValueError('Unsupported reader tag: ' + str(tag))

    def footnotes(self):
        if not self.notes:
            return ''
        notes = []
        # Nested notes are allowed but bounded to catch malformed recursive data.
        index = 0
        while index < len(self.notes):
            if index > 1000:
                raise ValueError('Too many nested footnotes')
            identity, node = self.notes[index]
            notes.append(f'<aside id="{identity}">' + self.node(node) + '</aside>')
            index += 1
        return '<section class="footnotes"><h2>Примечания</h2>' + ''.join(notes) + '</section>'


def build_chapters(toc: dict, blocks: list[dict]):
    starts = {0: 'Титульная страница'}
    for body in toc['Body']:
        if len(toc['Body']) > 1 and body.get('t'):
            starts[body['s']] = body['t']
        for section in body.get('c', []):
            if section.get('t'):
                starts[section['s']] = section['t']
    order = sorted(starts)
    if any(not isinstance(i, int) or i < 0 or i >= len(blocks) for i in order):
        raise ValueError('Invalid table of contents position')
    chapters, images = [], set()
    note_count = 0
    for index, start in enumerate(order):
        end = order[index + 1] if index + 1 < len(order) else len(blocks)
        renderer = Renderer()
        body = ''.join(renderer.node(node, major=(offset == 0)) for offset, node in enumerate(blocks[start:end]))
        body += renderer.footnotes()
        chapters.append({'title': starts[start].replace('\u00ad',''), 'html': body})
        images.update(renderer.images)
        note_count += len(renderer.notes)
    metadata = toc['Meta']
    title = metadata['Title']
    if metadata.get('Subtitle'):
        title += '. ' + metadata['Subtitle']
    authors = metadata.get('Authors', [])
    author = ', '.join(' '.join(item.get(k,'') for k in ('First','Middle','Last')).replace('  ',' ').strip()
                       for item in authors if item.get('Role','author') == 'author')
    return title, author, chapters, images, note_count
