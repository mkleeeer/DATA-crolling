import unittest
from unittest.mock import Mock, patch

import requests
import net
from test_resolver import response


class TransportTests(unittest.TestCase):
    def test_ssl_failure_uses_fallback_and_checks_redirect_before_fetch(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError('EOF')
        redirect = response('https://example.com/file', b'', 302,
                            {'Location': 'http://127.0.0.1/private'})
        def check(url):
            if '127.0.0.1' in url:
                raise net.BlockedURLError('blocked')
        with patch.object(net, '_session', return_value=session), \
             patch.object(net, 'assert_public_url', side_effect=check), \
             patch.object(net.tls_fallback, 'fetch', return_value=redirect) as fallback:
            with self.assertRaises(net.BlockedURLError):
                net.fetch_image('https://example.com/file')
            fallback.assert_called_once()

    def test_fallback_receives_original_headers_and_explicit_cookies(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError('EOF')
        doc = response('https://example.com/file', b'%PDF-1.6\nfixture')
        with patch.object(net, '_session', return_value=session), \
             patch.object(net, 'assert_public_url'), \
             patch.object(net.tls_fallback, 'fetch', return_value=doc) as fallback:
            result = net.fetch_image(doc.url, 'https://example.com/page', cookies={'session': 'fixture'})
            self.assertEqual(result.content, doc.content)
            self.assertEqual(fallback.call_args.args[1]['Referer'], 'https://example.com/page')
            self.assertEqual(fallback.call_args.args[2], {'session': 'fixture'})

    def test_http_error_does_not_trigger_tls_fallback(self):
        session = Mock()
        session.get.return_value = response('https://example.com/file', b'Forbidden', 403)
        with patch.object(net, '_session', return_value=session), \
             patch.object(net, 'assert_public_url'), \
             patch.object(net.tls_fallback, 'fetch') as fallback:
            self.assertEqual(net.fetch_image('https://example.com/file').status_code, 403)
            fallback.assert_not_called()
