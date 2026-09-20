"""Windows curl/Schannel fallback for Python TLS handshake failures only."""
import io
import os
from pathlib import Path
import subprocess
import tempfile

import requests


def fetch(url, headers, cookies=None):
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'curl.exe'
    if os.name != 'nt' or not executable.is_file():
        raise requests.exceptions.SSLError('Windows HTTPS fallback is unavailable')
    # Never follow redirects here: net.fetch_image checks every next address.
    # -q ignores local curl configuration; certificate checks stay enabled.
    with tempfile.TemporaryDirectory(prefix='crawler-tls-') as directory:
        header_path = Path(directory) / 'headers'
        body_path = Path(directory) / 'body'
        args = [str(executable), '-q', '--silent', '--show-error',
                '--proto', '=http,https', '--connect-timeout', '15', '--max-time', '120',
                '--dump-header', str(header_path), '--output', str(body_path)]
        for key, value in headers.items():
            if '\r' in str(value) or '\n' in str(value):
                raise requests.exceptions.InvalidHeader('Invalid header value')
            args.extend(['--header', f'{key}: {value}'])
        if cookies:
            prepared = requests.Request('GET', url, cookies=cookies).prepare()
            cookie = prepared.headers.get('Cookie')
            if cookie:
                args.extend(['--header', f'Cookie: {cookie}'])
        args.extend(['--url', url])
        try:
            result = subprocess.run(args, capture_output=True, timeout=125,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise requests.exceptions.Timeout('Windows HTTPS fallback timed out') from exc
        if result.returncode:
            # Do not expose URLs with temporary keys from curl stderr.
            raise requests.exceptions.ConnectionError(f'Windows HTTPS fallback failed (curl {result.returncode})')
        response = requests.Response()
        response.url = url
        for line in header_path.read_text(encoding='iso-8859-1').splitlines():
            if line.startswith('HTTP/'):
                response.status_code = int(line.split()[1])
                response.headers.clear()
            elif ':' in line:
                key, value = line.split(':', 1)
                response.headers[key] = value.strip()
        response._content = body_path.read_bytes()
        response._content_consumed = True
        response.raw = io.BytesIO(response._content)
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        return response
