"""Exercise formal text, declared PDF, and push-only statistics with disposable publications."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from pypdf import PdfWriter

from formal_stats import page_count, word_count
from review import Application, digest, merge


class StatisticsRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'Opening.md'
        self.source.write_text('# 早市\n\nA slow morning.\n', encoding='utf-8')
        (self.root / 'closing.txt').write_text("门外\nHello world\n", encoding='utf-8')
        (self.root / 'not-in-manifest.txt').write_text('忽略' * 500, encoding='utf-8')
        self.config = {'version': 1, 'documents': [self.source.name, 'closing.txt']}
        self.manifest = self.root / 'draftpair.json'
        self.manifest.write_text(json.dumps(self.config))
        self.data = self.root / '.draftpair'
        self.app = Application(self.root, self.data)

    def test_words_cjk_inline_boundaries_table_cells_and_raw_fallback(self):
        self.assertEqual(word_count([{'html': '<p>相遇，can\'t 1.25 tea-<em>house</em>.</p><p>OK.</p>',
                                     't': 'paragraph'}]), 6)
        self.assertEqual(word_count([{'html': '<table><tr><td>One</td><td>Two</td></tr></table>',
                                     't': 'table'}]), 2)
        self.assertEqual(word_count([{'html': '<pre class="review-source">\\end{document}</pre>',
                                     't': 'code'}]), 0)

    def test_candidate_then_source_only_merge_changes_cached_figures(self):
        before = self.app.state()['book_stats']
        self.assertEqual((before['words'], before['pages']), (9, None))
        ch = self.source.name
        raw = self.source.read_bytes()
        live = 'live:' + digest(raw)
        note = self.app.store.save(dict(ch=ch, id='note-a', anchor={'type':'chapter'},
                                        source_version=live, target_version=live, text='Add a sentence'))
        candidate = self.data / ch
        candidate.write_bytes(raw + '\n荷叶上有雨。\n'.encode())
        version = self.app.store.publish(ch, candidate, note['target_version'])['id']
        self.assertEqual(self.app.state()['book_stats'], before)
        with patch.object(self.app.renderer, 'catalog', side_effect=AssertionError('unexpected render')):
            self.assertEqual(self.app.book_stats(), before)
        record = merge(self.app, ch, version)
        self.assertEqual(json.loads(record.read_bytes())['status'], 'complete')
        current = self.app.book_stats()
        self.assertEqual((current['words'], current['pages']), (14, None))
        self.assertGreater(current['revision'], before['revision'])
        self.assertEqual(Application(self.root, self.data).book_stats(), current)
        self.assertEqual(self.app.refresh_stats(), current)

    def test_pdf_is_physical_and_explicit(self):
        writer = PdfWriter()
        for _ in range(4):
            writer.add_blank_page(200, 400)
        proof = self.root / 'book.pdf'
        writer.write(proof)
        self.assertIsNone(self.app.refresh_stats()['pages'])
        self.config['build'] = {'render':['workshop-build'], 'artifact': proof.name}
        self.manifest.write_text(json.dumps(self.config))
        self.assertEqual(self.app.refresh_stats()['pages'], 4)
        proof.rename(self.root / 'backup.pdf')
        self.assertIsNone(self.app.refresh_stats()['pages'])
        html = self.root / 'book.html'
        html.write_text('<p>Proof</p>')
        self.assertIsNone(page_count(html))

    def test_open_stream_delivers_published_snapshot_and_server_drains_on_exit(self):
        local = build_opener(ProxyHandler({}))
        args = [sys.executable, str(Path(__file__).resolve().with_name('review.py')), '--root', str(self.root),
                'serve', '--port', '0']
        with subprocess.Popen(args, stdout=subprocess.PIPE, text=True) as server:
            try:
                url = server.stdout.readline().strip()
                with ExitStack() as clients:
                    streams = [clients.enter_context(local.open(url + 'api/book-stats/events', timeout=10))
                               for _ in range(2)]
                    def receive(stream):
                        self.assertEqual(stream.headers.get_content_type(), 'text/event-stream')
                        line = stream.readline()
                        self.assertEqual(stream.readline(), b'\n')
                        self.assertTrue(line.startswith(b'data: '))
                        return json.loads(line[6:])
                    first = self.app.book_stats()
                    self.assertEqual([receive(s) for s in streams], [first, first])
                    self.source.write_text('# 早市\n\n桥下落了一阵雨。\n', encoding='utf-8')
                    expected = self.app.refresh_stats()
                    self.assertNotEqual(first['words'], expected['words'])
                    self.assertEqual([receive(s) for s in streams], [expected, expected])
                    record = json.loads((self.data/'server.json').read_bytes())
                    stop = Request(url + 'api/stop', data=b'{}', method='POST',
                                   headers={'Content-Type': 'application/json', 'X-DraftPair-Control': record['control_token']})
                    with local.open(stop, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                    self.assertEqual(server.wait(timeout=5), 0)
            finally:
                if server.poll() is None:
                    server.kill()


if __name__ == '__main__':
    unittest.main(verbosity=2)
