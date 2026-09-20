import hashlib
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import drive
import pipeline
import sheets
from test_resolver import response
import test_unknown_binary


class IntegrationTests(unittest.TestCase):
    setUp = test_unknown_binary.UnknownBinaryTests.setUp

    def test_landing_page_filename_and_duplicate_checksum(self):
        raw = b'%PDF-1.6\nfilename integration fixture'
        url = 'https://example.com/landing'
        final = 'https://example.com/get/0123456789abcdef0123456789abcdef'
        replies = [response(url, f'<a href="{final}">Economic report.pdf</a>'), response(final, raw)]
        with patch.object(pipeline, '_fetch_url', side_effect=replies), redirect_stdout(io.StringIO()):
            saved = pipeline.download_and_process(url)
        self.assertEqual(saved['filename'], 'Economic report.pdf')
        with patch.object(pipeline, '_fetch_url') as fetch, redirect_stdout(io.StringIO()):
            duplicate = pipeline.download_and_process(final, expected_md5=hashlib.md5(raw).hexdigest())
            self.assertTrue(duplicate['duplicate'])
            self.assertTrue(duplicate['md5_verified'])
            with self.assertRaisesRegex(pipeline.DownloadError, 'MD5_MISMATCH'):
                pipeline.download_and_process(final, expected_md5='0' * 32)
            fetch.assert_not_called()

    def test_queue_rejects_non_string_urls_without_server_error(self):
        import app
        with app.app.test_client() as client, patch.object(sheets, 'append_rows') as append:
            for value in [1, {}, [], None]:
                reply = client.post('/api/pdf-queue/add', json={'urls': [value]})
                self.assertEqual(reply.status_code, 400)
            append.assert_not_called()

    def test_worker_never_launches_interactive_login(self):
        with patch.object(drive, 'CLIENT_SECRET_FILE') as secret, \
             patch.object(drive, 'TOKEN_FILE') as token, \
             patch.object(drive, 'InstalledAppFlow') as flow, \
             patch.dict(drive._auth_state, {'running': False}):
            secret.exists.return_value = True
            token.exists.return_value = False
            with self.assertRaisesRegex(drive.DriveNotConfigured, 'Google'):
                drive.get_credentials()
            flow.from_client_secrets_file.assert_not_called()

    def test_auth_wait_does_not_block_queue(self):
        with patch.dict(drive._auth_state, {'running': True}):
            with self.assertRaisesRegex(drive.DriveNotConfigured, '승인 대기'):
                drive.get_credentials()
