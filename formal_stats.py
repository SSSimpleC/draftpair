"""Formal manuscript statistics and change events for visible review pages."""
from __future__ import annotations

import contextlib
from http.client import HTTPConnection, HTTPException
import json
import hmac
from pathlib import Path
import re
import select
import socket
import threading
import time

from urllib.parse import urlsplit

from lxml import html as lhtml

from file_lock import file_lock

from project import load_manifest
from store import atomic_write


HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U0003347f]')
WORD = re.compile(r"[^\W_]+(?:['’.-][^\W_]+)*")
SEPARATORS = {'br', 'p', 'td', 'th', 'li', 'figcaption', 'pre', 'div'}


def word_count(blocks):
    total = 0
    for block in blocks:
        markup = block['html']
        if block['t'] == 'code' and 'class="review-source"' in markup:
            continue  # Unsupported source and TeX preambles are displayed for verification.
        # Insert boundaries between table cells, list items and explicit line breaks.
        fragment = lhtml.fragment_fromstring(markup, create_parent='div')
        for node in fragment.iterdescendants():
            if node.tag in SEPARATORS:
                node.tail = ' ' + (node.tail or '')
        remainder, han = HAN.subn(' ', fragment.text_content())
        total += han + len(WORD.findall(remainder))
    return total


def declared_pdf(app):
    config = load_manifest(app.root)
    if config is not None:
        build = config.get('build', {})
        name = build.get('artifact') if isinstance(build, dict) else None
    else:
        config = app.renderer._settings()
        fmt = config.get('format', {})
        formats = {fmt} if isinstance(fmt, str) else fmt
        if not isinstance(formats, (dict, list, set)) or not any(f in formats for f in ('typst', 'pdf', 'beamer')):
            return None
        book = config['book']
        title = book.get('output-file') or book.get('title')
        if not isinstance(title, str) or not title:
            return None
        output = config.get('project', {}).get('output-dir', '_book')
        name = str(Path(output) / (title if title.lower().endswith('.pdf') else title + '.pdf'))
    if not isinstance(name, str) or Path(name).is_absolute() or Path(name).suffix.lower() != '.pdf':
        return None
    path = (app.root / name).resolve()
    return path if path.is_relative_to(app.root) else None


def page_count(path):
    if not path or path.suffix.lower() != '.pdf' or not path.is_file():
        return None
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError:
        return None
    try:
        with path.open('rb') as stream:
            return len(PdfReader(stream).pages)
    except (OSError, ValueError, KeyError, IndexError, TypeError, PyPdfError):
        return None


def saved_stats(path):
    try:
        result = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None
    if (isinstance(result, dict) and type(result.get('revision')) is int
            and type(result.get('words')) is int and 'pages' in result
            and (result['pages'] is None or type(result['pages']) is int)):
        return result
    return None


def snapshot(app, *, refresh=False, chapters=None, artifact=None):
    """Only a cold start, explicit refresh or successful merge computes fresh figures."""
    directory = app.data / 'book-stats'
    path = directory / 'current.json'
    if not refresh:
        previous = saved_stats(path)
        if previous is not None:
            return previous
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / 'write.lock'):
        previous = saved_stats(path)
        if previous is not None and not refresh:
            return previous
        if chapters is None:
            words = 0
            for entry in app.renderer.catalog():
                ch = entry['id']
                text = (app.root / ch).read_bytes().decode('utf-8')
                words += word_count(app.renderer.render(ch, text)['blocks'])
        else:
            words = sum(word_count(entry['live']['blocks']) for entry in chapters)
        pages = page_count(artifact if artifact is not None else declared_pdf(app))
        if previous and (words, pages) == (previous['words'], previous['pages']):
            return previous
        revision = max(time.time_ns() // 1000, previous['revision'] + 1 if previous else 1)
        result = dict(revision=revision, words=words, pages=pages)
        atomic_write(path, (json.dumps(result) + '\n').encode())
    if refresh:
        announce(app)
    return result


def announce(app):
    """Wake open pages in the matching local service after publishing a durable snapshot."""
    try:
        info = json.loads((app.data / 'server.json').read_bytes())
        address = urlsplit(info['url'])
        token = info.get('control_token')
        if (Path(info['root']).resolve() != app.root or not isinstance(token, str) or not token or
                info['url'] != f'http://127.0.0.1:{address.port}/'):
            return
        # An idle/stopped service is normal. Reading the snapshot never depends on delivery.
        with contextlib.closing(HTTPConnection('127.0.0.1', address.port, timeout=1)) as link:
            link.request('POST', '/api/book-stats/notify', b'{}',
                         {'X-DraftPair-Control': token, 'Content-Type': 'application/json'})
            link.getresponse().read(128)
    except (OSError, HTTPException, KeyError, ValueError, TypeError):
        return


def authorized(handler):
    supplied = handler.headers.get('X-DraftPair-Control', '')
    return (not handler.headers.get('Origin') and handler.client_address[0] == '127.0.0.1'
            and hmac.compare_digest(supplied.encode(), handler.server.control_token.encode()))


class Subscribers:
    """Each live tab sleeps on a socket. One signal covers any pending snapshot changes."""
    def __init__(self):
        self.lock = threading.Lock()
        self.writers = set()
        self.closed = False

    @contextlib.contextmanager
    def listen(self):
        reader, writer = socket.socketpair()
        try:
            writer.setblocking(False)
            with self.lock:
                if self.closed:
                    writer.shutdown(socket.SHUT_WR)
                else:
                    self.writers.add(writer)
            yield reader
        finally:
            with self.lock:
                self.writers.discard(writer)
            writer.close()
            reader.close()

    def publish(self):
        with self.lock:
            for writer in self.writers:
                try:
                    writer.send(b'!')
                except OSError:
                    # A full notification buffer already wakes its reader. Closed tabs unregister.
                    pass

    def close(self):
        with self.lock:
            self.closed = True
            for writer in self.writers:
                writer.shutdown(socket.SHUT_WR)


def stream(handler):
    app = handler.app
    try:
        with handler.server.stats_events.listen() as signal:
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            last = None
            while True:
                current = app.book_stats()
                if current['revision'] != last:
                    handler.wfile.write(('data: ' + json.dumps(current) + '\n\n').encode())
                    handler.wfile.flush()
                    last = current['revision']
                ready, _, _ = select.select([handler.connection, signal], [], [])
                if handler.connection in ready or not signal.recv(8192):
                    return
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        return
