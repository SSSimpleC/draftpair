"""Read the ordered UTF-8 source files and adapter declarations of a DraftPair manuscript."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath


MANIFEST = 'draftpair.json'
FORMATS = {'.md': 'markdown', '.markdown': 'markdown', '.mdown': 'markdown',
           '.qmd': 'markdown', '.tex': 'latex', '.ltx': 'latex', '.txt': 'text'}
BUILTINS = {'markdown', 'latex', 'text'}
RESERVED = {'.git', '.draftpair', '.qmd-margin'}


def source_file(root, name):
    if not isinstance(name, str) or not name.strip() or '\x00' in name:
        raise ValueError('文稿清单中需要非空相对路径。')
    rel = Path(name)
    if (rel.is_absolute() or PureWindowsPath(name).drive or '\\' in name
            or '..' in rel.parts or name != rel.as_posix()
            or {part.casefold() for part in rel.parts} & RESERVED or name.casefold() == MANIFEST):
        raise ValueError('路径须指向项目内的文稿或适配器：' + name)
    path = root / rel
    resolved = path.resolve()
    if (not resolved.is_relative_to(root) or resolved == root / MANIFEST
            or {part.casefold() for part in resolved.relative_to(root).parts} & RESERVED):
        raise ValueError('路径超出文稿范围：' + name)
    if not path.is_file():
        raise ValueError('找不到清单指定的文件：' + name)
    return path


def guess_format(name):
    return FORMATS.get(Path(name).suffix.lower(), 'text')


def load_manifest(root):
    root = Path(root).resolve()
    path = root / MANIFEST
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f'{MANIFEST} 不是有效的 UTF-8 JSON：{e}') from e
    if not isinstance(config, dict) or type(config.get('version')) is not int or config['version'] != 1:
        raise ValueError(f'{MANIFEST} 需要 version: 1。')
    if 'reader' in config:
        if config['reader'] != 'quarto':
            raise ValueError('reader 目前仅可显式设为 quarto；使用 documents 清单时可省略 reader。')
        if config.get('documents', []) != [] or 'format' in config or config.get('adapters', {}) != {}:
            raise ValueError('reader: quarto 从 _quarto.yml 读取书稿；请在其中维护章节格式和顺序。')
        source_file(root, '_quarto.yml')
        return {**config, 'documents': [], 'adapters': {}}
    adapters = config.get('adapters', {})
    if not isinstance(adapters, dict):
        raise ValueError('adapters 须为格式名到项目内 Python 脚本的映射。')
    for name, script in adapters.items():
        if not name.strip() or name in BUILTINS or source_file(root, script).suffix != '.py':
            raise ValueError('自定义适配器需要独立的格式名和 .py 脚本：' + name)
    raw = config.get('documents')
    if not isinstance(raw, list) or not raw:
        raise ValueError('documents 须按阅读顺序列出至少一个文件。')
    documents, seen = [], set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {'path': entry}
        if not isinstance(entry, dict):
            raise ValueError('documents 的每一项应为路径或包含 path 的对象。')
        name = entry.get('path')
        absolute = source_file(root, name).resolve()
        if absolute in seen:
            raise ValueError('同一文稿重复登记：' + name)
        seen.add(absolute)
        fmt = entry.get('format', config.get('format', guess_format(name)))
        if not isinstance(fmt, str) or fmt not in BUILTINS and fmt not in adapters:
            raise ValueError(f'{name} 的格式未登记：{fmt}')
        title = entry.get('title', '')
        if not isinstance(title, str):
            raise ValueError(name + ' 的 title 需要文本。')
        documents.append({'path': name, 'format': fmt, 'title': title})
    return {**config, 'documents': documents, 'adapters': adapters}
