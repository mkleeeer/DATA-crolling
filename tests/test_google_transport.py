import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import google_transport as transport


class GoogleTransportTests(unittest.TestCase):
    def test_secrets_use_stdin_and_certificates_stay_enabled(self):
        def run(args, **kwargs):
            self.assertNotIn('secret-token', ' '.join(args))
            self.assertNotIn('--insecure', args)
            self.assertNotIn('--location', args)
            self.assertIn(b'Authorization: Bearer secret-token', kwargs['input'])
            self.assertIn(b'code=secret-code', kwargs['input'])
            self.assertIn(b'Accept-Encoding: identity', kwargs['input'])
            self.assertNotIn(b'Accept-Encoding: gzip', kwargs['input'])
            Path(args[args.index('--dump-header') + 1]).write_bytes(
                b'HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 400\r\nContent-Type: application/json\r\n\r\n')
            return subprocess.CompletedProcess(args, 0, b'{"error":"invalid_grant"}', b'')
        with patch.object(transport.subprocess, 'run', side_effect=run):
            response = transport.native_request('POST', 'https://oauth2.googleapis.com/token',
                {'Authorization': 'Bearer secret-token', 'Accept-Encoding': 'gzip'}, 'code=secret-code')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'invalid_grant')

    def test_rejects_non_google_or_insecure_endpoint(self):
        for url in ['https://evil.test/token', 'http://oauth2.googleapis.com/token',
                    'https://oauth2.googleapis.com.evil.test/token']:
            with self.assertRaises(ValueError):
                transport.native_request('GET', url)

    def test_errors_do_not_expose_credentials(self):
        with patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 35, b'', b'secret-code')):
            with self.assertRaisesRegex(Exception, r'curl 35') as error:
                transport.native_request('POST', 'https://oauth2.googleapis.com/token', body='code=secret-code')
        self.assertNotIn('secret-code', str(error.exception))

    def test_httplib_response_compatible(self):
        import requests
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"ok": true}'
        with patch.object(transport, 'native_request', return_value=response):
            headers, body = transport.GoogleHttp().request('https://sheets.googleapis.com/v4/spreadsheets/x')
        self.assertEqual(headers.status, 200)
        self.assertEqual(body, response.content)


if __name__ == '__main__':
    unittest.main()
