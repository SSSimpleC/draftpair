"""Merge lifecycle regression with disposable QMDs and a synthetic PDF marker.

The test PDF checks file identity/durability only; it does not certify a rendered
thesis PDF or perform a visual inspection. Run: python3 regression_merge.py.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import review
from store import Store, StoreError, digest


class MergeRegression(unittest.TestCase):
    first = 'chapters/one.qmd'
    second = 'chapters/two.qmd'
    base = b"# Intro\n\nA sentence.\n\nAnchor two.\n\nAnchor three.\n\nA final sentence.\n"
    candidate = base.replace(b'A final', b'A revised final')
    make = "check:\n\t@true\nrender:\n\t@mkdir -p build && cp fixture.pdf build/thesis.pdf\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve()
        root, data = self.dir / 'source', self.dir / 'data'
        (root / 'chapters').mkdir(parents=True)
        (root / 'Makefile').write_text(self.make)
        # Synthetic marker for format, path and digest tests; no real visual approval.
        (root / 'fixture.pdf').write_bytes(b'%PDF-1.7\n% synthetic logic-only marker\n%%EOF\n')
        self.ledger = Store(root, data)
        self.app = SimpleNamespace(root=root, data=data, store=self.ledger)
        self.source = root / self.first
        for ch in (self.first, self.second):
            (root/ch).write_bytes(self.base)
            live = 'live:' + digest(self.base)
            self.ledger.save(dict(ch=ch, id=Path(ch).stem, source_version=live, target_version=live,
                                  anchor={'type':'chapter'}, quote='Intro', text='revise'))
        self.input = self.dir / 'candidate.qmd'
        self.input.write_bytes(self.candidate)
        parent = self.ledger.round_info(self.first)['latest']
        self.version = self.ledger.publish(self.first, self.input, parent)['id']

    def enter(self):
        with self.assertRaises(review.MergeWritten) as raised:
            review.merge(self.app, self.first, self.version)
        return Path(raised.exception.args[0])

    def cli(self, *args):
        argv = ['review.py', *map(str, args)]
        with patch.object(sys, 'argv', argv), patch.object(review, 'Application', return_value=self.app), redirect_stdout(StringIO()):
            review.main()

    def complete(self, j, pdf=None):
        self.cli('merge-complete', '--journal', j, '--pdf', pdf or j.parent/'review.pdf', '--review-note',
                 '仅检查合并协议的逻辑测试标记，未进行论文视觉验收。')

    def test_clean_merge_preserves_external_changes_and_other_chapter(self):
        self.source.write_bytes(self.base.replace(b'A sentence', b'New sentence'))
        record = self.enter()
        self.assertEqual(self.source.read_bytes(), self.candidate.replace(b'A sentence', b'New sentence'))
        self.assertTrue(self.ledger.round_info(self.first))
        self.assertRaises(StoreError, self.ledger.publish, self.first, self.input, self.version)
        other = self.ledger.round_info(self.second)['latest']
        self.assertEqual(self.ledger.publish(self.second, self.input, other)['number'], 1)
        self.assertRaisesRegex(ValueError, '正在合并', review.merge, self.app, self.first, self.version)
        self.complete(record)
        self.assertIsNone(self.ledger.round_info(self.first))
        self.assertIsNone(self.ledger.merge_owner(self.first))
        self.assertTrue(self.ledger.round_info(self.second))
        self.complete(record)  # a replay is harmless
        self.assertEqual(json.loads(record.read_text())['status'], 'complete')

    def test_content_conflict_never_writes_source(self):
        before = self.base.replace(b'A final', b'Conflicting final')
        self.source.write_bytes(before)
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.enter()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIsNone(self.ledger.merge_owner(self.first))
        data = self.app.data / 'merges'
        j = next(data.glob('*/journal.json'))
        self.assertEqual(json.loads(j.read_text())['status'], 'conflict')
        self.assertIn(b'<<<<<<<', (j.parent / 'merged.qmd').read_bytes())

    def test_check_failure_resumes_without_replacing_source(self):
        make = self.app.root / 'Makefile'
        make.write_text('check:\n\t@false\nrender:\n\t@true\n')
        with self.assertRaisesRegex(ValueError, 'make check'):
            review.merge(self.app, self.first, self.version)
        j = next((self.app.data/'merges').glob('*/journal.json'))
        self.assertEqual(json.loads(j.read_text())['status'], 'validation-failed')
        inode = self.source.stat().st_ino
        make.write_text(self.make)
        self.cli('merge-resume', '--journal', j)
        self.assertEqual(json.loads(j.read_text())['status'], 'awaiting-pdf-review')
        self.assertEqual(inode, self.source.stat().st_ino)
        self.complete(j)

    def test_reject_other_pdf_or_modified_build_output(self):
        record = self.enter()
        self.assertRaisesRegex(ValueError, '本次构建', self.complete, record, self.source)
        build = self.app.root / 'build' / 'thesis.pdf'
        build.write_bytes(b'%PDF-1.7\n% unrelated later build\n')
        self.assertRaisesRegex(ValueError, 'PDF 已变化', self.complete, record, build)
        self.complete(record, record.parent / 'review.pdf')
        self.assertEqual(json.loads(record.read_text())['pdf_hash'],
                         digest((self.app.root/'fixture.pdf').read_bytes()))

    def test_successful_make_cannot_use_an_untouched_old_pdf(self):
        build = self.app.root / 'build'
        build.mkdir()
        (build / 'stale.pdf').write_bytes((self.app.root/'fixture.pdf').read_bytes())
        (self.app.root/'Makefile').write_text('check:\n\t@true\nrender:\n\t@true\n')
        with self.assertRaisesRegex(ValueError, '本次构建生成'):
            self.enter()

    def test_source_changed_during_review_is_preserved_and_released(self):
        record = self.enter()
        latest = self.source.read_bytes() + b'External change.\n'
        self.source.write_bytes(latest)
        with self.assertRaisesRegex(ValueError, '新的改动'):
            self.complete(record)
        self.assertEqual(self.source.read_bytes(), latest)
        self.assertIsNone(self.ledger.merge_owner(self.first))
        self.assertEqual(json.loads(record.read_text())['status'], 'external-change')
        self.assertTrue(self.ledger.round_info(self.first))

    def test_two_concurrent_merges_produce_only_one_write(self):
        def attempt(_):
            try:
                review.merge(self.app, self.first, self.version)
            except review.MergeWritten:
                return 'written'
            except ValueError as err:
                self.assertIn('正在合并', str(err))
                return 'busy'
        with ThreadPoolExecutor(max_workers=2) as ex:
            results = sorted(ex.map(attempt, range(2)))
        self.assertEqual(results, ['busy', 'written'])
        self.assertEqual(len(list((self.app.data/'merges').glob('*/journal.json'))), 1)

    def crash_source(self, after):
        # Use a child process and _exit, so Python cleanup/finally cannot mask the crash.
        child = os.fork()
        if child == 0:
            original = review.atomic
            def crash(path, data):
                if path == self.source and not after:
                    os._exit(47)
                original(path, data)
                if path == self.source:
                    os._exit(47)
            review.atomic = crash
            try:
                review.merge(self.app, self.first, self.version)
            finally:
                os._exit(48)
        self.assertEqual(os.waitpid(child, 0)[1] >> 8, 47)
        j = next((self.app.data/'merges').glob('*/journal.json'))
        self.assertEqual(json.loads(j.read_text())['status'], 'prepared')
        self.assertEqual(self.source.read_bytes(), self.candidate if after else self.base)
        inode = self.source.stat().st_ino
        self.cli('merge-resume', '--journal', j)
        self.assertEqual(self.source.read_bytes(), self.candidate)
        if after:
            self.assertEqual(self.source.stat().st_ino, inode)
        self.complete(j)

    def test_crash_before_source_replacement(self):
        self.crash_source(False)

    def test_crash_after_source_replacement(self):
        self.crash_source(True)

    def test_crash_after_archiving_before_journal_close(self):
        record = self.enter()
        original = review.json_write
        def fail(path, obj):
            if path == record and obj['status'] == 'complete':
                raise OSError('模拟日志落盘中断')
            original(path, obj)
        with patch.object(review, 'json_write', side_effect=fail), self.assertRaises(OSError):
            self.complete(record)
        self.assertIsNone(self.ledger.round_info(self.first))
        self.assertEqual(json.loads(record.read_text())['status'], 'pdf-reviewed')
        self.cli('merge-resume', '--journal', record)
        self.assertEqual(json.loads(record.read_text())['status'], 'complete')


if __name__ == '__main__':
    unittest.main(verbosity=2)
