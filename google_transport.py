"""Bounded Windows Schannel transport for Google OAuth and APIs.

Credentials travel through stdin, never command-line arguments or logs.
Certificate verification remains enabled; redirects are not followed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

import httplib2
import requests


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t') + '"'


def native_request(method, url, headers=None, body=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.googleapis.com') or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Google transport requires a Google HTTPS endpoint')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'curl.exe'
    config = ['url = ' + _quote(url), 'request = ' + _quote(method), 'header = "Accept-Encoding: identity"']
    for key, value in (headers or {}).items():
        if key.lower() == 'accept-encoding':
            continue
        if any(c in str(key) + str(value) for c in '\r\n'):
            raise ValueError('Invalid Google request header')
        config.append('header = ' + _quote(f'{key}: {value}'))
    if hasattr(body, 'read'):
        body = body.read()
    with tempfile.TemporaryDirectory(prefix='google-http-') as directory:
        header_path = Path(directory) / 'headers'
        if body is not None:
            if isinstance(body, bytes):
                try:
                    body = body.decode('utf-8')
                except UnicodeDecodeError:
                    pass
            if isinstance(body, bytes) or '\x00' in body:
                payload = Path(directory) / 'upload'
                payload.write_bytes(body if isinstance(body, bytes) else body.encode('utf-8'))
                config.append('data-binary = ' + _quote('@' + str(payload)))
            else:
                # OAuth form bodies and Sheets JSON remain entirely in memory.
                config.append('data-binary = ' + _quote(body))
        try:
            result = subprocess.run(
                [str(executable), '-q', '--silent', '--show-error', '--proto', '=https',
                 '--connect-timeout', '10', '--max-time', '45', '--dump-header', str(header_path),
                 '--config', '-'], input=('\n'.join(config) + '\n').encode('utf-8'),
                capture_output=True, timeout=50, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise requests.exceptions.Timeout('Google connection timed out') from None
        if result.returncode:
            raise requests.exceptions.ConnectionError(f'Google connection failed (curl {result.returncode})')
        response = requests.Response()
        response.url = url
        for line in header_path.read_text(encoding='iso-8859-1').splitlines():
            if line.startswith('HTTP/'):
                response.status_code = int(line.split()[1])
                response.headers.clear()
            elif ':' in line:
                key, value = line.split(':', 1)
                response.headers[key] = value.strip()
        response._content = result.stdout
        response._content_consumed = True
        return response


class GoogleAdapter(requests.adapters.BaseAdapter):
    def send(self, request, **kwargs):
        response = native_request(request.method, request.url, request.headers, request.body)
        response.request = request
        return response

    def close(self):
        pass


def configure_oauth(session):
    if os.name == 'nt':
        session.mount('https://oauth2.googleapis.com/', GoogleAdapter())


class GoogleHttp(httplib2.Http):
    def request(self, uri, method='GET', body=None, headers=None, **kwargs):
        response = native_request(method, uri, headers, body)
        return httplib2.Response({**response.headers, 'status': str(response.status_code)}), response.content


def make_http():
    return GoogleHttp(timeout=45) if os.name == 'nt' else httplib2.Http(timeout=45)
