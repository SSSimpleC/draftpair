"""Check folded text drafts with real mouse gestures in a disposable manuscript."""
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

from regression_portable import HERE, api, workbench


QUOTE = '纸灯亮了'
PREFIX = '纸灯亮了，走到桥边，'


def drag_text(page, element, start, text):
    # Use the first and last glyph, including after the text has been wrapped in a draft mark.
    element.scroll_into_view_if_needed()
    points = element.evaluate("""(el, [start, end]) => {
        const node = el.firstChild;
        return [start, end].map((offset, i) => {
            const range = document.createRange();
            range.setStart(node, offset);
            range.setEnd(node, offset + 1);
            const rect = range.getBoundingClientRect();
            return [rect.left + rect.width * (i ? .85 : .15), rect.top + rect.height / 2];
        });
    }""", [start, start + len(text) - 1])
    page.mouse.move(*points[0])
    page.mouse.down()
    page.mouse.move(*points[1], steps=10)
    page.mouse.up()
    assert page.evaluate('getSelection().toString()') == text


def check_style(mark):
    expect(mark).to_have_css('text-decoration-style', 'dashed')
    # Compare painted colors to avoid browser-specific CSS color string formats.
    assert mark.evaluate("""el => {
        const css = getComputedStyle(el);
        const ctx = document.createElement('canvas').getContext('2d');
        function pixel(color) {
            ctx.clearRect(0, 0, 1, 1);
            ctx.fillStyle = color;
            ctx.fillRect(0, 0, 1, 1);
            return String(ctx.getImageData(0, 0, 1, 1).data);
        }
        return pixel(css.textDecorationColor) === pixel('rgba(201, 138, 27, .85)')
            && css.color === getComputedStyle(el.closest('p')).color && css.opacity === '1';
    }""")


def check_draft(page, url):
    page.goto(url, wait_until='domcontentloaded')
    paragraph = page.locator('.blk p')
    mark = page.locator('.anno.draft')
    editor = page.locator('#edText')
    button = page.locator('#selBtn')
    draft = '再想想桥边灯光的颜色。'
    revised = draft + '第二处写成暖色。'

    drag_text(page, paragraph, len(PREFIX), QUOTE)
    button.click()
    held = []
    page.route('**/api/annotation', lambda route: held.append(route), times=1)
    with page.expect_request(lambda req: req.url == url + 'api/annotation'):
        editor.fill(draft)
        page.locator('.chap-head h1').click()
    expect(editor).to_be_hidden()
    expect(mark).to_have_text(QUOTE)
    mark.click()  # Resume while the first autosave request is still in flight.
    expect(editor).to_have_value(draft)
    editor.fill(revised)
    editor.press('Escape')
    with page.expect_response(lambda res: res.url == url + 'api/annotation'
                              and res.request.post_data_json['text'] == revised):
        held[0].continue_()

    page.reload(wait_until='domcontentloaded')
    expect(mark).to_have_text(QUOTE)
    current, = api(url, 'state')['annotations']
    assert current['status'] == 'draft' and current['anchor']['segs'][0]['off'] == len(PREFIX)
    check_style(mark)
    mark.hover()
    check_style(mark)
    drag_text(page, mark, 0, QUOTE)
    expect(editor).to_be_hidden()
    expect(button).to_be_visible()
    page.locator('.chap-head h1').click()
    mark.click()
    expect(editor).to_have_value(revised)
    editor.press('Enter')
    expect(mark).to_have_count(0)
    expect(page.locator('.anno')).to_have_css('text-decoration-style', 'solid')
    pending, = api(url, 'state')['annotations']
    assert pending['id'] == current['id'] and pending['status'] == 'pending'
    assert pending['text'] == revised

    drag_text(page, paragraph, 0, QUOTE)
    button.click()
    editor.fill('这一处保留原句。')
    editor.press('Escape')
    mark.click()
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='放弃草稿').click()
    expect(mark).to_have_count(0)
    assert [a['id'] for a in api(url, 'state')['annotations']] == [pending['id']]


if __name__ == '__main__':
    with TemporaryDirectory(prefix='draftpair-draft-ui-') as temp:
        project = Path(temp).resolve()
        source = project / '夜行.md'
        original = ('# 夜行\n\n' + PREFIX + QUOTE + '。\n').encode('utf-8')
        source.write_bytes(original)
        (project / 'draftpair.json').write_text('{"version":1,"documents":["夜行.md"]}', encoding='utf-8')
        with workbench(HERE, project) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                check_draft(browser.new_page(viewport={'width': 1200, 'height': 800}), url)
            finally:
                browser.close()
        assert source.read_bytes() == original
    print('Fold, resume, reload, text selection, save and discard verified.')
