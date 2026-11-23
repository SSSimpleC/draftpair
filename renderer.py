"""Read-only Quarto manuscript rendering, with source anchors for the review UI."""
from __future__ import annotations

import copy
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from lxml import etree
from lxml import html as lhtml
from project import load_manifest


HERE = Path(__file__).resolve().parent
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.avif', '.bmp', '.tif', '.tiff'}
ALLOWED_TAGS = set("""
  p h1 h2 h3 h4 h5 h6 blockquote ol ul li dl dt dd div span a strong em b i u s
  sup sub mark small cite q abbr bdi br hr wbr pre code kbd samp var time ins del
  figure figcaption img table caption colgroup col thead tbody tfoot tr td th
  aside section details summary math semantics mrow mi mn mo mtext mspace ms mfrac
  msqrt mroot mstyle merror mpadded mphantom mfenced menclose msub msup msubsup
  munder mover munderover mmultiscripts mprescripts none mtable mtr mtd maligngroup
  malignmark maction mlabelledtr
""".split())
DROP_CONTENT = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'form', 'input', 'link', 'base', 'annotation', 'annotation-xml'}
ALLOWED_ATTRS = set("""
  id class title alt width height colspan rowspan scope start value type reversed
  lang dir role aria-label aria-hidden aria-labelledby data-cites data-chapter
  data-target display xmlns mathvariant stretchy accent accentunder fence form
  separator lspace rspace minsize maxsize movablelimits largeop symmetric
  columnalign rowalign columnspacing rowspacing linethickness notation
""".split())
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
DIV = re.compile(r'^ {0,3}(:{3,})(?:\s*(.*?))?\s*$')
HEAD = re.compile(r'^ {0,3}#{1,6}\s')
LIST = re.compile(r'^ {0,3}(?:[-+*]|\d+[.)])\s+')
LABEL = re.compile(r'^(?:fig|tbl|eq|sec)-')


def digest(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def inside(path, root):
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError('路径位于文稿项目以外') from None


def pandoc_command(prefer_quarto=False):
    """Use PATH or Quarto's native binary; the CLI also works behind a package-manager shim."""
    standalone = shutil.which('pandoc')
    if standalone and not prefer_quarto:
        return [standalone]
    quarto = shutil.which('quarto')
    if not quarto and prefer_quarto and sys.platform == 'darwin':
        local = Path.home() / 'Applications/quarto/bin/quarto'
        if local.is_file():
            quarto = str(local)
    if quarto:
        tools = Path(quarto).resolve().parent / 'tools'
        executable = 'pandoc.exe' if os.name == 'nt' else 'pandoc'
        for directory in (tools/'aarch64', tools/'x86_64', tools):
            binary = directory / executable
            if binary.is_file():
                return [str(binary)]
    if standalone:
        return [standalone]
    return [quarto, 'pandoc'] if quarto else []


def walk(value):
    if isinstance(value, dict):
        if 't' in value:
            yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def plaintext(value):
    if isinstance(value, list):
        return ''.join(plaintext(item) for item in value)
    if not isinstance(value, dict):
        return ''
    tag, content = value.get('t'), value.get('c')
    if tag in ('Space', 'SoftBreak', 'LineBreak'):
        return ' '
    if tag == 'Str':
        return content
    if tag in ('Code', 'Math'):
        return content[-1]
    if tag in ('Span', 'Cite', 'Link', 'Image'):
        return plaintext(content[1])
    return plaintext(content)


def metadata(node):
    tag, value = node['t'], node.get('c')
    if tag == 'MetaList':
        return [metadata(item) for item in value]
    if tag == 'MetaMap':
        return {k: metadata(item) for k, item in value.items()}
    if tag in ('MetaBool', 'MetaString'):
        return value
    return plaintext(value)


def chapter_paths(items):
    for item in items or []:
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            item_path = item.get('file') or item.get('href')
            if item_path:
                yield item_path
            yield from chapter_paths(item.get('chapters'))


def source_spans(source):
    """Mark only top-level boundaries. Keep fenced/chained blocks and captions together."""
    lines = source.splitlines()
    n, i = len(lines), 0
    if lines and lines[0].lstrip('\ufeff').strip() == '---':
        end = next((j for j in range(1, n) if lines[j].strip() in ('---', '...')), None)
        if end is not None:
            i = end + 1
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        start, first = i, lines[i]
        fence, div = FENCE.match(first), DIV.match(first)
        i += 1
        if fence:
            delim = fence[1]
            while i < n:
                line = lines[i].strip()
                i += 1
                if re.fullmatch(re.escape(delim[0]) + '{' + str(len(delim)) + ',}', line):
                    break
        elif div and div[2]:
            depth, code = 1, ''
            while i < n and depth:
                line = lines[i]
                i += 1
                f = FENCE.match(line)
                if code:
                    if line.strip().startswith(code) and not line.strip().strip(code[0]):
                        code = ''
                elif f:
                    code = f[1]
                else:
                    d = DIV.match(line)
                    if d:
                        depth += 1 if d[2] else -1
        elif first.lstrip().startswith('<!--'):
            while '-->' not in lines[i-1] and i < n:
                i += 1
        elif first.strip() == '$$':
            while i < n and not lines[i].lstrip().startswith('$$'):
                i += 1
            i = min(n, i + 1)
        elif HEAD.match(first):
            pass
        else:
            collect_list = bool(LIST.match(first) or first.lstrip().startswith('>') or re.match(r'^\[\^[^\]]+\]:', first))
            while i < n:
                if not lines[i].strip():
                    j = i
                    while j < n and not lines[j].strip():
                        j += 1
                    if collect_list and j < n and (LIST.match(lines[j]) or lines[j].startswith((' ', '\t', '>'))):
                        i = j + 1
                        continue
                    if j < n and (first.lstrip().startswith(('|', '+')) and re.match(r'^\s*(?::|Table:)\s+', lines[j])):
                        i = j + 1
                    break
                if HEAD.match(lines[i]) or FENCE.match(lines[i]) or DIV.match(lines[i]):
                    break
                i += 1
        yield start+1, i, '\n'.join(lines[start:i])


def align_original(rows, original, text):
    """Retain Pandoc's unmodified parse even for a spanning list or another new construct."""
    staged = [(index, block) for index, row in enumerate(rows) for block in row['ast']]
    if [block for _, block in staged] == original:
        return [row for row in rows if row['ast']]
    lines = text.splitlines()
    if not staged:
        return [dict(line_start=1, line_end=max(1,len(lines)), raw=text, ast=original)] if original else []
    def signature(block):
        return digest(json.dumps(block, ensure_ascii=False, separators=(',', ':')))
    matcher = difflib.SequenceMatcher(None, [signature(b) for _, b in staged],
                                      [signature(b) for b in original], autojunk=False)
    regions = []
    for op, left, right, first, last in matcher.get_opcodes():
        if op == 'equal':
            regions.extend((staged[i][0], staged[i][0], [b]) for i, b in zip(range(left,right), original[first:last]))
        elif first < last:
            start = staged[min(left, len(staged)-1)][0]
            end = staged[max(left, right-1) if left < len(staged) else -1][0]
            regions.append((start, end, original[first:last]))
    merged = []
    for a, b, blocks in regions:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
            merged[-1][2].extend(blocks)
        else:
            merged.append([a, b, list(blocks)])
    result = []
    for a, b, blocks in merged:
        first, last = rows[a]['line_start'], rows[b]['line_end']
        result.append(dict(line_start=first, line_end=last, raw='\n'.join(lines[first-1:last]), ast=blocks))
    return result


def remember(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


def str_node(s):
    return {'t': 'Str', 'c': s}


def attr(node):
    c = node.get('c')
    t = node.get('t')
    if t in ('Header', 'Code', 'CodeBlock', 'Span', 'Div', 'Table', 'Figure', 'Image', 'Link'):
        return c[1] if t == 'Header' else c[0]
    return None


def safe_fragment(fragment):
    for node in list(fragment.iterdescendants()):
        if not isinstance(node.tag, str):
            node.drop_tree()
            continue
        tag = node.tag.lower()
        if tag in DROP_CONTENT:
            node.drop_tree()
            continue
        if tag not in ALLOWED_TAGS:
            node.drop_tag()
            continue
        for name, value in list(node.attrib.items()):
            if name == 'href' and tag == 'a':
                if value.startswith('#') or urlsplit(value).scheme.lower() in ('https', 'http', 'mailto'):
                    continue
            if name == 'data-review-img' and tag == 'img':
                continue
            if name == 'style':
                # Pandoc emits percentages in style for image/column widths.
                if re.fullmatch(r'\s*(?:(?:width|height):\s*[\d.]+(?:%|em|px)\s*;?\s*)+', value):
                    continue
            if name not in ALLOWED_ATTRS:
                del node.attrib[name]


def fragment_content(element):
    return html.escape(element.text or '', quote=False) + ''.join(
        etree.tostring(child, method='html', encoding='unicode', with_tail=True) for child in element
    )


def freeze_image(root, data_dir, rel):
    file = root / rel
    if not file.is_file():
        raise ValueError('找不到文稿图片：' + rel)
    data = file.read_bytes()
    key = digest(data)
    directory = data_dir/'resources'
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory/(key + file.suffix.lower())
    if not dest.exists():
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
            try:
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                Path(tmp.name).unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp.name, dest)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return key


def display_rows(root, chapter, text, rendered, assets):
    blocks, headings, used = [], [], {}
    for saved in rendered:
        row = dict(saved)
        resources, checks = row.pop('images'), []
        for token, name in resources.items():
            if name not in used:
                path = root/name
                if assets is None:
                    key = digest(path.read_bytes()) if path.is_file() else 'missing'
                    url = '/asset?path='+quote(name,safe='')+'&v='+key
                else:
                    key = assets.get(name)
                    if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
                        raise ValueError('快照缺少图片：'+name)
                    url = '/resource/'+key+path.suffix.lower()
                used[name] = (key, url)
            key, url = used[name]
            checks.append(key)
            row['html'] = row['html'].replace('data-review-img="'+token+'"', 'src="'+html.escape(url, quote=True)+'"')
        if checks:
            row['fingerprint'] = digest(row['fingerprint']+''.join(checks))
        if row['level']:
            headings.append({'id':row['id'], 'level':row['level'], 'text':row['text']})
        blocks.append(row)
    result = {'blocks':blocks, 'headings':headings, 'source_hash':digest(text), 'title':chapter['title']}
    return result


class Renderer:
    def __init__(self, root: Path, data_dir: Path):
        self.root, self.data_dir = Path(root).resolve(), Path(data_dir).resolve()
        self.project = None
        manifest = load_manifest(self.root)
        if manifest is not None and manifest.get('reader') != 'quarto':
            from manuscript import Manuscript
            self.project = Manuscript(self.root, self.data_dir)
            return
        self.pandoc = pandoc_command(prefer_quarto=True)
        if not self.pandoc:
            raise ValueError('预览 Quarto 书稿需要安装 Quarto 或 Pandoc')
        self._cache, self._parsed, self._config = OrderedDict(), OrderedDict(), OrderedDict()
        self._history_chapters = OrderedDict()
        self._lock = threading.RLock()

    def _run(self, args, stdin):
        proc = subprocess.run([*self.pandoc, *map(str, args)], input=stdin, text=True, encoding='utf-8',
                              cwd=self.root, capture_output=True, timeout=90)
        if proc.returncode:
            raise ValueError('论文预览转换失败。' + proc.stderr.strip()[-1200:])
        return proc.stdout

    def _settings(self):
        contents = (self.root/'_quarto.yml').read_text(encoding='utf-8')
        key = digest(contents)
        if key not in self._config:
            node = json.loads(self._run(['-f', 'markdown', '-t', 'json'], '---\n'+contents+'\n---'))
            cfg = {k: metadata(v) for k, v in node['meta'].items()}
            remember(self._config, key, cfg, 2)
        return self._config[key]

    def is_chapter(self, name):
        if self.project is not None:
            return self.project.is_chapter(name)
        with self._lock:
            book = self._settings()['book']
            return any(name == path for key in ('chapters', 'appendices')
                       for path in chapter_paths(book.get(key)))

    def catalog(self):
        if self.project is not None:
            return self.project.catalog()
        with self._lock:
            book = self._settings()['book']
            result, number = [], 0
            for part, names in [('chapter', list(chapter_paths(book.get('chapters')))),
                                ('appendix', list(chapter_paths(book.get('appendices'))))]:
                for index, name in enumerate(names):
                    path = self.root/name
                    inside(path, self.root)
                    if path.suffix.lower() != '.qmd':
                        raise ValueError('目录中含有非 QMD 文件：'+name)
                    text = path.read_bytes().decode('utf-8')
                    first = next((lines for _, _, lines in source_spans(text.removeprefix('\ufeff'))
                                  if re.match(r'^#\s', lines)), '')
                    title = re.sub(r'\s*\{[^{}]*\}\s*$', '', re.sub(r'^#\s+', '', first)).strip() or path.stem
                    no = ''
                    if name == 'index.qmd':
                        title = '前置页'
                    elif part == 'appendix':
                        if not ('作者简历' in title or 'author-resume' in name):
                            no = chr(65+index)
                    elif name != 'references.qmd':
                        number += 1
                        no = number
                    result.append(dict(id=name, title=title, no=no))
            return result

    def _parse(self, text, filters):
        key = (digest(text), tuple((str(f), f.stat().st_mtime_ns) for f in filters))
        if key in self._parsed:
            return self._parsed[key]
        signature = key[0][:20]
        spans = list(source_spans(text))
        staged = '\n\n'.join(f'<!--mgt-review-{signature}:{i}-->\n\n{raw}'
                             for i, (_, _, raw) in enumerate(spans))
        args = ['-f', 'markdown+east_asian_line_breaks', '-t', 'json']
        for path in filters:
            args.extend(['--lua-filter', path])
        doc = json.loads(self._run(args, staged))
        rows = []
        prefix = '<!--mgt-review-'+signature+':'
        for block in doc['blocks']:
            if block['t'] == 'RawBlock' and block['c'][1].startswith(prefix):
                idx = int(block['c'][1][len(prefix):-3])
                line_start, line_end, raw = spans[idx]
                rows.append(dict(line_start=line_start, line_end=line_end, raw=raw, ast=[]))
            elif rows:
                rows[-1]['ast'].append(block)
        direct = json.loads(self._run(args, text))['blocks']
        rows = align_original(rows, direct, text)
        return remember(self._parsed, key, (doc['pandoc-api-version'], rows), 52)

    def _image_path(self, chapter, original):
        target = urlsplit(original)
        if target.scheme or target.netloc or not target.path:
            return None
        raw = unquote(target.path)
        path = self.root/raw.lstrip('/') if raw.startswith('/') else self.root/Path(chapter).parent/raw
        rel = inside(path, self.root).as_posix()
        if Path(rel).suffix.lower() not in IMAGE_EXTENSIONS:
            return None
        return rel

    def _filters(self):
        lesson = self.root/'scripts/lesson-tables.lua'
        return ([lesson] if lesson.is_file() else []) + [HERE/'review_adapter.lua']

    def snapshot_assets(self, text: str, chapter_id: str):
        if self.project is not None:
            return self.project.snapshot_assets(text, chapter_id)
        with self._lock:
            if chapter_id not in {c['id'] for c in self.catalog()}:
                raise ValueError('章节不在论文目录中')
            _, rows = self._parse(text, self._filters())
            result = {}
            for row in rows:
                for node in walk(row['ast']):
                    if node['t'] != 'Image':
                        continue
                    source = node['c'][2][0]
                    rel = self._image_path(chapter_id, source)
                    if rel is None:
                        raise ValueError('无法冻结非本地图片：' + source)
                    if rel in result:
                        continue
                    result[rel] = freeze_image(self.root, self.data_dir, rel)
            return result

    def _render_context(self, catalog, sources, filters):
        cfg = self._settings()
        bibs = cfg.get('bibliography', [])
        if isinstance(bibs, str):
            bibs = [bibs]
        csl = cfg.get('csl')
        deps = [self.root/'_quarto.yml', *filters]
        for name in [*bibs, *([csl] if csl else [])]:
            path = self.root/name
            inside(path, self.root)
            deps.append(path)
        key = (tuple((item['id'], item['no'], digest(sources[item['id']])) for item in catalog),
               tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in deps))
        return key, bibs, csl, str(cfg.get('lang', 'zh'))

    def _book(self, catalog, sources, filters, context):
        key, bibs, csl, lang = context
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        rows, labels, attr_ids, images = [], {}, {}, {}
        api_version = None
        for ch in catalog:
            name, chapter_no = ch['id'], ch['no']
            api_version, parsed = self._parse(sources[name], filters)
            counts, seen = Counter(), Counter()
            section = [0]*7
            for original in parsed:
                row = copy.deepcopy(original)
                raw = row.pop('raw')
                row['chapter'], row['fingerprint'] = name, digest(raw)
                nodes = list(walk(row['ast']))
                heading = next((v for v in nodes if v['t']=='Header'), None)
                target = next((a[0] for v in nodes if (a:=attr(v)) and a[0] and LABEL.match(a[0])), '')
                eq_id = re.search(r'\{#(eq-[\w.:-]+)[^}]*\}\s*$', raw)
                if eq_id and not target:
                    target = eq_id[1]
                token = target or 'b-'+row['fingerprint'][:18]
                seen[token] += 1
                row['id'] = token if seen[token]==1 else f'{token}-{seen[token]}'
                row['level'] = heading['c'][0] if heading else 0
                if heading:
                    row['t'] = 'heading'
                    level = heading['c'][0]
                    section[level] += 1
                    section[level+1:] = [0]*(6-level)
                else:
                    tags = {v['t'] for v in nodes}
                    row['t'] = next((label for tag,label in [('Table','table'),('Figure','figure'),('Image','figure'),
                        ('CodeBlock','code'),('BulletList','list'),('OrderedList','list'),('BlockQuote','blockquote')]
                        if tag in tags), 'paragraph')
                    if target.startswith('eq-') or any(v['t']=='Math' and v['c'][0]['t']=='DisplayMath' for v in nodes):
                        row['t'] = 'equation'
                    if any(v['t']=='Div' and v['c'][0][0]=='refs' for v in nodes):
                        row['t'] = 'bibliography'
                if target:
                    kind = target.split('-',1)[0]
                    counts[kind] += 1
                    number = (str(chapter_no)+'.' if chapter_no != '' else '')+str(counts[kind])
                    if kind == 'sec' and heading:
                        parts = ([str(chapter_no)] if chapter_no != '' else []) + [str(i) for i in section[2:row['level']+1]]
                        caption = '第' + '.'.join(parts or [str(counts[kind])]) + ('章' if row['level']==1 else '节')
                    else:
                        caption = {'fig':'图 ', 'tbl':'表 ', 'eq':'式 '}.get(kind, '')+number
                    labels[target] = (caption, name, row['id'])
                    for node in nodes:
                        a = attr(node)
                        if a and a[0] == target and node['t'] in ('Table','Figure'):
                            blocks = node['c'][1][1]
                            if blocks and blocks[0]['t'] in ('Plain','Para'):
                                blocks[0]['c'][0:0] = [str_node(caption), {'t':'Space'}]
                    if kind == 'eq':
                        for node in nodes:
                            if node['t'] == 'Str' and node.get('c') == '{#'+target+'}':
                                node['c'] = '（'+number+'）'
                for node in nodes:
                    a = attr(node)
                    if a and a[0]:
                        attr_ids.setdefault(a[0], (name, row['id']))
                    if node['t']=='Image':
                        original_src = node['c'][2][0]
                        rel = self._image_path(name, original_src)
                        image_id = 'review-img-'+digest(name+'\0'+original_src)[:32]
                        images[image_id] = (rel, original_src)
                        node['c'][2][0] = image_id
                rows.append(row)
        doc_blocks = []
        bib_row = None
        for idx, row in enumerate(rows):
            for node in walk(row['ast']):
                if node['t'] == 'Cite':
                    cites = node['c'][0]
                    if cites and all(LABEL.match(c['citationId']) for c in cites):
                        replacements = []
                        for c in cites:
                            key_ref = c['citationId']
                            if replacements:
                                replacements.append(str_node('、'))
                            if key_ref in labels:
                                label, chapter, target = labels[key_ref]
                                replacements.append({'t':'Link','c':[
                                    ['',[],[['data-chapter',chapter],['data-target',target]]],
                                    [str_node(label)], ['#'+key_ref,'']]})
                            else:
                                replacements.append(str_node('@'+key_ref))
                        node.update(t='Span', c=[['',[],[]],replacements])
            blocks = row.pop('ast')
            if row['t']=='bibliography':
                bib_row = idx
                blocks = []
            doc_blocks.append({'t':'Div', 'c':[['',['mgt-review-block'],[['data-row',str(idx)]]],blocks]})
        if bib_row is not None:
            doc_blocks.append({'t':'Div','c':[['refs',[],[]],[]]})
        doc = {'pandoc-api-version':api_version,'meta':{'lang':{'t':'MetaString','c':lang}},'blocks':doc_blocks}
        args = ['-f','json','-t','html5','--wrap=none','--mathml','--reference-location=block']
        if bibs:
            args.extend(['--citeproc','-M','link-citations=true'])
            for b in bibs:
                args.extend(['--bibliography', self.root/b])
            if csl:
                args.extend(['--csl',self.root/csl])
        source = self._run(args, json.dumps(doc, ensure_ascii=False))
        page = lhtml.fragment_fromstring(source, create_parent='div')
        views = {int(el.get('data-row')):el for el in page if el.get('data-row') is not None}
        for aside in list(page.xpath('./aside[contains(@class,"footnotes")]')):
            previous = aside.getprevious()
            if previous is not None and previous.get('data-row') is not None:
                previous.append(aside)
        if bib_row is not None:
            contents = page.xpath('./*[@id="refs"]')
            if contents:
                views[bib_row].append(contents[0])
        for idx, el in views.items():
            row = rows[idx]
            for child in el.xpath('.//*[@id]'):
                attr_ids.setdefault(child.get('id'), (row['chapter'], row['id']))
        by_chapter = {s['id']:[] for s in catalog}
        for idx, el in views.items():
            row = rows[idx]
            image_map = {}
            for img in el.xpath('.//img'):
                src = img.get('src','')
                rel, original_url = images.get(src, (None, src))
                if rel is None:
                    img.tag = 'span'
                    img.text = img.get('alt') or original_url or '图片'
                    img.set('title', '无法显示图片：'+original_url)
                    continue
                img.set('data-review-img', src)
                image_map[src] = rel
            for link in el.xpath('.//a[starts-with(@href,"#")]'):
                target = unquote(link.get('href')[1:])
                if target in attr_ids and link.get('data-target') is None:
                    name, block = attr_ids[target]
                    link.set('data-chapter', name)
                    link.set('data-target', block)
            safe_fragment(el)
            # Remove serialization indentation at the *outer* fragment boundaries.
            row['html'] = fragment_content(el).strip()
            clean = lhtml.fragment_fromstring(row['html'], create_parent='div')
            row['text'] = clean.text_content()
            by_chapter[row.pop('chapter')].append(dict(row, images=image_map))
        return remember(self._cache, key, by_chapter, 6)

    def render(self, chapter_id: str, text: str, assets: dict | None = None, context: dict | None = None):
        if self.project is not None:
            return self.project.render(chapter_id, text, assets)
        with self._lock:
            catalog = self.catalog()
            chapter = next((c for c in catalog if c['id']==chapter_id), None)
            if chapter is None:
                raise ValueError('章节不在论文目录中')
            overlays = dict(context or {})
            unknown = set(overlays).difference(c['id'] for c in catalog)
            if unknown:
                raise ValueError('候选全文包含未登记章节：'+', '.join(sorted(unknown)))
            overlays[chapter_id] = text
            if any(not isinstance(value, str) for value in overlays.values()):
                raise ValueError('全文候选必须是 QMD 文字')
            sources = {}
            for entry in catalog:
                name = entry['id']
                sources[name] = overlays[name] if name in overlays else (self.root/name).read_bytes().decode('utf-8')
            filters = self._filters()
            render_context = self._render_context(catalog, sources, filters)
            history_key = None
            if assets is not None:
                history_key = (chapter_id, render_context[0], tuple(sorted(assets.items())))
                if history_key in self._history_chapters:
                    self._history_chapters.move_to_end(history_key)
                    return copy.deepcopy(self._history_chapters[history_key])
            rendered = self._book(catalog, sources, filters, render_context)[chapter_id]
            result = display_rows(self.root, chapter, text, rendered, assets)
            if history_key is not None:
                remember(self._history_chapters, history_key, copy.deepcopy(result), 128)
            return result
