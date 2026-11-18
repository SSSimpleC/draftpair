#!/usr/bin/env python3
"""Local thesis review server and explicit Codex publishing/merge commands."""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from renderer import Renderer, IMAGE_EXTENSIONS
from store import Store, StoreError


def default_project():
    return HERE.parent if (HERE.parent / '_quarto.yml').is_file() else HERE / 'example'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with open(temp, 'wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


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
        self.data.mkdir(parents=True, exist_ok=True)
        if self.data.name == '.qmd-margin':
            ignore = self.data / '.gitignore'
            if not ignore.exists():
                ignore.write_text('*\n')
        self.scope = digest(os.fsencode(self.root) + b'\0' + os.fsencode(self.data))
        self.renderer = Renderer(self.root, self.data)
        self.store = Store(self.root, self.data, self.renderer, is_chapter=self.renderer.is_chapter)
        self.render_lock = threading.RLock()

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
            return {**stored, 'chapters': chapters, 'project': str(self.root)}


class Handler(BaseHTTPRequestHandler):
    server_version = 'ThesisReview/1.0'

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
                return self.send(200, {'root': str(self.app.root), 'data': str(self.app.data), 'pid': os.getpid()})
            if parsed.path == '/api/state':
                return self.send(200, self.app.state())
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
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.send(403, {'error': '请在本机审阅页保存'})
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                return self.send(415, {'error': '请求须为 JSON'})
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4 * 1024 * 1024:
                raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(length))
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
    with open(app.data / 'server.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            info = json.loads((app.data / 'server.json').read_text())
            if open_browser:
                webbrowser.open(info['url'])
            print(info['url'])
            return
        preferred = port
        if preferred is None:
            preferred = 0
            try:
                previous = json.loads((app.data / 'server.json').read_text())
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
        url = f'http://127.0.0.1:{server.server_port}/'
        json_write(app.data / 'server.json', {'url': url, 'pid': os.getpid(), 'root': str(app.root)})
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


OPEN_MERGES = {'prepared', 'written', 'validation-failed', 'awaiting-pdf-review', 'pdf-reviewed'}


class MergeWritten(Exception):
    """The merged source was validated and its PDF is ready for inspection."""


@contextlib.contextmanager
def merge_lock(app, ch):
    name = digest(ch.encode())[:24] + '.lock'
    directory = app.data / 'merges'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def merge_record(app, path):
    record = path.resolve(strict=True)
    if record.name != 'journal.json' or record.parent.parent != (app.data / 'merges').resolve():
        raise ValueError('合并记录路径无效')
    return record, json.loads(record.read_text())


def open_record(app, ch):
    owner = app.store.merge_owner(ch)
    if owner:
        return Path(owner)
    for record in (app.data / 'merges').glob('*/journal.json'):
        info = json.loads(record.read_text())
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
        source = inside(app.root, ch)
        run = app.data / 'merges' / (time.strftime('%Y%m%d-%H%M%S') + '-' + digest(os.urandom(16))[:8])
        run.mkdir(parents=True)
        record = (run / 'journal.json').resolve()
        before = source.read_bytes()
        base = app.store.read_version(ch, info['versions'][0]['id'])
        candidate = app.store.read_version(ch, version)
        for name, raw in [('before.qmd', before), ('base.qmd', base), ('candidate.qmd', candidate)]:
            atomic(run / name, raw)
        result = subprocess.run(['git', 'merge-file', '-p', '--diff3', str(run/'before.qmd'),
                                 str(run/'base.qmd'), str(run/'candidate.qmd')], capture_output=True)
        if result.returncode < 0 or result.returncode > 127:
            raise ValueError('三方比较失败：' + result.stderr.decode(errors='replace'))
        atomic(run / 'merged.qmd', result.stdout)
        journal = {'chapter': ch, 'version': version, 'latest': info['latest'], 'time': time.time(),
                   'status': 'conflict' if result.returncode else 'prepared',
                   'before_hash': digest(before), 'merged_hash': digest(result.stdout)}
        json_write(record, journal)
        if result.returncode:
            raise ValueError(f'同一处文字发生冲突，请查看 {run / "merged.qmd"}')
        continue_merge(app, record, journal)
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
    if journal['status'] == 'pdf-reviewed':
        finish_merge(app, record, journal)
        return
    if journal['status'] not in OPEN_MERGES:
        raise ValueError('该记录没有需要恢复的写入或检查')
    try:
        app.store.reserve_merge(ch, journal['latest'], journal['version'], str(record))
    except StoreError as error:
        if journal['status'] == 'prepared' and app.store.merge_owner(ch) != str(record):
            journal['status'] = 'superseded'
            json_write(record, journal)
        raise
    source = inside(app.root, ch)
    current_hash = digest(source.read_bytes())
    if journal['status'] == 'prepared' and current_hash != journal['merged_hash']:
        # A prepared journal may precede OR follow the atomic source replacement.
        if current_hash != journal['before_hash']:
            external_change(app, record, journal)
        merged = (record.parent / 'merged.qmd').read_bytes()
        if digest(merged) != journal['merged_hash']:
            raise ValueError('合并候选副本内容已变化，请核对备份')
        atomic(source, merged)
        current_hash = digest(merged)
    if current_hash != journal['merged_hash']:
        external_change(app, record, journal)
    journal['status'] = 'written'
    json_write(record, journal)
    validate_merge(app, record, journal)


def build_pdfs(app):
    build = (app.root / 'build').resolve()
    return {p.resolve(): (p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino)
            for p in build.glob('*.pdf') if p.is_file() and p.resolve().parent == build}


def validate_merge(app, record, journal):
    # Different chapters share the same Quarto book output. Save the exact validated PDF
    # before another chapter builds, so either chapter can later be reviewed and archived.
    with (app.data / 'merges' / 'build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = build_pdfs(app)
        try:
            for target in ('check', 'render'):
                process = subprocess.run(['make', target], cwd=app.root, capture_output=True)
                atomic(record.parent / (target + '.log'), process.stdout + process.stderr)
                if process.returncode:
                    raise ValueError(f'make {target} 未通过')
            after = build_pdfs(app)
            candidates = [p for p, signature in after.items() if before.get(p) != signature]
            if len(candidates) != 1:
                raise ValueError('需要本次构建生成或更新的唯一论文 PDF，请核对 build 目录和 render.log')
            pdf = candidates[0]
            content = pdf.read_bytes()
            if not content.startswith(b'%PDF-'):
                raise ValueError('构建产物缺少 PDF 文件标识')
            archived_pdf = record.parent / 'review.pdf'
            atomic(archived_pdf, content)
        except (ValueError, OSError) as error:
            journal['status'] = 'validation-failed'
            json_write(record, journal)
            raise ValueError(f'正文已写入，{error}；修复后用 merge-resume 继续。记录 {record}') from error
        if digest(inside(app.root, journal['chapter']).read_bytes()) != journal['merged_hash']:
            external_change(app, record, journal)
        journal.update(status='awaiting-pdf-review', build_pdf=str(pdf), review_pdf=str(archived_pdf),
                       build_pdf_hash=digest(content))
        json_write(record, journal)


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


def confirm_merge(app, record, info, pdf, note):
    record = Path(record).resolve()
    if info['status'] in {'pdf-reviewed', 'complete'}:
        finish_merge(app, record, info)
        return
    if info['status'] != 'awaiting-pdf-review' or not info.get('build_pdf_hash'):
        raise ValueError('请先使用 merge-resume 完成编译检查并登记生成的 PDF')
    pdf = pdf.resolve()
    if pdf not in {Path(info['build_pdf']), Path(info['review_pdf'])} or not pdf.is_file():
        raise ValueError('请提供本次构建的论文 PDF 或该次合并目录内的 review.pdf')
    if digest(pdf.read_bytes()) != info['build_pdf_hash'] or not note.strip():
        raise ValueError('PDF 已变化或缺少实际检查结论，请核对本次合并目录内的 review.pdf')
    if digest(inside(app.root, info['chapter']).read_bytes()) != info['merged_hash']:
        external_change(app, record, info)
    info.update(status='pdf-reviewed', pdf=str(pdf), pdf_hash=info['build_pdf_hash'], review_note=note)
    json_write(record, info)
    finish_merge(app, record, info)


def main():
    parser = argparse.ArgumentParser(description='页畔 · QMD Margin')
    parser.add_argument('--root', type=Path, default=default_project())
    parser.add_argument('--data', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    server = commands.add_parser('serve')
    server.add_argument('--port', type=int)
    server.add_argument('--open', action='store_true')
    commands.add_parser('status')
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
    finish.add_argument('--pdf', type=Path, required=True)
    finish.add_argument('--review-note', required=True)
    resume = commands.add_parser('merge-resume')
    resume.add_argument('--journal', type=Path, required=True)
    args = parser.parse_args()
    app = Application(args.root, args.data or args.root / '.qmd-margin')
    if args.command == 'serve':
        serve(app, args.port, args.open)
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
        questions = json.loads(args.questions.read_text()) if args.questions else {}
        print(json.dumps(app.store.publish(args.chapter, args.input, args.parent, args.applied, questions), ensure_ascii=False, indent=2))
    elif args.command == 'merge':
        try:
            merge(app, args.chapter, args.version)
        except MergeWritten as result:
            print(f'正文已写入，check/render 已通过。查看生成 PDF 后执行 merge-complete。记录 {result}')
    elif args.command in {'merge-resume', 'merge-complete'}:
        record, initial = merge_record(app, args.journal)
        with merge_lock(app, initial['chapter']):
            _, info = merge_record(app, record)
            if args.command == 'merge-resume':
                if info['status'] != 'complete':
                    continue_merge(app, record, info)
                print('该章已归档' if info['status'] == 'complete' else f'检查通过，请核对 {record.parent / "review.pdf"}')
            else:
                confirm_merge(app, record, info, args.pdf, args.review_note)
                print('该章已回到实时正文')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, StoreError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
