"""Start or reuse the project's local workbench, preserving its browser origin."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import webbrowser
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from project import MANIFEST
from review import PROTOCOL, default_data, default_project
from store import atomic_write
from file_lock import file_lock

HERE = Path(__file__).resolve().parent
LOCAL = build_opener(ProxyHandler({}))
PREFERENCE = HERE / '.draftpair-local.json'


def last_project():
    try:
        path = Path(json.loads(PREFERENCE.read_text(encoding='utf-8'))['root'])
    except (OSError, ValueError, KeyError, TypeError):
        return default_project()
    if not any((path / name).is_file() for name in (MANIFEST, '_quarto.yml')):
        raise ValueError('上次接入的文稿已移动。用 --root 指定新位置，或用 --demo 打开示例。')
    return path


def running(root, data):
    try:
        record = json.loads((data / 'server.json').read_text(encoding='utf-8'))
        url = record['url']
        if url != f'http://127.0.0.1:{urlsplit(url).port}/':
            return None
        with LOCAL.open(url + 'api/health', timeout=2) as response:
            health = json.load(response)
        if (Path(health['root']) == root and Path(health['data']) == data
                and type(health.get('pid')) is int and health['pid'] == record.get('pid')):
            return {**health, 'url': url, 'control_token': record.get('control_token')}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def stop_service(info, data):
    if info.get('control_token'):
        request = Request(info['url']+'api/stop', b'{}', headers={
            'Content-Type': 'application/json', 'X-DraftPair-Control': info['control_token']})
        with LOCAL.open(request, timeout=5) as response:
            response.read()
    else:
        stop_legacy_launcher(info)
    with file_lock(data / 'server.lock', timeout=15):
        pass  # All active requests have drained and the listener released its lock.


def stop_legacy_launcher(info):
    if os.name == 'nt':
        raise RuntimeError('请先关闭运行旧版审阅服务的终端，再打开对稿。')
    pid = info['pid']
    command = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'uid=,command='], capture_output=True, text=True)
    fields = command.stdout.strip().split(None, 1)
    expected = ' '.join([sys.executable, str(HERE / 'review.py'), '--root', info['root'], 'serve'])
    if fields != [str(os.getuid()), expected]:
        raise RuntimeError(f'请先结束该文稿先前的审阅服务（PID {pid}），再打开对稿。')
    os.kill(pid, signal.SIGTERM)


def readable(url):
    for endpoint in ('index.html', 'api/state'):
        try:
            with LOCAL.open(url + endpoint, timeout=90) as response:
                response.read()
        except HTTPError as error:
            details = error.read(2048).decode(errors='replace')
            if any(word in details for word in ('Operation not permitted', 'Permission denied', 'unable to open database file')):
                return False
            raise RuntimeError('读取审阅页失败：' + details) from error
    return True


def main():
    parser = argparse.ArgumentParser(description='打开对稿')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--root', type=Path)
    target.add_argument('--demo', action='store_true', help='打开自带的示例')
    parser.add_argument('--remember', action='store_true', help='将 --root 设为今后双击时打开的文稿')
    parser.add_argument('--no-open', action='store_true', help='输出本机地址，留给当前浏览器打开')
    parser.add_argument('--stop', action='store_true', help='结束所选文稿的本机服务')
    args = parser.parse_args()
    if args.remember and not args.root:
        parser.error('--remember 需要 --root 文稿目录。')
    root = (HERE / 'sample' if args.demo else args.root or last_project()).resolve()
    if not any((root / name).is_file() for name in (MANIFEST, '_quarto.yml')):
        parser.error('请先用 review.py --root <文稿目录> init 接入文稿。')
    data = default_data(root).resolve()
    if args.stop and not data.is_dir():
        print('该文稿没有运行中的审阅服务。')
        return
    data.mkdir(exist_ok=True, parents=True)
    with file_lock(data / 'launch.lock'):
        info = running(root, data)
        if args.stop:
            if info:
                stop_service(info, data)
            print('该文稿的审阅服务已关闭。')
            return
        if info and (info.get('protocol') != PROTOCOL or not readable(info['url'])):
            stop_service(info, data)
            info = None
        if not info:
            with open(data / 'server.log', 'ab') as log:
                detach = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                          if os.name == 'nt' else {'start_new_session': True})
                process = subprocess.Popen([sys.executable, str(HERE / 'review.py'), '--root', str(root), 'serve'],
                                           cwd=HERE, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **detach)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                info = running(root, data)
                if info:
                    break
                if process.poll() is not None:
                    raise RuntimeError(f'启动失败，请查看 {data / "server.log"}')
                time.sleep(.15)
            if not info or not readable(info['url']):
                raise RuntimeError(f'无法读取文稿，请核对项目权限及 {data / "server.log"}')
        if args.remember:
            atomic_write(PREFERENCE, (json.dumps({'root': str(root)}, ensure_ascii=False)+'\n').encode())
        if not args.no_open:
            webbrowser.open(info['url'])
        print(info['url'])


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        sys.exit(str(error))
