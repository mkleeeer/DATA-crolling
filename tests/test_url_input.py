import unittest
from unittest.mock import patch
from url_input import extract_urls


class PasteTests(unittest.TestCase):
    def test_mixed_notes_preserve_keys_and_balanced_parentheses(self):
        text = '''1. 보고서 [받기](https://example.com/get?md5=abc&key=A%2FB%3D)
        | 설명 | https://example.com/report(2026).pdf | 20쪽 |
        <a href="https://example.com/get?md5=abc&amp;key=A%2FB%3D">중복</a>
        참고: (https://example.com/other.pdf). 끝'''
        self.assertEqual(extract_urls(text), [
            'https://example.com/get?md5=abc&key=A%2FB%3D',
            'https://example.com/report(2026).pdf', 'https://example.com/other.pdf'])

    def test_bad_link_does_not_hide_valid_peers(self):
        self.assertEqual(extract_urls('잡담 https://[bad ftp://example.com/a https://example.com/b'),
                         ['https://example.com/b'])

    def test_api_registers_only_extracted_urls(self):
        import app
        with app.app.test_client() as client, patch.object(app.sheets, 'append_rows') as append:
            reply = client.post('/api/pdf-queue/add', json={'text': '제목 https://example.com/a 설명 https://example.com/b', 'folder': 'Reports'})
            self.assertEqual(reply.status_code, 200)
            rows = append.call_args.args[2]
            self.assertEqual([r['url'] for r in rows], ['https://example.com/a', 'https://example.com/b'])
            self.assertTrue(all(r['folder'] == 'Reports' for r in rows))
            append.reset_mock()
            self.assertEqual(client.post('/api/pdf-queue/add', json={'text': '설명만 있음'}).status_code, 400)
            append.assert_not_called()
