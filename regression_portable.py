"""Exercise the real launcher and a writing agent's workflow using a disposable manuscript."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


HERE = Path(__file__).resolve().parent
LOCAL = build_opener(ProxyHandler({}))


def run(tool, *args):
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
           'DRAFTPAIR_NONINTERACTIVE': '1'}
    result = subprocess.run([str(arg) for arg in args], cwd=tool, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, encoding='utf-8', errors='replace', timeout=240)
    if result.returncode:
        raise AssertionError(f'{args} exited {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result.stdout


def cli(tool, project, *args):
    return run(tool, sys.executable, 'review.py', '--root', project, *args)


def launch(tool, project, option):
    if os.name == 'nt':
        run(tool, os.environ['COMSPEC'], '/d', '/c', 'call', 'Open DraftPair.cmd', '--root', project, option)
    else:
        run(tool, sys.executable, 'launch.py', '--root', project, option)


def api(url, route, payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = Request(url + 'api/' + route, data=body, headers={'Content-Type': 'application/json'})
    with LOCAL.open(request, timeout=15) as response:
        return json.load(response)


@contextmanager
def workbench(tool, project):
    record = project / '.draftpair' / 'server.json'
    try:
        launch(tool, project, '--no-open')
        listener = json.loads(record.read_bytes())
        url = listener['url']
        health = api(url, 'health')
        assert health['pid'] == listener['pid'] and Path(health['root']) == project.resolve()
        assert Path(health['data']) == record.parent.resolve()
        launch(tool, project, '--no-open')
        same = json.loads(record.read_bytes())
        assert same['url'] == url and api(url, 'health')['pid'] == health['pid']
        yield url
    except Exception:
        log = record.with_name('server.log')
        if log.is_file():
            print(log.read_text(encoding='utf-8', errors='replace')[-4000:], file=sys.stderr)
        raise
    finally:
        if record.is_file():
            launch(tool, project, '--stop')
    # Confirm the previously healthy listener really released its port.
    deadline = time.monotonic() + 5
    while True:
        try:
            health_after_stop = api(url, 'health')
        except (OSError, URLError):
            break
        if health_after_stop.get('pid') != health['pid']:
            break
        assert time.monotonic() < deadline, 'stopped service still accepts requests'
        time.sleep(.05)


def check_manuscript(tool, project):
    chapter = '章节/街角 01.md'
    source = project / chapter
    source.parent.mkdir(parents=True)
    original = '# 街角\r\n\r\n窗前亮着一盏灯，门口也亮着一盏灯。\r\n\r\n<!-- 留给下一轮的编辑注记 -->\r\n'.encode()
    source.write_bytes(original)
    config = json.loads(cli(tool, project, 'init', '--dry-run'))
    assert config['documents'] == [chapter] and not (project / 'draftpair.json').exists()
    assert json.loads(cli(tool, project, 'init')) == config
    diagnosis = json.loads(cli(tool, project, 'doctor'))
    assert diagnosis['ok'] and diagnosis['documents'][0]['path'] == chapter
    assert source.read_bytes() == original

    data = project / '.draftpair'
    quote = '一盏灯'
    with workbench(tool, project) as url:
        state = api(url, 'state')
        before = state['book_stats']
        live = state['chapters'][0]['live']
        block = next(b for b in live['blocks'] if b['text'].count(quote) == 2)
        offset = block['text'].rindex(quote)
        segment = {'block': block['id'], 'off': offset, 'text': quote,
                   'before': block['text'][:offset], 'after': block['text'][offset+len(quote):],
                   'line_start': block['line_start'], 'line_end': block['line_end']}
        note = {'id': 'street-note', 'ch': chapter, 'source_version': live['id'],
                'target_version': live['id'], 'anchor': {'type': 'text', 'segs': [segment]},
                'quote': quote, 'text': '请补充门口那盏灯的颜色。'}
        saved = api(url, 'annotation', note)['annotations'][0]
        submission = {'key': 'street-batch', 'entries': [{'id': saved['id'], 'revision': saved['revision']}]}
        api(url, 'submit', submission)
        batch = json.loads((data / 'latest.json').read_bytes())
        assert batch['ids'] == [note['id']] and batch['annotations'][0]['anchor']['segs'][0] == segment
        parent = batch['parents'][chapter]['id']
        candidate = data / '候选 01.md'
        cli(tool, project, 'export', '--chapter', chapter, '--version', parent, '--output', candidate)
        assert candidate.read_bytes() == original and source.read_bytes() == original
        revised = original.replace('门口也亮着一盏灯'.encode(), '门口也亮着一盏橘黄色的小灯'.encode())
        candidate.write_bytes(revised)
        published = json.loads(cli(tool, project, 'publish', '--chapter', chapter, '--input', candidate,
                                   '--parent', parent, '--applied', note['id']))
        state = api(url, 'state')
        doc = state['chapters'][0]
        assert source.read_bytes() == original and state['book_stats'] == before
        assert doc['round']['base_version'] == parent and doc['round']['latest_version'] == published['id']
        assert doc['working']['source_hash'] == hashlib.sha256(revised).hexdigest()
        assert state['annotations'][0]['status'] == 'applied' and Path(published['path']).suffix == '.md'

        cli(tool, project, 'merge', '--chapter', chapter, '--version', published['id'])
        final = api(url, 'state')
        assert source.read_bytes() == revised and final['chapters'][0]['round'] is None
        assert final['book_stats']['words'] > before['words'] and final['book_stats']['pages'] is None
        journal, = (data / 'merges').glob('*/journal.json')
        assert json.loads(journal.read_bytes())['status'] == 'complete'
        assert (journal.parent / 'before.md').read_bytes() == original
        assert (data / 'submissions/0001.json').is_file()
    print('Markdown source, annotation, candidate, merge, stats and shutdown verified.')


def check_sample(tool):
    with workbench(tool, tool / 'sample') as url:
        state = api(url, 'state')
        story = next(c for c in state['chapters'] if c['id'] == 'afternoon.md')
        assert [v['number'] for v in story['versions']] == [0, 1, 2] and len(state['batches']) == 2
        assert len({v['source_hash'] for v in story['versions']}) == 3
        assert state['book_stats']['words'] > 0
    print('Bundled Markdown sample opens with its two independent candidate versions.')


def main():
    with tempfile.TemporaryDirectory(prefix='draftpair-', dir=os.environ.get('RUNNER_TEMP')) as temp:
        workspace = Path(temp).resolve()
        tool = workspace / 'DraftPair 工具'
        tool.mkdir()
        # A fresh distribution, including its real Windows entry point and no existing venv or ledger.
        for path in HERE.iterdir():
            if path.is_file() and path.suffix in {'.py', '.js', '.css', '.html', '.lua', '.txt', '.cmd'}:
                shutil.copy2(path, tool / path.name)
        shutil.copytree(HERE / 'sample', tool / 'sample', ignore=shutil.ignore_patterns('.*', '__pycache__'))
        check_manuscript(tool, workspace / "作者's 短篇")
        if os.name == 'nt':
            assert (tool / '.venv/Scripts/python.exe').is_file(), 'Windows entry point did not create its venv'
        check_sample(tool)


if __name__ == '__main__':
    main()
