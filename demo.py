"""Prepare the two candidate versions of the bundled story."""
from pathlib import Path
from tempfile import TemporaryDirectory

from store import digest
from file_lock import file_lock


SAMPLE = Path(__file__).resolve().parent / 'sample'
STORY = 'afternoon.md'
EDITS = [
    ('角上有一小圈油印', '油印正好圈住配料表里半个“盐”字',
     '油印落在什么地方？让读者一眼认出，这一页是常翻的。'),
    ('瓶子里有好几种颜色', '红线绕住青线，瓶底还压着半寸银丝',
     '挑两种线写吧，想看清这家装订铺的颜色。'),
    ('车上发出了一阵很响的声音', '后架上的两只铝饭盒碰在一起，叮的一声',
     '隔着窗子先听见的是什么？把响声交给车上的一件东西。'),
]
SECOND_PASS = [
    ('把收据沿着折痕叠成一个小方块', '把收据叠回小方块，用拇指把翘起的纸角压平',
     '收据后面还要展开。这里留一点纸的手感，试试写手指压过折痕的动作。'),
]


def sample_note(blocks, version, quote, comment):
    block = next(b for b in blocks if quote in b['text'])
    text = block['text']
    start = text.index(quote)
    segment = {
        'ch': STORY, 'source_version': version, 'target_version': version,
        'block': block['id'], 'off': len(text[:start].encode('utf-16-le')) // 2,
        'text': quote, 'before': text[max(0, start - 40):start],
        'after': text[start + len(quote):start + len(quote) + 40],
        'line_start': block['line_start'], 'line_end': block['line_end'],
    }
    return {'ch': STORY, 'source_version': version, 'target_version': version,
            'quote': quote, 'text': comment, 'anchor': {'type': 'text', 'segs': [segment]}}


def publish_pass(app, original, source_version, edits, batch_key):
    blocks = app.renderer.render(STORY, original)['blocks']
    notes = [sample_note(blocks, source_version, quote, comment) for quote, _, comment in edits]
    revised = original
    for quote, replacement, _ in edits:
        revised = revised.replace(quote, replacement)
    saved = app.store.save_many(notes)
    entries = [{'id': n['id'], 'revision': n['revision']} for n in saved]
    app.store.submit(entries, batch_key)
    parent = app.store.round_info(STORY)['latest']
    with TemporaryDirectory(prefix='draftpair-sample-') as temp:
        candidate = Path(temp) / STORY
        candidate.write_text(revised, encoding='utf-8')
        published = app.store.publish(STORY, candidate, parent, [n['id'] for n in saved])
    return revised, published['id']


def seed_example(app):
    if app.root != SAMPLE:
        return
    done = app.data / 'sample-initialized'
    if done.exists():
        return
    with file_lock(app.data / 'sample.lock'):
        if done.exists():
            return
        # Keep a previously used sample (including closed rounds and withdrawn notes).
        if app.store.state()['revision']:
            done.touch()
            return
        raw = (SAMPLE / STORY).read_bytes()
        original = raw.decode('utf-8')
        follow_up = '窗框的影子挪到了收据背面'
        phrases = [edit[0] for edit in EDITS + SECOND_PASS] + [follow_up]
        if any(original.count(quote) != 1 for quote in phrases):
            return  # A reader has customized the sample.
        first, first_id = publish_pass(app, original, 'live:' + digest(raw), EDITS, 'sample-reading-1')
        second, second_id = publish_pass(app, first, first_id, SECOND_PASS, 'sample-reading-2')
        revised_blocks = app.renderer.render(STORY, second)['blocks']
        app.store.save(sample_note(revised_blocks, second_id, follow_up,
                                   '想知道看见影子的人坐在哪里。这道光是从书脊滑过去，还是从窗沿移过来的？'))
        done.touch()
