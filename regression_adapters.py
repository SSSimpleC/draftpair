"""Public manuscript adapter contract tests. All source files are temporary fixtures."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from lxml import html as lhtml

from project import load_manifest
from renderer import Renderer, digest, pandoc_command


HERE = Path(__file__).resolve().parent


class ManuscriptAdapters(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='draftpair-formats-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / 'book'
        self.root.mkdir()
        self.data = self.root / '.draftpair'

    def source(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
        return path

    def manifest(self, docs, **kwargs):
        self.source('draftpair.json', json.dumps({'version': 1, 'documents': docs, **kwargs}, ensure_ascii=False))

    def verify_blocks(self, rendered, text):
        self.assertEqual(rendered['source_hash'], digest(text))
        last = 0
        ids = set()
        for row in rendered['blocks']:
            self.assertTrue(last < row['line_start'] <= row['line_end'] <= len(text.splitlines()))
            self.assertNotIn(row['id'], ids)
            self.assertEqual(lhtml.fragment_fromstring(row['html'], create_parent='div').text_content(), row['text'])
            last = row['line_end']
            ids.add(row['id'])

    @patch('renderer.shutil.which', return_value=None)
    def test_native_markdown_table_links_and_historical_images(self, _):
        name = 'letters/门廊.md'
        source = ('---\nlang: zh\n---\n# 门廊\n\n两行\n一段。\n\n'
                  '- 一\n- **二**\n\n|列|值|\n|-|-|\n|甲|8|\n\n'
                  '[下篇](../after.txt) 与 [页外参考][ref]。\n\n[ref]: https://example.org/detail\n\n'
                  '![铜铃](../fig/bell.svg)\n\n短句。\n\n短句。\n')
        self.source(name, source)
        self.source('after.txt', '排练笔记\n第二行。\n\n下一段。\n')
        image = self.source('fig/bell.svg', '<svg xmlns="http://www.w3.org/2000/svg"><desc>铜</desc></svg>')
        self.manifest([name, {'path': 'after.txt', 'title': '散场'}])
        renderer = Renderer(self.root, self.data)
        self.assertEqual(renderer.catalog(), [{'id': name, 'no':'', 'title':'门廊'},
                                              {'id':'after.txt','no':'','title':'散场'}])
        before = renderer.render(name, source)
        self.verify_blocks(before, source)
        rows = before['blocks']
        self.assertEqual(rows[0]['line_start'], 4)
        self.assertEqual((rows[1]['line_start'], rows[1]['line_end']), (6, 7))
        self.assertEqual(rows[2]['t'], 'list')
        self.assertEqual(rows[3]['t'], 'table')
        self.assertIn('data-chapter="after.txt"', rows[4]['html'])
        self.assertIn('href="https://example.org/detail"', rows[4]['html'])
        image_url = lhtml.fragment_fromstring(rows[5]['html']).xpath('.//img/@src')[0]
        self.assertEqual(urlsplit(image_url).path, '/asset')
        self.assertEqual(parse_qs(urlsplit(image_url).query)['path'], ['fig/bell.svg'])
        self.assertNotEqual(rows[-2]['id'], rows[-1]['id'])
        snapshot = renderer.snapshot_assets(source, name)
        self.assertEqual(snapshot, {'fig/bell.svg': digest(image.read_bytes())})
        old_image_row = renderer.render(name, source, snapshot)['blocks'][5]
        image.unlink()
        self.assertEqual(renderer.render(name, source, snapshot)['blocks'][5], old_image_row)
        missing = renderer.render(name, source)['blocks'][5]
        self.assertIn('铜铃', missing['text'])
        self.assertNotIn('<img', missing['html'])
        self.assertEqual(renderer.snapshot_assets(source, name), {})
        self.assertIn('/resource/', old_image_row['html'])
        self.assertNotIn('<img', renderer.render(name, source, {})['blocks'][5]['html'])

    def test_latex_input_stays_in_its_own_file_and_unknown_macros_stay_visible(self):
        main = ('\\documentclass{book}\n\\begin{document}\n'
                '\\section{车站}\n\n等待 \\emph{明天} 和 $x^2$。\n\n'
                '\\begin{equation}\nx=y+1\n\\end{equation}\n\n'
                '\\input{parts/scene}\n\n\\unknown{字不可丢}\n\\end{document}\n')
        second = '\\section{归途}\n\n另一段只属于子文件。\n\n\\includegraphics{images/map}\n'
        self.source('main.tex', main)
        self.source('parts/scene.tex', second)
        self.source('images/map.svg', '<svg xmlns="http://www.w3.org/2000/svg"><desc>地图</desc></svg>')
        self.manifest(['main.tex','parts/scene.tex'])
        renderer = Renderer(self.root, self.data)
        live = renderer.render('main.tex',main)
        self.verify_blocks(live, main)
        joined = '\n'.join(b['text'] for b in live['blocks'])
        self.assertNotIn('另一段只属于子文件', joined)
        self.assertIn('字不可丢', joined)
        self.assertIn('\\input{parts/scene}', joined)
        self.assertTrue(any(h['text'] == '车站' for h in live['headings']))
        if renderer.project.pandoc:
            self.assertTrue(any('<math ' in b['html'] for b in live['blocks']))
            self.assertTrue(any('<em>明天</em>' in b['html'] for b in live['blocks']))
            frozen = renderer.snapshot_assets(second,'parts/scene.tex')
            self.assertEqual(list(frozen), ['images/map.svg'])
            self.assertTrue(any('/resource/' in b['html'] for b in renderer.render('parts/scene.tex', second, frozen)['blocks']))
        with patch('renderer.shutil.which', return_value=None):
            fallback = Renderer(self.root, self.data).render('main.tex',main)
            self.assertIn('x=y+1','\n'.join(b['text'] for b in fallback['blocks']))
            self.verify_blocks(fallback, main)

    def test_windows_crlf_and_utf8_bom_preserve_title_lines_and_original_bytes(self):
        name = '日记.md'
        raw = '\ufeff# **小满**\r\n\r\n换了一场雨。\r\n'.encode('utf-8')
        path = self.root / name
        path.write_bytes(raw)
        self.manifest([name])
        app = Renderer(self.root, self.data)
        self.assertEqual(app.catalog()[0]['title'], '小满')
        view = app.render(name, raw.decode('utf-8'))
        self.assertEqual(view['source_hash'], digest(raw))
        self.assertEqual(view['blocks'][0]['t'], 'heading')
        self.assertEqual(view['blocks'][1]['line_start'], 3)
        self.assertEqual(path.read_bytes(), raw)

    def test_custom_projection_reads_candidate_and_preserves_unmapped_lines(self):
        name = '终点站.scene'
        base = '@ 电车站\n\n（站台空了。）\n\n小安 | 明天还是这个点吗？\n'
        self.source(name, base)
        adapter = self.root / 'tools/dialogue.py'
        adapter.parent.mkdir()
        shutil.copy(HERE/'docs/adapters/dialogue.py', adapter)
        self.manifest([{'path':name, 'format':'dialogue'}], adapters={'dialogue':'tools/dialogue.py'})
        renderer = Renderer(self.root, self.data)
        for source in (base, base.replace('明天还是这个点吗？','末班车是几点？')):
            view = renderer.render(name, source)
            self.verify_blocks(view, source)
            self.assertIn('电车站', view['headings'][0]['text'])
            self.assertEqual(view['blocks'][1]['t'], 'code')
            self.assertIn('站台空了',view['blocks'][1]['text'])
            self.assertEqual(view['blocks'][-1]['line_start'], 5)
            self.assertIn('小安　'+source.split(' | ')[-1].rstrip(),view['blocks'][-1]['text'])
        self.assertEqual((self.root/name).read_text(encoding='utf-8'), base)
        adapter.write_text('print(\'{"version":1,"blocks":[{"start":2,"end":90,"markdown":"bad"}]}\')')
        with self.assertRaisesRegex(ValueError, '源行'):
            renderer.render(name, base)

    def test_manifest_rejects_aliases_and_files_outside_the_manuscript(self):
        self.source('some.txt','可以读')
        self.source('.draftpair/history.txt','在审阅存储中')
        outside = self.root.parent/'elsewhere.txt'
        outside.write_text('另外的目录', encoding='utf-8')
        for invalid in [['some.txt','some.txt'],['.draftpair/history.txt'],['../elsewhere.txt'],[str(outside)],
                        [r'C:\Windows\asset.txt'], ['C:/Windows/asset.txt'], ['C:relative.txt'], [r'\\server\share\x.md'],
                        ['missing.md'],[{'path':'some.txt','format':'unknown'}],[]]:
            self.manifest(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                load_manifest(self.root)
        reader = self.root.parent / 'external.py'
        reader.write_text('raise AssertionError("should never execute")')
        for script in [str(reader), '../external.py']:
            self.manifest(['some.txt'], format='custom', adapters={'custom':script})
            with self.subTest(adapter=script), self.assertRaises(ValueError):
                load_manifest(self.root)

    def test_custom_projection_freezes_images_and_excludes_external_paths(self):
        src = '封面插画'
        self.source('draft.scene', src)
        image = self.source('fig/灯.svg', '<svg xmlns="http://www.w3.org/2000/svg"><desc>旧图</desc></svg>')
        outside = self.root.parent / 'elsewhere.svg'
        outside.write_bytes(b'<svg/>')
        self.source('preview.py', 'import json, sys\n'
                    'json.load(sys.stdin)\nprint(json.dumps({"version":1,"blocks":[{"start":1,"end":1,'
                    '"markdown":"![灯](fig/灯.svg)\\n\\n![外部图](../elsewhere.svg)"}]}))')
        self.manifest(['draft.scene'], format='custom', adapters={'custom':'preview.py'})
        renderer = Renderer(self.root,self.data)
        old_bytes = image.read_bytes()
        frozen = renderer.snapshot_assets(src, 'draft.scene')
        self.assertEqual(frozen, {'fig/灯.svg': digest(old_bytes)})
        image.write_bytes(b'<svg/>')
        old = renderer.render('draft.scene',src,frozen)['blocks'][0]
        live = renderer.render('draft.scene',src)['blocks'][0]
        self.assertIn('/resource/'+digest(old_bytes)+'.svg', old['html'])
        self.assertEqual(old['html'].count('<img'), 1)
        self.assertIn('外部图', old['text'])
        self.assertNotEqual(old['fingerprint'], live['fingerprint'])

    def test_symlink_cannot_register_an_external_document_or_adapter(self):
        outside = self.root.parent / 'external.py'
        outside.write_bytes(b'print(0)')
        self.source('some.txt','sample')
        try:
            (self.root/'link.py').symlink_to(outside)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows account lacks symbolic-link privilege')
            raise
        self.manifest(['link.py'])
        with self.assertRaises(ValueError):
            load_manifest(self.root)
        self.manifest(['some.txt'], format='custom', adapters={'custom':'link.py'})
        with self.assertRaises(ValueError):
            load_manifest(self.root)

    def test_quarto_binary_and_command_wrapper_discovery(self):
        quarto = self.root/'bin'/('quarto.exe' if os.name == 'nt' else 'quarto')
        quarto.parent.mkdir()
        executable = 'pandoc.exe' if os.name == 'nt' else 'pandoc'
        bundled = quarto.parent/'tools'/executable
        bundled.parent.mkdir()
        with patch('renderer.shutil.which', side_effect=lambda tool: str(quarto) if tool == 'quarto' else None):
            self.assertEqual(pandoc_command(True), [str(quarto), 'pandoc'])
            bundled.touch()
            self.assertEqual(pandoc_command(True), [str(bundled)])

    def test_quarto_overlay_keeps_native_catalog_and_cross_references(self):
        plan = {'render': ['quarto','render'], 'artifact':'out/book.pdf'}
        settings = {'version':1, 'reader':'quarto', 'build':plan}
        self.source('draftpair.json', json.dumps(settings))
        with self.assertRaisesRegex(ValueError, '_quarto.yml'):
            load_manifest(self.root)
        self.source('_quarto.yml', 'book:\n  chapters: [one.qmd, two.qmd]\n')
        self.source('one.qmd', '# 车站 {#sec-station}\n')
        second = '# 归途\n\n回看 @sec-station。\n'
        self.source('two.qmd',second)
        result = load_manifest(self.root)
        self.assertEqual((result['documents'], result['build']), ([], plan))
        self.source('draftpair.json', json.dumps({**settings, 'reader':'anything'}))
        with self.assertRaisesRegex(ValueError, 'reader'):
            load_manifest(self.root)
        self.source('draftpair.json', json.dumps(settings))
        if pandoc_command(prefer_quarto=True):
            native = Renderer(self.root, self.data)
            self.assertIsNone(native.project)
            self.assertEqual([c['id'] for c in native.catalog()], ['one.qmd','two.qmd'])
            preview = native.render('two.qmd',second)
            self.assertTrue(any('data-chapter="one.qmd"' in b['html'] for b in preview['blocks']))

    def test_timed_out_adapter_stops_its_converter(self):
        self.source('draft.scene', '一句')
        self.source('reader.py', 'import subprocess, sys\nfrom pathlib import Path\n'
                    'subprocess.run([sys.executable, "-c", '
                    '"from pathlib import Path; import time; Path(\'started\').touch(); '
                    'time.sleep(1); Path(\'orphan\').touch()"], check=True)\n')
        self.manifest(['draft.scene'], format='custom', adapters={'custom':'reader.py'})
        renderer = Renderer(self.root, self.data)
        with patch('manuscript.ADAPTER_TIMEOUT', 0.5), self.assertRaisesRegex(ValueError, '执行超过'):
            renderer.render('draft.scene','一句')
        self.assertTrue((self.root/'started').is_file())
        time.sleep(0.8)
        self.assertFalse((self.root/'orphan').exists())


if __name__ == '__main__':
    unittest.main()
