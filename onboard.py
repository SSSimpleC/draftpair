"""Discover a manuscript and write its reading order without changing any source files."""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit

from project import FORMATS, MANIFEST, guess_format, load_manifest, source_file


SKIP_DIRS = {'node_modules', 'venv', '__pycache__', 'build', 'dist', '_book', '_site', 'target'}
EDITORIAL_FILES = {'readme', 'agents', 'contributing', 'changelog', 'license', 'style_guide', 'claude'}
TEX_INCLUDE = re.compile(r'\\(?:input|include|subfile)\s*\{([^{}]+)\}')


def in_project(root, path):
    return path.resolve().is_relative_to(root)


def natural_key(name):
    # Numeric chapter prefixes keep chapter 2 ahead of chapter 10.
    return [int(token) if token.isdecimal() else token.casefold() for token in re.split(r'(\d+)', name)]


def discover(root):
    files = []
    tools_checkout = Path(__file__).resolve().parent
    for base, dirs, names in os.walk(root):
        dirs[:] = [n for n in dirs if not n.startswith('.') and n not in SKIP_DIRS
                   and not n.endswith(('.app', '_files')) and not (Path(base) / n).is_symlink()
                   and Path(base) / n != tools_checkout and not (Path(base) / n / '.git').exists()]
        for name in names:
            path = Path(base) / name
            if (not name.startswith('.') and path.suffix.lower() in FORMATS and path.is_file()
                    and in_project(root, path)):
                files.append(path.relative_to(root).as_posix())
    files.sort(key=natural_key)
    known = set(files)

    # GitBook repositories carry their intended reading order in SUMMARY.md.
    summary = root / 'SUMMARY.md'
    if 'SUMMARY.md' in known:
        targets = re.findall(r'\[[^\]]*\]\(<?([^)>]+)>?\)', summary.read_text(encoding='utf-8-sig'))
        contents = [unquote(urlsplit(target.strip()).path) for target in targets if not urlsplit(target.strip()).scheme]
        ordered = list(dict.fromkeys(Path(name).as_posix() for name in contents if name))
        ordered = [name for name in ordered if name in known and name != 'SUMMARY.md']
        if ordered:
            return ordered

    tex = [name for name in files if guess_format(name) == 'latex']
    def tex_source(name):
        return re.sub(r'(?<!\\)%[^\n]*', '', (root / name).read_text(encoding='utf-8-sig'))
    mains = [n for n in tex if re.search(r'\\documentclass\b', tex_source(n))]
    if len(mains) > 1:
        raise ValueError('找到多个 LaTeX 主文件。请按阅读顺序把要审阅的文件路径传给 init。')
    if mains:
        ordered, seen = [], set()
        def visit(name):
            if name in seen:
                return
            seen.add(name)
            ordered.append(name)
            for target in TEX_INCLUDE.findall(tex_source(name)):
                child = Path(target.strip())
                if not child.suffix:
                    child = child.with_suffix('.tex')
                if child.is_absolute():
                    continue
                for base in (root, (root / name).parent):
                    path = (base / child).resolve()
                    if path.is_relative_to(root) and path.relative_to(root).as_posix() in known:
                        visit(path.relative_to(root).as_posix())
                        break
        visit(mains[0])
        main_body = re.search(r'\\begin\{document\}(.*?)\\end\{document\}', tex_source(mains[0]), re.S)
        if len(ordered) > 1 and main_body and not TEX_INCLUDE.sub('', main_body[1]).strip():
            ordered.pop(0)  # A pure entry point orders the chapters but has no manuscript of its own.
        return ordered
    manuscripts = [n for n in files if Path(n).stem.casefold() not in EDITORIAL_FILES]
    if not manuscripts:
        manuscripts = [n for n in files if Path(n).stem.casefold() == 'readme']
    return sorted(manuscripts, key=lambda n: (Path(n).stem.casefold() != 'index', natural_key(n)))


def initialize(root, documents, dry_run=False):
    root = root.resolve(strict=True)
    destination = root / MANIFEST
    if destination.exists():
        load_manifest(root)
        if documents:
            raise ValueError('此项目已有 draftpair.json。请直接编辑其中的文稿顺序。')
        return json.loads(destination.read_text(encoding='utf-8-sig'))
    quarto = root / '_quarto.yml'
    if not documents and quarto.is_file() and re.search(r'^book\s*:', quarto.read_text(encoding='utf-8-sig'), re.M):
        return {'mode': 'quarto', 'config': '_quarto.yml'}
    names = []
    for name in documents or discover(root):
        path = source_file(root, Path(name).as_posix())
        path.read_text(encoding='utf-8-sig')
        names.append(path.relative_to(root).as_posix())
    if not names:
        raise ValueError('未找到可接入的文稿。请在 init 后列出项目内的 UTF-8 源文件。')
    if len({(root / n).resolve() for n in names}) != len(names):
        raise ValueError('同一份源文件在阅读顺序中出现了两次。')
    config = {'version': 1, 'documents': names}
    if not dry_run:
        payload = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as file:
            temporary = Path(file.name)
        try:
            temporary.write_bytes(payload)
            if os.name == 'nt':
                os.rename(temporary, destination)  # Windows rename fails when destination exists.
            else:
                try:
                    os.link(temporary, destination)
                except OSError as error:
                    if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS):
                        raise
                    with destination.open('xb') as out:  # e.g. a manuscript stored on exFAT
                        out.write(payload)
        finally:
            temporary.unlink(missing_ok=True)
    return config


def diagnose(root):
    from renderer import Renderer
    root = root.resolve(strict=True)
    manifest = load_manifest(root)
    if manifest is None and not (root / '_quarto.yml').is_file():
        raise ValueError('未找到文稿配置。请先运行 init --dry-run 检查阅读顺序。')
    formats = {d['path']: d['format'] for d in manifest['documents']} if manifest else {}
    with tempfile.TemporaryDirectory(prefix='draftpair-doctor-') as tmp:
        reader = Renderer(root, Path(tmp))
        documents = []
        for entry in reader.catalog():
            name = entry['id']
            text = (root / name).read_bytes().decode('utf-8')
            view = reader.render(name, text)
            blocks = view['blocks']
            for block in blocks:
                if not 1 <= block['line_start'] <= block['line_end'] <= len(text.splitlines()):
                    raise ValueError('预览块的行范围超出源文件：' + name)
            documents.append({'path': name, 'format': formats.get(name, 'quarto'), 'title': entry['title'],
                              'blocks': len(blocks), 'headings': len(view['headings'])})
    mode = 'manifest' if manifest and manifest.get('reader') != 'quarto' else 'quarto'
    return {'ok': True, 'mode': mode, 'documents': documents}
