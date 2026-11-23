"""Check manuscript discovery and the file boundary of first-time setup."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from onboard import initialize, diagnose


class Onboarding(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.book = Path(work.name).resolve()

    def test_summary_drives_order_and_preview_creates_nothing(self):
        for name in ('10.md', '2.md', 'README.md', 'AGENTS.md'):
            (self.book/name).write_text('# Opening\n\nAn afternoon walk.', encoding='utf-8')
        (self.book/'SUMMARY.md').write_text('- [Start](2.md)\n- [Later](10.md)\n', encoding='utf-8')
        proposal = initialize(self.book, [], dry_run=True)
        self.assertEqual(proposal['documents'], ['2.md', '10.md'])
        self.assertFalse((self.book/'draftpair.json').exists())
        self.assertEqual(initialize(self.book, []), proposal)
        with self.assertRaisesRegex(ValueError, '已有'):
            initialize(self.book, ['README.md'])
        self.assertEqual([f['path'] for f in diagnose(self.book)['documents']], proposal['documents'])
        self.assertFalse((self.book/'.draftpair').exists())

    def test_latex_dispatcher_and_entry_with_its_own_prose(self):
        preamble = '\\documentclass{article}\n\\begin{document}\n'
        includes = '\\input{b}\n\\include{a}\n\\end{document}\n'
        for name in ('a.tex', 'b.tex', 'unrelated.tex'):
            (self.book/name).write_text('A few words.', encoding='utf-8')
        main = self.book/'main.tex'
        main.write_text(preamble+includes, encoding='utf-8')
        self.assertEqual(initialize(self.book, [], True)['documents'], ['b.tex', 'a.tex'])
        main.write_text(preamble+'Dear reader,\n'+includes, encoding='utf-8')
        self.assertEqual(initialize(self.book, [], True)['documents'], ['main.tex', 'b.tex', 'a.tex'])
        self.assertEqual(initialize(self.book, ['unrelated.tex', 'b.tex'], True)['documents'],
                         ['unrelated.tex', 'b.tex'])


if __name__ == '__main__':
    unittest.main()
