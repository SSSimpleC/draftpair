"""Preview explicitly registered source files. All line numbers address the original UTF-8 text."""
from __future__ import annotations

from collections import Counter, OrderedDict
import html
import json
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit

from lxml import html as lhtml
from markdown_it import MarkdownIt

from project import RESERVED, load_manifest
from renderer import (HERE, IMAGE_EXTENSIONS, digest, display_rows, fragment_content,
                      freeze_image, inside, pandoc_command, remember, safe_fragment)


SECTION = re.compile(r'^\s*\\(chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^\]]*\]\s*)?\{')
ENV = re.compile(r'\\(begin|end)\{([^}]+)\}')
DOC_WRAPPER = re.compile(r'^\s*\\(?:begin|end)\{document\}\s*(?:%.*)?$')
TEX_LEVEL = {'chapter': 1, 'section': 2, 'subsection': 3, 'subsubsection': 4, 'paragraph': 5, 'subparagraph': 6}
ADAPTER_TIMEOUT = 30


def source_block(text):
    return '<pre class="review-source"><code>' + html.escape(text) + '</code></pre>'


def plain_block(text):
    return '<p>' + '<br>'.join(html.escape(line) for line in text.splitlines()) + '</p>'


def paragraphs(lines):
    """Half-open ranges of nonblank paragraphs."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() and start is None:
            start = i
        elif not line.strip() and start is not None:
            yield start, i
            start = None
    if start is not None:
        yield start, len(lines)


def markdown_tokens(reader, text):
    lines = text.removeprefix('\ufeff').splitlines()
    offset = 0
    if lines and lines[0].strip() == '---':
        for i, line in enumerate(lines[1:], 1):
            if line.strip() in {'---', '...'}:
                offset = i + 1
                break
    env = {}
    tokens = reader.parse('\n'.join(lines[offset:]), env)
    return tokens, env, offset


def markdown_blocks(reader, text):
    tokens, env, offset = markdown_tokens(reader, text)
    i = 0
    while i < len(tokens):
        first = tokens[i]
        stop = i + 1
        if first.nesting == 1:
            while stop < len(tokens) and tokens[stop].level > first.level:
                stop += 1
            stop += 1  # The top-level closing token.
        if first.map:
            start, end = first.map
            yield start + offset, end + offset, reader.renderer.render(tokens[i:stop], reader.options, env)
        i = stop


def tex_spans(lines):
    """Keep environments, headings and display equations intact across physical lines."""
    i = 0
    begin = next((n for n, line in enumerate(lines)
                  if DOC_WRAPPER.match(line) and '\\begin' in line), None)
    if begin is not None:
        yield 0, begin + 1, False
        i = begin + 1
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        start, depth, braces = i, 0, 0
        delimiter = (r'\]' if lines[i].strip().startswith(r'\[') else
                     '$$' if lines[i].strip().startswith('$$') else None)
        heading = SECTION.match(lines[i])
        while i < len(lines):
            line = lines[i]
            visible = re.split(r'(?<!\\)%', line, maxsplit=1)[0]
            depth += sum(1 if kind == 'begin' else -1 for kind, _ in ENV.findall(visible))
            if heading:
                braces += len(re.findall(r'(?<!\\)\{', visible)) - len(re.findall(r'(?<!\\)\}', visible))
            i += 1
            if delimiter and (delimiter in line[(2 if i == start + 1 else 0):]):
                delimiter = None
            if i == len(lines):
                break
            if depth > 0 or delimiter or braces > 0:
                continue
            if heading or DOC_WRAPPER.match(line) or not lines[i].strip() or SECTION.match(lines[i]) or DOC_WRAPPER.match(lines[i]):
                break
        yield start, i, not DOC_WRAPPER.match(lines[start])


def simple_tex(raw):
    match = SECTION.match(raw)
    if match and raw[match.end():].rstrip().endswith('}'):
        title = raw[match.end():].strip()[:-1]
        level = TEX_LEVEL[match[1]]
        return f'<h{level}>' + html.escape(title) + f'</h{level}>'
    return source_block(raw) if re.search(r'[\\$%{}]', raw) else plain_block(raw)


def latex_blocks(pandoc, text, root):
    lines = text.removeprefix('\ufeff').splitlines()
    spans = list(tex_spans(lines))
    originals = ['\n'.join(lines[a:b]) for a, b, _ in spans]
    views = [simple_tex(raw) for raw in originals]
    if pandoc and spans:
        # Native reader sandbox keeps \\input files separate. Unique raw commands survive as
        # visible code through the bundled filter and map the HTML back to the exact source ranges.
        prefix = r'\DraftPairBoundary{' + digest(text)[:20] + '-'
        names = {prefix + str(i) + '}': i for i, (_, _, body) in enumerate(spans) if body}
        marked = '\n\n'.join(name + '\n\n' + originals[i] for name, i in names.items())
        if marked:
            try:
                result = subprocess.run([*pandoc, '--sandbox', '-f', 'latex+raw_tex', '-t', 'html5',
                                         '--mathml', '--wrap=none', '--lua-filter', str(HERE / 'review_adapter.lua')],
                                        input=marked, text=True, encoding='utf-8', cwd=root, capture_output=True, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                page = lhtml.fragment_fromstring(result.stdout, create_parent='div')
                groups, index = {}, None
                for node in page:
                    key = node.text_content().strip() if node.tag == 'pre' else ''
                    if key in names:
                        index = names[key]
                        groups[index] = []
                    elif index is not None:
                        groups[index].append(lhtml.tostring(node, encoding='unicode', with_tail=False))
                if len(groups) == len(names):
                    for i, fragments in groups.items():
                        if fragments:
                            views[i] = '\n'.join(fragments)
    for (start, end, _), markup in zip(spans, views):
        yield start, end, markup


def stop_conversion(proc):
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill.exe', '/PID', str(proc.pid), '/T', '/F'], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def adapter_response(script, name, text, root):
    # File-backed stdio avoids blocked pipe readers/writers when a converter times out on Windows.
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with tempfile.TemporaryFile() as input_data, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        payload = json.dumps({'version': 1, 'path': name, 'source': text}, ensure_ascii=False)
        input_data.write(payload.encode('utf-8'))
        input_data.seek(0)
        with subprocess.Popen([sys.executable, str(script)], cwd=root, stdin=input_data, stdout=output, stderr=error,
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, **options) as proc:
            try:
                code = proc.wait(timeout=ADAPTER_TIMEOUT)
            except subprocess.TimeoutExpired:
                stop_conversion(proc)
                raise ValueError(f'执行超过 {ADAPTER_TIMEOUT} 秒') from None
        if code:
            size = error.seek(0, os.SEEK_END)
            error.seek(max(0, size - 2000))
            raise ValueError(error.read().decode('utf-8', errors='replace').strip()[-1000:] or f'退出码 {code}')
        output.seek(0)
        return json.load(output)


def adapter_blocks(script, name, text, root, markdown):
    try:
        response = adapter_response(script, name, text, root)
        if (not isinstance(response, dict) or type(response.get('version')) is not int or response['version'] != 1
                or not isinstance(response.get('blocks'), list)):
            raise ValueError('需要 version: 1 和 blocks 数组')
        lines, cursor = text.splitlines(), 0
        for item in response['blocks']:
            if not isinstance(item, dict):
                raise ValueError('blocks 中的条目须为对象')
            start, end, projection = item.get('start'), item.get('end'), item.get('markdown')
            if (type(start) is not int or type(end) is not int or not cursor < start <= end <= len(lines)
                    or not isinstance(projection, str) or not projection.strip()):
                raise ValueError('每块需含有序且不重叠的源行 start、end 和非空 markdown')
            gap = '\n'.join(lines[cursor:start-1])
            if gap.strip():
                yield cursor, start - 1, source_block(gap)
            yield start - 1, end, markdown.render(projection)
            cursor = end
        gap = '\n'.join(lines[cursor:])
        if gap.strip():
            yield cursor, len(lines), source_block(gap)
    except (ValueError, OSError) as exc:
        raise ValueError(f'{script.name} 无法预览 {name}：{exc}') from exc


def first_title(name, text, fmt, reader):
    if fmt == 'markdown':
        tokens, env, _ = markdown_tokens(reader, text)
        for index, token in enumerate(tokens[:-1]):
            if token.type == 'heading_open' and token.level == 0:
                markup = reader.renderer.renderInline(tokens[index+1].children or [], reader.options, env)
                title = lhtml.fragment_fromstring(markup, create_parent='div').text_content().strip()
                if title:
                    return title
    if fmt == 'latex':
        visible = re.sub(r'(?<!\\)%[^\n]*', '', text)
        heading = re.search(r'\\(?:title|chapter|section)\*?(?:\[[^\]]*\])?\{([^{}]+)\}', visible)
        if heading:
            return heading[1]
    return Path(name).stem


def local_links(fragment, source, documents):
    for link in fragment.xpath('.//a[@href]'):
        address = urlsplit(link.get('href'))
        if address.scheme or address.netloc or address.query or not address.path:
            continue
        path = unquote(address.path)
        target = Path(path.lstrip('/')) if path.startswith('/') else Path(source).parent / path
        # Normalize relative links such as ../chapters/next.md before matching the manifest.
        name = posixpath.normpath(target.as_posix())
        if name in documents:
            link.set('data-chapter', name)
            link.set('href', '#' + (address.fragment or 'draftpair-document-start'))


class Manuscript:
    def __init__(self, root, data_dir):
        self.root, self.data_dir = root, data_dir
        self.markdown = MarkdownIt('commonmark', {'html': False}).enable(['table', 'strikethrough'])
        self.pandoc = pandoc_command()
        self.cache, self.titles = OrderedDict(), OrderedDict()
        self.lock = threading.RLock()

    def config(self):
        config = load_manifest(self.root)
        if config is None:
            raise ValueError('找不到 draftpair.json，请核对项目目录或重新启动。')
        return config

    def is_chapter(self, name):
        return any(item['path'] == name for item in self.config()['documents'])

    def title(self, name, text, fmt):
        key = (name, fmt, digest(text))
        if key not in self.titles:
            remember(self.titles, key, first_title(name, text, fmt, self.markdown), 256)
        return self.titles[key]

    def catalog(self):
        with self.lock:
            return [dict(id=item['path'], no='', title=item['title'] or
                         self.title(item['path'], (self.root / item['path']).read_bytes().decode('utf-8'), item['format']))
                    for item in self.config()['documents']]

    def rows(self, name, text):
        config = self.config()
        doc = next((item for item in config['documents'] if item['path'] == name), None)
        if doc is None:
            raise ValueError('文稿未在 draftpair.json 中登记：' + name)
        fmt = doc['format']
        script = self.root / config['adapters'][fmt] if fmt in config['adapters'] else None
        documents = tuple(item['path'] for item in config['documents'])
        key = (name, fmt, digest(text), documents, (str(script), digest(script.read_bytes())) if script else None)
        title = doc['title'] or self.title(name, text, fmt)
        if key not in self.cache:
            lines = text.splitlines()
            if fmt == 'markdown':
                sections = markdown_blocks(self.markdown, text)
            elif fmt == 'latex':
                sections = latex_blocks(self.pandoc, text, self.root)
            elif script:
                sections = adapter_blocks(script, name, text, self.root, self.markdown)
            else:
                visible = text.removeprefix('\ufeff').splitlines()
                sections = ((a, b, plain_block('\n'.join(visible[a:b]))) for a, b in paragraphs(visible))
            seen, slugs, rows = Counter(), {'draftpair-document-start'}, []
            for start, end, markup in sections:
                raw = '\n'.join(lines[start:end])
                fragment = lhtml.fragment_fromstring(markup.strip(), create_parent='div')
                local_links(fragment, name, documents)
                images = {}
                for img in fragment.xpath('.//img'):
                    address = img.get('src', '')
                    token = 'image-' + digest(address)[:24]
                    if address:
                        images[token] = address
                    img.set('data-review-img', token)
                safe_fragment(fragment)
                header = fragment.xpath('.//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6]')
                level = int(header[0].tag[1]) if header else 0
                for el in header:
                    base = el.get('id') or re.sub(r'[^\w-]', '', el.text_content().strip().lower().replace(' ', '-')) or 'heading'
                    slug, n = base, 0
                    while slug in slugs:
                        n += 1
                        slug = f'{base}-{n}'
                    slugs.add(slug)
                    el.set('id', slug)
                if not rows:
                    fragment.insert(0, lhtml.Element('span', id='draftpair-document-start'))
                tags = {node.tag for node in fragment.iterdescendants()}
                kind = 'heading' if header else next((label for tag, label in
                        [('table','table'), ('img','figure'), ('pre','code'), ('ol','list'), ('ul','list'),
                         ('blockquote','blockquote')] if tag in tags), 'paragraph')
                if not header and fragment.xpath('.//math[@display="block"]'):
                    kind = 'equation'
                fingerprint = digest(raw)
                token = 'b-' + fingerprint[:18]
                seen[token] += 1
                rows.append(dict(id=token if seen[token] == 1 else f'{token}-{seen[token]}',
                                 line_start=start+1, line_end=end, level=level, t=kind,
                                 fingerprint=fingerprint, text=fragment.text_content(), html=fragment_content(fragment),
                                 images=images))
            remember(self.cache, key, rows, 64)
        return title, fmt, self.cache[key]

    def image_path(self, name, address, fmt, assets):
        url = urlsplit(address)
        if url.scheme or url.netloc or not url.path:
            return None
        raw = unquote(url.path)
        if PureWindowsPath(raw).drive or '\\' in raw:
            return None
        origins = [self.root] if raw.startswith('/') else [(self.root / name).parent]
        if fmt == 'latex' and self.root not in origins:
            origins.append(self.root)
        paths = [base / raw.lstrip('/') for base in origins]
        if fmt == 'latex' and not Path(raw).suffix:
            paths = [p.with_suffix(ext) for p in paths for ext in ('.png', '.jpg', '.jpeg', '.svg')]
        for path in paths:
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            try:
                relative = inside(path, self.root)
            except ValueError:
                continue
            if RESERVED.intersection(part.casefold() for part in relative.parts):
                continue
            rel = relative.as_posix()
            available = rel in assets if assets is not None else path.is_file()
            if available:
                return rel
        return None

    def image_rows(self, name, text, assets):
        title, fmt, saved = self.rows(name, text)
        rows = []
        for row in saved:
            if not row['images']:
                rows.append(row)
                continue
            fragment = lhtml.fragment_fromstring(row['html'], create_parent='div')
            images = {}
            for img in fragment.xpath('.//img'):
                token = img.get('data-review-img')
                address = row['images'].get(token, '')
                rel = self.image_path(name, address, fmt, assets)
                if rel:
                    images[token] = rel
                else:
                    label = img.get('alt') or address or '图片'
                    img.tag = 'span'
                    img.attrib.clear()
                    img.text = label
            rows.append({**row, 'images': images, 'html': fragment_content(fragment), 'text': fragment.text_content()})
        return title, rows

    def snapshot_assets(self, text, name):
        with self.lock:
            _, rows = self.image_rows(name, text, None)
            names = {rel for row in rows for rel in row['images'].values()}
            return {rel: freeze_image(self.root, self.data_dir, rel) for rel in names}

    def render(self, name, text, assets):
        with self.lock:
            title, rows = self.image_rows(name, text, assets)
            return display_rows(self.root, {'title': title}, text, rows, assets)
