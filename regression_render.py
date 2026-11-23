#!/usr/bin/env python3
"""Check the current thesis and a disposable, independently changing figure."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from lxml import html as lhtml

from renderer import LABEL, Renderer, digest, walk
from store import Store, StoreError


def check_actual(project, work):
    renderer = Renderer(project, work)
    catalog = renderer.catalog()
    sources = {c['id']: (project / c['id']).read_bytes() for c in catalog}
    cfg = renderer._settings()
    assert len(catalog) == len(set(c['id'] for c in catalog)), '目录出现重复 QMD'
    actual, expected, source_keys, anchors, links = Counter(), Counter(), set(), set(), []
    for chapter in catalog:
        name = chapter['id']
        text = sources[name].decode()
        arguments = ['-f', 'markdown+east_asian_line_breaks', '-t', 'json']
        for f in renderer._filters():
            arguments.extend(['--lua-filter', f])
        direct = json.loads(renderer._run(arguments, text))['blocks']
        _, mapped = renderer._parse(text, renderer._filters())
        assert [b for row in mapped for b in row['ast']] == direct, f'位置标记改变了 {name} 的论文结构'
        for row in mapped:
            lines = text.splitlines()[row['line_start']-1:row['line_end']]
            assert '\n'.join(lines) == row['raw'], f'{name} 行范围与原稿不一致'
        for node in walk(direct):
            tag = node['t']
            if tag in ('Image', 'Table', 'Math'):
                expected[tag] += 1
            if tag == 'Cite':
                keys = [c['citationId'] for c in node['c'][0] if not LABEL.match(c['citationId'])]
                if keys:
                    source_keys.update(keys)
                    expected['Cite'] += 1
        rendered = renderer.render(name, text)
        assert rendered['source_hash'] == digest(sources[name])
        last = 0
        for row in rendered['blocks']:
            assert row['line_start'] > last, f'{name} 行范围重叠'
            last = row['line_end']
            assert 1 <= row['line_start'] <= last <= len(text.splitlines())
            assert (name, row['id']) not in anchors
            anchors.add((name, row['id']))
            el = lhtml.fragment_fromstring(row['html'], create_parent='div')
            assert el.text_content() == row['text'], f'{name} 选区文字不同于 HTML'
            for kind, selector in [('Image','.//img'), ('Table','.//table'),('Math','.//math'),
                                   ('Cite','.//span[@data-cites]'),('Refs','.//*[contains(concat(" ",@class," ")," csl-entry ")]')]:
                actual[kind] += len(el.xpath(selector))
            for img in el.xpath('.//img'):
                assert img.get('src','').startswith('/asset?'), f'{name} 正文图未引用原位置'
            for link in el.xpath('.//a[@data-target]'):
                links.append((link.get('data-chapter'), link.get('data-target')))
            for group in el.xpath('.//span[@data-cites]'):
                assert group.xpath('.//a[@data-target]'), f'{name} 引用没有书目链接'
        actual['Blocks'] += len(rendered['blocks'])
    for kind in expected:
        assert actual[kind] == expected[kind], f'{kind} 实际 {actual[kind]}, 应为 {expected[kind]}'
    if cfg.get('bibliography') and 'references.qmd' in sources:
        assert actual['Refs'] == len(source_keys), '已引用文献与书目条数不一致'
    assert set(links) <= anchors, '跨章链接未定位到有效阅读块'
    assert all((project / s).read_bytes() == raw for s,raw in sources.items())
    return {'chapters':len(catalog), 'links':len(links), **dict(actual)}


def check_fixture(work):
    project, data = work/'book', work/'data'
    project.mkdir()
    (project/'_quarto.yml').write_text('book:\n  chapters: [sample.qmd]\n')
    source = (
        '# 物理课堂 {#sec-sample}\n\n![测量图](原图.svg){#fig-demo}\n\n'
        '图见 @fig-demo。\n\n$$\nF=ma\n$$ {#eq-demo}\n\n公式见 @eq-demo。\n\n'
        '两段脚注[^a]。\n\n[^a]: 第一行。\n\n    第二段说明。\n\n'
        '```{=typst}\n#par(first-line-indent: 24pt)[#h(24pt)摘要甲。]\n```\n\n'
        '```{=typst}\n#new-command[尚未支持的内容。]\n```\n\n'
        '```{.lesson-table-html}\n<table id="tbl-lesson"><caption>设计</caption>'
        '<tr><td rowspan="2">活动</td><td>甲</td></tr><tr><td>乙</td></tr></table>\n```\n'
    )
    name = 'sample.qmd'
    (project/name).write_bytes(source.encode('utf-8'))
    svg = project/'原图.svg'
    original = b'<svg xmlns="http://www.w3.org/2000/svg"><text>A</text></svg>'
    updated = original.replace(b'>A<', b'>B<')
    svg.write_bytes(original)
    r = Renderer(project, data)
    def figure(view):
        return next(row for row in view['blocks'] if row['id'] == 'fig-demo')
    live_before = r.render(name, source)
    ledger = Store(project, data, r, is_chapter=r.is_chapter)
    version = 'live:' + digest(source)
    payload = dict(id='root-chapter', ch=name, source_version=version, target_version=version,
                   anchor={'type':'chapter'}, quote='物理课堂', text='核对章节', status='draft')
    assert ledger.save(payload)['ch'] == name
    (project/'private.qmd').write_text('# Unlisted')
    try:
        ledger.save({**payload, 'id':'unlisted', 'ch':'private.qmd'})
    except StoreError:
        pass
    else:
        raise AssertionError('接受了未在书稿目录登记的 QMD')
    assert '<math ' in next(b['html'] for b in live_before['blocks'] if b['t']=='equation')
    text = '\n'.join(b['text'] for b in live_before['blocks'])
    assert '摘要甲。' in text and '尚未支持的内容。' in text and '第二段说明。' in text
    assert '式 1.1' in text and '图 1.1' in text
    assert any('rowspan="2"' in b['html'] and b['t']=='table' for b in live_before['blocks'])
    snap = r.snapshot_assets(source,name)
    old = figure(r.render(name,source,snap))
    assert f'/resource/{digest(original)}.svg' in old['html']
    assert (data/'resources'/(digest(original)+'.svg')).read_bytes()==original
    svg.write_bytes(updated)
    new = figure(r.render(name,source))
    assert digest(updated) in new['html']
    assert new['fingerprint'] != figure(live_before)['fingerprint']
    assert figure(r.render(name,source,snap)) == old
    assert (project/name).read_text(encoding='utf-8')==source


def check_working_citations(work):
    project = work/'working-book'
    project.mkdir()
    first, second, refs = 'one.qmd', 'two.qmd', 'references.qmd'
    formal = {first:'# 第一章\n\n正式稿。\n', second:'# 第二章\n\n原有文献[@early]。\n',
              refs:'# 参考文献\n\n```{=typst}\n#ldu-bibliography()\n```\n'}
    for name, value in formal.items():
        (project/name).write_bytes(value.encode('utf-8'))
    (project/'_quarto.yml').write_text('book:\n  chapters: [one.qmd, two.qmd, references.qmd]\n'
                                      'bibliography: test.bib\ncsl: numeric.csl\n')
    (project/'test.bib').write_text(''.join(
        '@book{'+key+', author={Researcher, T.}, title={Book '+key+'}, year={2020}}\n'
        for key in ('early','added','secondnew')))
    (project/'numeric.csl').write_text("""<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
      <info><title>QMD Margin test</title><id>urn:qmd-margin:test</id><updated>2026-01-01T00:00:00Z</updated></info>
      <citation><layout delimiter=","><text variable="citation-number"/></layout></citation>
      <bibliography><layout><text variable="title"/></layout></bibliography>
    </style>""")
    r = Renderer(project,work/'working-data')
    proposals = {first:formal[first]+'\n新增引文[@added]。\n',
                 second:formal[second]+'\n第二章新增[@secondnew]，也使用[@added]。\n'}
    def links(version):
        doc = lhtml.fragment_fromstring(''.join(b['html'] for b in version['blocks']),create_parent='div')
        return [(a.get('href'),a.text_content()) for a in doc.xpath('.//span[@data-cites]//a')]
    def entries(version):
        doc = lhtml.fragment_fromstring(''.join(b['html'] for b in version['blocks']),create_parent='div')
        return [e.get('id') for e in doc.xpath('.//*[contains(concat(" ",@class," ")," csl-entry ")]')]
    assert links(r.render(second,formal[second],{}))==[('#ref-early','1')]
    assert links(r.render(first,proposals[first],{},context=proposals))==[('#ref-added','1')]
    expected = [('#ref-early','2'),('#ref-secondnew','3'),('#ref-added','1')]
    assert links(r.render(second,proposals[second],{},context=proposals)) == expected
    assert entries(r.render(refs,formal[refs],context=proposals)) == ['ref-added','ref-early','ref-secondnew']
    assert entries(r.render(refs,formal[refs])) == ['ref-early']
    assert links(r.render(second,formal[second],{},context=proposals))==[('#ref-early','2')]
    assert links(r.render(second,formal[second],{}))==[('#ref-early','1')]


if __name__ == '__main__':
    with TemporaryDirectory(prefix='mgt-render-qa-') as tmp:
        work = Path(tmp)
        summary = check_actual(Path(__file__).resolve().parent/'example', work/'real-cache')
        check_fixture(work)
        check_working_citations(work)
    print('渲染、选区原文、跨章定位与图片版本检查通过。', json.dumps(summary, ensure_ascii=False))
