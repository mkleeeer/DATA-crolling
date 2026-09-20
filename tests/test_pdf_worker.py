import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pdf_worker
import sheets
from queue_config import PDF_SHEET_NAME, SPREADSHEET_ID
import test_unknown_binary


class PdfQueueTests(unittest.TestCase):
    setUp = test_unknown_binary.UnknownBinaryTests.setUp

    def run_rows(self, rows, download=None, update=None, stop=None):
        with patch.object(sheets, 'read_rows', return_value=rows) as read, \
             patch.object(sheets, 'update_row', side_effect=update) as write, \
             patch.object(pdf_worker.pipeline, 'download_and_process', side_effect=download) as fetch, \
             patch.object(pdf_worker.time, 'sleep'):
            count = pdf_worker.run_once(stop)
        read.assert_called_once_with(SPREADSHEET_ID, PDF_SHEET_NAME, sheets.PDF_HEADERS)
        return count, write, fetch

    def record(self):
        return {'id': 'file1', 'filename': '01.pdf', 'local_path': 'PDF/01.pdf', 'md5': 'a' * 32}

    def test_url_only_and_pending_download_without_consuming_terminal_rows(self):
        rows = [{'_row_number': 2, 'url': 'https://example.com/a.pdf'},
                {'_row_number': 3, 'url': ''},
                {'_row_number': 4, 'url': 'https://example.com/b', 'status': 'pending', 'folder': 'Books', 'expected_md5': 'b' * 32}]
        rows += [{'_row_number': i + 5, 'url': 'https://example.com/c', 'status': s}
                 for i, s in enumerate(['downloaded', 'failed', 'downloading', 'discarded'])]
        count, write, fetch = self.run_rows(rows, download=[self.record(), self.record()])
        self.assertEqual(count, 2)
        self.assertEqual(fetch.call_count, 2)
        fetch.assert_any_call('https://example.com/a.pdf', folder='PDF', expected_md5='')
        fetch.assert_any_call('https://example.com/b', folder='Books', expected_md5='b' * 32)
        result = write.call_args_list[1].args[3]
        self.assertEqual(result['status'], 'downloaded')
        self.assertTrue(Path(result['local_path']).is_absolute())
        self.assertEqual(result['file_id'], 'file1')

    def test_download_failure_is_recorded_and_next_row_continues(self):
        rows = [{'_row_number': n, 'url': 'https://example.com/a'} for n in [2, 3]]
        count, write, fetch = self.run_rows(rows, download=[ValueError('checksum mismatch'), self.record()])
        self.assertEqual(count, 2)
        self.assertEqual(write.call_args_list[1].args[3]['error'], 'checksum mismatch')
        self.assertEqual([r['status'] for r in rows], ['failed', 'downloaded'])

    def test_result_write_failure_does_not_mark_download_failed_or_retry(self):
        rows = [{'_row_number': 2, 'url': 'https://example.com/a'}]
        count, write, fetch = self.run_rows(rows, download=[self.record()], update=[None, RuntimeError('offline')])
        self.assertEqual(rows[0]['status'], 'downloading')
        self.assertEqual(write.call_count, 2)
        self.assertIn('2행', pdf_worker.status()['error'])
        count, write, fetch = self.run_rows(rows)
        self.assertEqual(count, 0)
        fetch.assert_not_called()

    def test_stop_and_concurrent_pass_do_not_download(self):
        stop = threading.Event()
        stop.set()
        count, write, fetch = self.run_rows([{'_row_number': 2, 'url': 'https://example.com/a'}], stop=stop)
        self.assertEqual(count, 0)
        fetch.assert_not_called()
        with pdf_worker._run_lock, patch.object(sheets, 'read_rows') as read:
            self.assertEqual(pdf_worker.run_once(), 0)
            read.assert_not_called()

    def test_web_add_uses_pdf_tab_and_deduplicates_batch(self):
        import app
        with app.app.test_client() as client, patch.object(sheets, 'append_rows') as append:
            res = client.post('/api/pdf-queue/add', json={'urls': ['https://example.com/a', 'https://example.com/a']})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json['added'], 1)
            append.assert_called_once_with(SPREADSHEET_ID, PDF_SHEET_NAME,
                [{'url': 'https://example.com/a', 'folder': 'PDF', 'status': 'pending'}], sheets.PDF_HEADERS)
            append.reset_mock()
            for urls in [[], ['javascript:alert(1)'], ['https://example.com/a'] * 201, [' ']]:
                self.assertEqual(client.post('/api/pdf-queue/add', json={'urls': urls}).status_code, 400)
            append.assert_not_called()
            page = client.get('/pdf').get_data(as_text=True)
            self.assertIn('PDF 시트 자동 다운로드', page)
            self.assertNotIn('/api/submissions/add', page)

    def test_link_metadata_reaches_pdf_worker_without_moving_legacy_columns(self):
        import app
        self.assertEqual(sheets.PDF_HEADERS[:10], [
            'url', 'folder', 'expected_md5', 'status', 'filename', 'local_path',
            'file_id', 'md5', 'error', 'updated_at',
        ])
        submission = {
            'url': 'https://example.com/download', 'folder': 'Reports',
            'source_page': 'https://example.com/posts/1', 'title': '경제전망 2026.pdf',
        }
        with app.app.test_client() as client, patch.object(sheets, 'append_rows') as append:
            reply = client.post('/api/pdf-queue/add', json={'submissions': [submission]})
        self.assertEqual(reply.status_code, 200)
        queued = append.call_args.args[2][0]
        self.assertEqual(queued['source_page'], submission['source_page'])
        self.assertEqual(queued['title'], submission['title'])

        row = {'_row_number': 2, **queued}
        count, _, fetch = self.run_rows([row], download=[self.record()])
        self.assertEqual(count, 1)
        fetch.assert_called_once_with(
            submission['url'], folder='Reports', expected_md5='',
            source_page=submission['source_page'], title=submission['title'],
        )


if __name__ == '__main__':
    unittest.main()
