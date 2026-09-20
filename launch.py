"""Start the local app independently of a terminal, then open its PDF page."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:5000'


def ready():
    try:
        with urlopen(URL + '/api/workers/status', timeout=1) as response:
            return 'pdf' in json.load(response)
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not ready():
        python = Path(sys.executable)
        flags = 0
        if sys.platform == 'win32':
            python = python.with_name('pythonw.exe')
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        with (ROOT / 'server.log').open('ab') as log:
            process = subprocess.Popen(
                [str(python), '-u', str(ROOT / 'app.py')], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=flags, close_fds=True,
            )
        deadline = time.monotonic() + 30
        while not ready():
            if process.poll() is not None or time.monotonic() >= deadline:
                print('Could not start the crawler. See server.log in the app folder.')
                return 1
            time.sleep(0.3)
    if not args.no_browser:
        webbrowser.open(URL + '/pdf')
    print('PDF crawler ready: ' + URL + '/pdf')
    return 0


if __name__ == '__main__':
    sys.exit(main())
