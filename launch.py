"""Start or reuse the loopback review server, then open its browser page."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

from urllib.request import urlopen

from review import default_project

HERE = Path(__file__).resolve().parent


def existing_url(root, data):
    try:
        info = json.loads((data / 'server.json').read_text())
        with urlopen(info['url'] + 'api/health', timeout=1) as response:
            health = json.load(response)
        if health['root'] == str(root) and health['data'] == str(data):
            return info['url']
    except (OSError, ValueError, KeyError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description='打开页畔')
    parser.add_argument('--root', type=Path, default=default_project())
    root = parser.parse_args().root.resolve()
    if not (root / '_quarto.yml').is_file():
        parser.error('目录中没有 _quarto.yml')
    data = root / '.qmd-margin'
    data.mkdir(exist_ok=True)
    with open(data / 'launch.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        url = existing_url(root, data)
        if not url:
            with open(data / 'server.log', 'ab') as log:
                process = subprocess.Popen([sys.executable, str(HERE / 'review.py'), '--root', str(root), 'serve'],
                                           cwd=HERE, stdout=log, stderr=log, start_new_session=True)
            for _ in range(100):
                url = existing_url(root, data)
                if url:
                    break
                if process.poll() is not None:
                    raise RuntimeError(f'启动失败，请查看 {data / "server.log"}')
                time.sleep(.15)
        if not url:
            raise RuntimeError(f'启动超时，请查看 {data / "server.log"}')
        webbrowser.open(url)
        print('页畔 ' + url)


if __name__ == '__main__':
    main()
