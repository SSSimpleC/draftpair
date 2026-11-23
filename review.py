#!/usr/bin/env python3
"""DraftPair local review server and manuscript revision commands."""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import formal_stats
from renderer import Renderer, IMAGE_EXTENSIONS
from file_lock import file_lock
from store import Store, StoreError, atomic_write
from project import MANIFEST, RESERVED, load_manifest

PROTOCOL = 2


def default_project():
    parent = HERE.parent
    # Keep the original Dock shortcut for an existing v1 manuscript. New installations open the sample.
    if (parent / '.qmd-margin' / 'review.sqlite3').is_file() and (parent / '_quarto.yml').is_file():
        return parent
    return HERE / 'sample'


def default_data(root):
    current, legacy = root / '.draftpair', root / '.qmd-margin'
    if not (current / 'review.sqlite3').is_file() and (legacy / 'review.sqlite3').is_file():
        return legacy
    return current


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    atomic_write(path, data, preserve_mode=True)


def json_write(path, obj):
    atomic(path, json.dumps(obj, ensure_ascii=False, indent=2).encode())


def inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('路径超出项目目录')
    return path


class Application:
    def __init__(self, root, data_dir):
        self.root = root.resolve()
        self.data = data_dir.resolve()
        if self.data == self.root:
            raise ValueError('请为审阅数据指定独立目录。')
        self.data.mkdir(parents=True, exist_ok=True)
        if self.data.name in {'.draftpair', '.qmd-margin'}:
            ignore = self.data / '.gitignore'
            if not ignore.exists():
                ignore.write_text('*\n')
        self.scope = digest(os.fsencode(self.root) + b'\0' + os.fsencode(self.data))
        self.renderer = Renderer(self.root, self.data)
        self.layout = 'documents' if self.renderer.project is not None else 'quarto'
        self.store = Store(self.root, self.data, self.renderer, is_chapter=self.renderer.is_chapter)
        self.render_lock = threading.RLock()

    def book_stats(self, chapters=None):
        return formal_stats.snapshot(self, chapters=chapters)

    def refresh_stats(self, artifact=None):
        return formal_stats.snapshot(self, refresh=True, artifact=artifact)

    def state(self):
        with self.render_lock:
            stored = self.store.state()
            chapters = []
            catalog = self.renderer.catalog()
            current_sources = {entry['id']: inside(self.root, entry['id']).read_bytes() for entry in catalog}
            working_sources = {ch: raw.decode('utf-8') for ch, raw in current_sources.items()}
            for ch, active in stored.get('chapters', {}).items():
                working_sources[ch] = self.store.read_version(ch, active['versions'][-1]['id']).decode('utf-8')
            for entry in catalog:
                ch = entry['id']
                raw = current_sources[ch]
                live = self.renderer.render(ch, raw.decode('utf-8'))
                live.update(id='live:' + digest(raw), number=0)
                active = stored.get('chapters', {}).get(ch)
                versions = []
                rounds = stored.get('rounds', [])
                if isinstance(rounds, dict):
                    rounds = list(rounds.values())
                for round_info in rounds:
                    if round_info.get('ch', round_info.get('chapter')) != ch:
                        continue
                    for version in round_info.get('versions', []):
                        info = dict(version)
                        text = self.store.read_version(ch, info['id']).decode('utf-8')
                        context = working_sources if active and info['id'] == active['versions'][-1]['id'] else None
                        rendered = self.renderer.render(ch, text, self.store.version_assets(ch, info['id']), context=context)
                        info.update(rendered)
                        info['round'] = round_info['id']
                        info['archived'] = bool(round_info.get('closed'))
                        versions.append(info)
                current_round = None
                if active and not active.get('closed'):
                    av = active['versions']
                    current_round = {**active, 'base_version': av[0]['id'], 'latest_version': av[-1]['id']}
                if current_round:
                    working = next(v for v in versions if v['id'] == current_round['latest_version'])
                else:
                    working = {**self.renderer.render(ch, working_sources[ch], context=working_sources),
                               'id': live['id'], 'number': 0}
                chapters.append({**entry, 'live': live, 'working': working, 'round': current_round, 'versions': versions,
                                 'external_changed': bool(current_round and digest(raw) != active.get('base_hash'))})
            return {**stored, 'chapters': chapters, 'project': str(self.root), 'layout': self.layout,
                    'handoff': str(self.data / 'latest.md'), 'book_stats': self.book_stats(chapters)}


class Handler(BaseHTTPRequestHandler):
    server_version = 'DraftPair/2.0'
    timeout = 5

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.headers.get('Host', '').split(':', 1)[0].lower() not in {'127.0.0.1', 'localhost'}:
            self.send_error(403, 'Local requests only')
            return False
        return True

    def log_message(self, fmt, *args):
        if args and '200' not in str(args):
            super().log_message(fmt, *args)

    @property
    def app(self):
        return self.server.app

    def send(self, code, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == '/api/health':
                return self.send(200, {'root': str(self.app.root), 'data': str(self.app.data),
                                       'pid': os.getpid(), 'protocol': PROTOCOL})
            if parsed.path == '/api/state':
                return self.send(200, self.app.state())
            if parsed.path == '/api/book-stats':
                return self.send(200, self.app.book_stats())
            if parsed.path == '/api/book-stats/events':
                return formal_stats.stream(self)
            if parsed.path == '/favicon.ico':
                return self.send(204, b'', 'image/x-icon')
            if parsed.path == '/asset':
                path = inside(self.app.root, query.get('path', [''])[0])
                if path.suffix.lower() not in IMAGE_EXTENSIONS | {'.pdf'}:
                    raise ValueError('资源类型不可读取')
            elif parsed.path.startswith('/resource/'):
                name = unquote(parsed.path.split('/')[-1])
                if not re.fullmatch(r'[a-f0-9]{64}\.[a-zA-Z0-9]+', name):
                    raise ValueError('资源编号无效')
                path = inside(self.app.data / 'resources', name)
            else:
                name = unquote(parsed.path).lstrip('/') or 'index.html'
                if name not in {'index.html', 'style.css', 'app.js', 'favicon.ico'}:
                    return self.send(404, {'error': '页面不存在'})
                path = HERE / name
            if not path.is_file():
                return self.send(404, {'error': '资源不存在'})
            payload = path.read_bytes()
            if path == HERE / 'index.html':
                payload = payload.replace(b'__REVIEW_SCOPE__', self.app.scope.encode())
            self.send(200, payload, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        except (ValueError, OSError) as error:
            self.send(400, {'error': str(error)})
        except Exception as error:
            self.send(500, {'error': str(error)})

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if self.path == '/api/stop':
                token = self.headers.get('X-DraftPair-Control', '')
                if origin or not secrets.compare_digest(token, self.server.control_token):
                    return self.send(403, {'error': '本机启动器凭据无效'})
                self.send(200, {'stopping': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.send(403, {'error': '请在本机审阅页保存'})
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                return self.send(415, {'error': '请求须为 JSON'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4 * 1024 * 1024:
                raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(length))
            if self.path == '/api/book-stats/notify':
                if not formal_stats.authorized(self):
                    return self.send(403, {'error': '本机控制凭据不匹配'})
                self.server.stats_events.publish()
                return self.send(204, b'')
            if self.path == '/api/annotation':
                self.app.store.save(body)
            elif self.path == '/api/annotations':
                self.app.store.save_many(body['entries'])
            elif self.path == '/api/annotation/delete':
                self.app.store.delete(body['id'], body['expected_revision'])
            elif self.path == '/api/submit':
                self.app.store.submit(body['entries'], body['key'])
            else:
                return self.send(404, {'error': '操作不存在'})
            self.send(200, self.app.state())
        except StoreError as error:
            self.send(error.status, {'error': str(error)})
        except (ValueError, KeyError, TypeError) as error:
            self.send(400, {'error': str(error)})
        except Exception as error:
            self.send(500, {'error': str(error)})


def serve(app, port, open_browser):
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(app.data / 'server.lock', timeout=0))
        except BlockingIOError:
            info = json.loads((app.data / 'server.json').read_bytes())
            if open_browser:
                webbrowser.open(info['url'])
            print(info['url'])
            return
        preferred = port
        if preferred is None:
            preferred = 0
            try:
                previous = json.loads((app.data / 'server.json').read_bytes())
                preferred = urlparse(previous['url']).port or 0
            except (OSError, ValueError, KeyError):
                pass
        try:
            server = ThreadingHTTPServer(('127.0.0.1', preferred), Handler)
        except OSError as error:
            if port is not None or error.errno != errno.EADDRINUSE:
                raise
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.app = app
        server.control_token = secrets.token_urlsafe(32)
        server.daemon_threads = False
        server.stats_events = formal_stats.Subscribers()
        url = f'http://127.0.0.1:{server.server_port}/'
        record = {'url': url, 'pid': os.getpid(), 'root': str(app.root), 'control_token': server.control_token}
        atomic_write(app.data / 'server.json', json.dumps(record, ensure_ascii=False).encode())
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        def stop_server(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        previous = None
        if os.name != 'nt' and threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, stop_server)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.stats_events.close()
            server.server_close()
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


AWAITING_REVIEW = {'awaiting-pdf-review', 'awaiting-artifact-review'}
READY_TO_ARCHIVE = {'validated', 'pdf-reviewed', 'artifact-reviewed'}
OPEN_MERGES = {'prepared', 'written', 'validation-failed'} | AWAITING_REVIEW | READY_TO_ARCHIVE


class MergeWritten(Exception):
    """The merged source was validated and its rendered artifact is ready for inspection."""


@contextlib.contextmanager
def merge_lock(app, ch):
    name = digest(ch.encode())[:24] + '.lock'
    directory = app.data / 'merges'
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / name):
        yield


def merge_record(app, path):
    record = path.resolve(strict=True)
    if record.name != 'journal.json' or record.parent.parent != (app.data / 'merges').resolve():
        raise ValueError('合并记录路径无效')
    return record, json.loads(record.read_bytes())


def open_record(app, ch):
    owner = app.store.merge_owner(ch)
    if owner:
        return Path(owner)
    for record in (app.data / 'merges').glob('*/journal.json'):
        info = json.loads(record.read_bytes())
        if info['chapter'] == ch and info['status'] in OPEN_MERGES:
            return record
    return None


def merge(app, ch, version):
    with merge_lock(app, ch):
        previous = open_record(app, ch)
        if previous:
            raise ValueError(f'该章正在合并，请使用 merge-resume --journal "{previous}" 继续')
        info = app.store.round_info(ch)
        if not info or info.get('closed'):
            raise ValueError('这一章没有进行中的修订')
        selected = app.store.version_info(ch, version)
        if not selected.get('number') or version not in {v['id'] for v in info['versions']}:
            raise ValueError('请选择本轮候选稿')
        plan = configured_build(app.root, app.data)
        before = app.store.read_source(ch)
        run = app.data / 'merges' / (time.strftime('%Y%m%d-%H%M%S') + '-' + digest(os.urandom(16))[:8])
        run.mkdir(parents=True)
        record = (run / 'journal.json').resolve()
        base = app.store.read_version(ch, info['versions'][0]['id'])
        candidate = app.store.read_version(ch, version)
        suffix = Path(ch).suffix
        inputs = [run / (name + suffix) for name in ('before', 'base', 'candidate')]
        for path, raw in zip(inputs, (before, base, candidate)):
            atomic(path, raw)
        result = subprocess.run(['git', 'merge-file', '-p', '--diff3', *inputs], capture_output=True)
        if result.returncode < 0 or result.returncode > 127:
            raise ValueError('三方比较失败：' + result.stderr.decode(errors='replace'))
        merged_path = run / ('merged' + suffix)
        atomic(merged_path, result.stdout)
        journal = {'chapter': ch, 'version': version, 'latest': info['latest'], 'time': time.time(),
                   'build': plan,
                   'status': 'conflict' if result.returncode else 'prepared',
                   'before_hash': digest(before), 'merged_hash': digest(result.stdout)}
        json_write(record, journal)
        if result.returncode:
            raise ValueError(f'同一处文字发生冲突，请查看 {merged_path}')
        continue_merge(app, record, journal)
        if journal['status'] == 'complete':
            return record
        raise MergeWritten(record)


def external_change(app, record, journal):
    journal['status'] = 'external-change'
    json_write(record, journal)
    app.store.release_merge(journal['chapter'], str(record))
    raise ValueError(f'正式正文已有新的改动，已保留现有文件及合并前后副本。核对 {record.parent} 后可重新三方合并。')


def continue_merge(app, record, journal):
    ch = journal['chapter']
    if journal['status'] in {'external-change', 'superseded'}:
        app.store.release_merge(ch, str(record))
        raise ValueError('该合并记录已被后续改动取代，可以重新合并。')
    if journal['status'] in READY_TO_ARCHIVE:
        finish_merge(app, record, journal)
        return
    if journal['status'] not in OPEN_MERGES:
        raise ValueError('该记录没有需要恢复的写入或检查')
    if journal['status'] in AWAITING_REVIEW:
        # The archived output can be reviewed even after another document has built.
        saved = Path(journal.get('review_artifact') or journal['review_pdf'])
        expected = journal.get('artifact_hash') or journal['build_pdf_hash']
        if saved.is_file() and digest(saved.read_bytes()) == expected:
            if digest(inside(app.root, ch).read_bytes()) != journal['merged_hash']:
                external_change(app, record, journal)
            return
    if journal['status'] == 'validation-failed' and journal.get('build') is not None:
        # A failed custom command or its output path can be corrected in the manifest.
        plan = configured_build(app.root, app.data)
        if plan is None:
            raise ValueError('恢复本次合并需要原项目的 draftpair.json')
        journal['build'] = plan
    try:
        app.store.reserve_merge(ch, journal['latest'], journal['version'], str(record))
    except StoreError as error:
        if journal['status'] == 'prepared' and app.store.merge_owner(ch) != str(record):
            journal['status'] = 'superseded'
            json_write(record, journal)
        raise
    # The write, shared build and its snapshot must describe the same book revision.
    with file_lock(app.data / 'merges' / 'build.lock'):
        write_merged_source(app, record, journal)
        validate_merge(app, record, journal)


def write_merged_source(app, record, journal):
    source = inside(app.root, journal['chapter'])
    current_hash = digest(source.read_bytes())
    if journal['status'] == 'prepared' and current_hash != journal['merged_hash']:
        # A prepared journal may precede OR follow the atomic source replacement.
        if current_hash != journal['before_hash']:
            external_change(app, record, journal)
        merged = (record.parent / ('merged' + Path(journal['chapter']).suffix)).read_bytes()
        if digest(merged) != journal['merged_hash']:
            raise ValueError('合并候选副本内容已变化，请核对备份')
        atomic(source, merged)
        current_hash = digest(merged)
    if current_hash != journal['merged_hash']:
        external_change(app, record, journal)
    journal['status'] = 'written'
    json_write(record, journal)


def configured_build(root, data):
    """None preserves a Quarto project's original Make/PDF workflow; {} means source-only."""
    root, data = Path(root).resolve(), Path(data).resolve()
    manifest = load_manifest(root)
    if manifest is None:
        return None
    build = manifest.get('build', {})
    if not isinstance(build, dict) or set(build) - {'check', 'render', 'artifact'}:
        raise ValueError('draftpair.json 的 build 可包含 check、render 和 artifact')
    for action in ('check', 'render'):
        if action in build:
            argv = build[action]
            if (not isinstance(argv, list) or not argv or not argv[0]
                    or not all(isinstance(arg, str) and '\x00' not in arg for arg in argv)):
                raise ValueError(f'build.{action} 需要程序及参数组成的非空数组')
    if 'artifact' in build:
        if not build.get('render'):
            raise ValueError('build.artifact 需要对应的 build.render 命令')
        output = artifact_path(root, data, build['artifact'])
        inputs = [doc['path'] for doc in manifest['documents']] + list(manifest['adapters'].values())
        if (any(output == inside(root, name) for name in inputs)
                or manifest.get('reader') == 'quarto' and output.suffix.lower() == '.qmd'):
            raise ValueError('构建产物与登记的文稿源文件重名')
    return build


def artifact_path(root, data, name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('build.artifact 需要项目内单个产物的相对路径')
    rel = Path(name)
    if not rel.parts or rel.drive or rel.root or '..' in rel.parts:
        raise ValueError('构建产物需要项目内的相对路径')
    path = inside(root, name)
    if (set(rel.parts) & RESERVED or set(path.relative_to(root).parts) & RESERVED
            or path in {root / MANIFEST, root / '_quarto.yml', root / '_quarto.yaml'}
            or data.is_relative_to(root) and path.is_relative_to(data)):
        raise ValueError('构建产物路径指向了项目配置或审阅数据')
    return path


def built_outputs(app, plan):
    if plan is None:
        directory = inside(app.root, 'build')
        paths = [p for p in directory.glob('*.pdf') if p.resolve().parent == directory]
    else:
        paths = [artifact_path(app.root, app.data, plan['artifact'])] if 'artifact' in plan else []
    result = {}
    for path in paths:
        if path.is_file():
            st = path.stat()
            result[path.resolve()] = (st.st_mtime_ns, st.st_size, st.st_ino, digest(path.read_bytes()))
    return result


def validate_merge(app, record, journal):
    plan = journal.get('build')  # Old journals always used the Quarto workflow.
    commands = {'check': ['make', 'check'], 'render': ['make', 'render']} if plan is None else plan
    artifact = None
    try:
        before = built_outputs(app, plan)
        for action in ('check', 'render'):
            if action not in commands:
                continue
            argv = commands[action]
            process = subprocess.run(argv, cwd=app.root, capture_output=True)
            atomic(record.parent / (action + '.log'), process.stdout + process.stderr)
            if process.returncode:
                raise ValueError(' '.join(argv) + f' 未通过（{action}.log）')
        if plan is None or 'artifact' in plan:
            after = built_outputs(app, plan)
            candidates = [p for p, signature in after.items() if before.get(p) != signature]
            if len(candidates) != 1:
                raise ValueError('需要本次构建生成或更新的唯一产物，请核对输出路径和 render.log')
            artifact = candidates[0]
            content = artifact.read_bytes()
            if not content or (artifact.suffix.lower() == '.pdf' and not content.startswith(b'%PDF-')):
                raise ValueError('构建产物为空或缺少 PDF 文件标识')
            archived = record.parent / ('review' + artifact.suffix)
            atomic(archived, content)
            if plan is None:
                journal.update(status='awaiting-pdf-review', build_pdf=str(artifact),
                               review_pdf=str(archived), build_pdf_hash=digest(content))
            else:
                journal.update(status='awaiting-artifact-review', build_artifact=str(artifact),
                               review_artifact=str(archived), artifact_hash=digest(content))
        else:
            journal['status'] = 'validated'
    except (ValueError, OSError) as error:
        journal['status'] = 'validation-failed'
        json_write(record, journal)
        raise ValueError(f'正文已写入，{error}。修复后用 merge-resume 继续。记录 {record}') from error
    if digest(inside(app.root, journal['chapter']).read_bytes()) != journal['merged_hash']:
        external_change(app, record, journal)
    try:
        app.refresh_stats(artifact)
        journal.pop('stats_error', None)
    except Exception as error:
        # Sidebar statistics can be recovered independently of a successful source merge.
        journal['stats_error'] = str(error)
        print(f'正式稿统计更新失败。可运行 refresh-stats 重试。{error}', file=sys.stderr)
    json_write(record, journal)
    if journal['status'] == 'validated':
        finish_merge(app, record, journal)


def finish_merge(app, record, info):
    ch, version = info['chapter'], info['version']
    selected = app.store.version_info(ch, version)
    round_info = app.store.round_info(ch, selected['round'])
    if not round_info.get('closed'):
        if digest(inside(app.root, ch).read_bytes()) != info['merged_hash']:
            external_change(app, record, info)
        app.store.close_round(ch, info['latest'], version, owner=str(record))
    elif round_info.get('merged_version') != version:
        raise ValueError('该轮已按另一版本归档，请核对记录')
    info['status'] = 'complete'
    json_write(record, info)


def confirm_merge(app, record, info, artifact, note):
    if info['status'] in READY_TO_ARCHIVE or info['status'] == 'complete':
        finish_merge(app, record, info)
        return
    if info['status'] not in AWAITING_REVIEW:
        raise ValueError('请先使用 merge-resume 完成检查并登记本次构建产物')
    legacy = info['status'] == 'awaiting-pdf-review'
    built = Path(info['build_pdf'] if legacy else info['build_artifact'])
    saved = Path(info['review_pdf'] if legacy else info['review_artifact'])
    expected = info['build_pdf_hash'] if legacy else info['artifact_hash']
    artifact = artifact.resolve()
    if artifact not in {built, saved} or not artifact.is_file():
        raise ValueError(f'请提供本次构建的产物或合并目录内的 {saved.name}')
    if digest(artifact.read_bytes()) != expected or not note.strip():
        problem = 'PDF 已变化' if saved.suffix.lower() == '.pdf' else '产物已变化'
        raise ValueError(f'{problem}或缺少实际检查结论，请核对 {saved}')
    if digest(inside(app.root, info['chapter']).read_bytes()) != info['merged_hash']:
        external_change(app, record, info)
    if legacy:
        info.update(status='pdf-reviewed', pdf=str(artifact), pdf_hash=expected)
    else:
        info.update(status='artifact-reviewed', inspected_artifact=str(artifact))
    info['review_note'] = note
    json_write(record, info)
    finish_merge(app, record, info)


def main():
    parser = argparse.ArgumentParser(description='对稿 · DraftPair')
    parser.add_argument('--root', type=Path, default=default_project())
    parser.add_argument('--data', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    server = commands.add_parser('serve')
    server.add_argument('--port', type=int)
    server.add_argument('--open', action='store_true')
    commands.add_parser('status')
    init = commands.add_parser('init', help='识别文稿或按指定顺序创建接入清单')
    init.add_argument('--dry-run', action='store_true', help='只输出拟采用的清单')
    init.add_argument('documents', nargs='*', help='按阅读顺序列出的项目内相对路径')
    commands.add_parser('doctor', help='只读验证文稿顺序、预览与原文件行范围')
    commands.add_parser('refresh-stats', help='重新保存正式文稿与已配置 PDF 的统计')
    export = commands.add_parser('export')
    export.add_argument('--chapter', required=True)
    export.add_argument('--version', required=True)
    export.add_argument('--output', type=Path, required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--chapter', required=True)
    publish.add_argument('--input', type=Path, required=True)
    publish.add_argument('--parent', required=True)
    publish.add_argument('--applied', action='append', default=[])
    publish.add_argument('--questions', type=Path)
    merge_parser = commands.add_parser('merge')
    merge_parser.add_argument('--chapter', required=True)
    merge_parser.add_argument('--version', required=True)
    finish = commands.add_parser('merge-complete')
    finish.add_argument('--journal', type=Path, required=True)
    finish.add_argument('--artifact', '--pdf', dest='artifact', type=Path, required=True)
    finish.add_argument('--review-note', required=True)
    resume = commands.add_parser('merge-resume')
    resume.add_argument('--journal', type=Path, required=True)
    args = parser.parse_args()
    if args.command in ('init', 'doctor'):
        from onboard import initialize, diagnose
        if args.command == 'init':
            result = initialize(args.root, args.documents, args.dry_run)
        else:
            root = args.root.resolve()
            plan = configured_build(root, (args.data or default_data(root)).resolve())
            result = {**diagnose(root), 'data': str((args.data or default_data(root)).resolve()),
                      'build': plan if plan is not None else 'quarto'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not any((args.root / name).is_file() for name in (MANIFEST, '_quarto.yml')):
        parser.error('请先在文稿根目录运行 init，或指定已有配置的项目。')
    app = Application(args.root, args.data or default_data(args.root))
    if args.command == 'serve':
        if args.data is None and app.root == (HERE / 'sample').resolve():
            from demo import seed_example
            seed_example(app)
        serve(app, args.port, args.open)
    elif args.command == 'refresh-stats':
        print(json.dumps(app.refresh_stats(), ensure_ascii=False))
    elif args.command == 'status':
        print(json.dumps(app.store.state(), ensure_ascii=False, indent=2))
    elif args.command == 'export':
        output = args.output.resolve()
        if not output.is_relative_to(HERE) and not output.is_relative_to(app.data):
            raise ValueError('请将独立候选稿导出到审阅目录内')
        if output.exists():
            raise ValueError('输出文件已存在，请换一个文件名')
        atomic(output, app.store.read_version(args.chapter, args.version))
        print(output)
    elif args.command == 'publish':
        questions = json.loads(args.questions.read_bytes()) if args.questions else {}
        print(json.dumps(app.store.publish(args.chapter, args.input, args.parent, args.applied, questions), ensure_ascii=False, indent=2))
    elif args.command == 'merge':
        try:
            record = merge(app, args.chapter, args.version)
            print(f'正式文稿已合并并归档。记录 {record}')
        except MergeWritten as result:
            record = result.args[0]
            _, info = merge_record(app, record)
            artifact = info.get('review_artifact') or info['review_pdf']
            print(f'正文已写入，项目检查已通过。核对 {artifact} 后执行 merge-complete。记录 {record}')
    elif args.command in {'merge-resume', 'merge-complete'}:
        record, initial = merge_record(app, args.journal)
        with merge_lock(app, initial['chapter']):
            _, info = merge_record(app, record)
            if args.command == 'merge-resume':
                if info['status'] != 'complete':
                    continue_merge(app, record, info)
                if info['status'] == 'complete':
                    print('文稿已归档')
                else:
                    print('检查通过，请核对 ' + (info.get('review_artifact') or info['review_pdf']))
            else:
                confirm_merge(app, record, info, args.artifact, args.review_note)
                print('该章已回到实时正文')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, StoreError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
