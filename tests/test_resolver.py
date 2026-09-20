import hashlib
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests

import net
import pipeline
import resolver
import scrape
import test_unknown_binary


def response(url, body, status=200, headers=None):
    result = requests.Response()
    result.url = url
    result.status_code = status
    result._content = body.encode() if isinstance(body, str) else body
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class ResolverTests(unittest.TestCase):
    def test_malformed_peer_links_do_not_hide_valid_downloads(self):
        html = b'''<base href="http://[bad"><a href="ed2k://|file|Book[1].pdf|123|/">Ed2k</a>
        <a href="http://[bad">Download</a><a href="https://example.com:bad/file">Download</a>
        <a href="/get.php?md5=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">GET</a>'''
        base = "https://example.com/book"
        expected = base.replace("/book", "/get.php?md5=" + "a" * 32)
        self.assertEqual(resolver.download_links(html, base), [expected])
        self.assertEqual(scrape.extract_links_from_html(html.decode(), base), [{"url": expected, "text": "GET"}])

    def test_direct_file_precedes_navigation_and_candidate_limit(self):
        html = ('<a href="/mirrors.php">Mirrors</a><a href="/setlang?md5=' + "a" * 32 + '&lang=ru">RU</a>'
                + ''.join(f'<a href="/alternate/{i}">Mirror {i}</a>' for i in range(12))
                + '<a href="/get.php">GET</a>')
        links = resolver.download_links(html.encode(), "https://example.com/book", limit=8)
        self.assertEqual(links[0], "https://example.com/get.php")
        self.assertEqual(len(links), 8)
        self.assertFalse(any("mirrors.php" in url or "setlang" in url for url in links))

    def test_multihop_mirror_fallback_and_checksum(self):
        raw = b"%PDF-1.6\ncorrect document"
        md5 = hashlib.md5(raw).hexdigest()
        start = f"https://example.com/md5/{md5}"
        pages = {
            "https://example.com/mirror1": response("https://example.com/mirror1", 'Financial <a href="/get/wrong">GET</a>'),
            "https://example.com/get/wrong": response("https://example.com/get/wrong", b"%PDF-1.6\nwrong file"),
            "https://example.com/mirror2": response("https://example.com/mirror2", b"Financial temporary failure"),
            "https://example.com/mirror3": response("https://example.com/mirror3", '<meta http-equiv="refresh" content="0; url=/get/right">'),
            "https://example.com/get/right": response("https://example.com/get/right", raw),
        }
        fetch = Mock(side_effect=lambda url, parent: pages[url])
        diagnose = Mock(return_value="saved diagnostic.bin")
        logs = io.StringIO()
        with redirect_stdout(logs):
            found, actual, expected = resolver.resolve(
                response(start, '<a href="/mirror1">Mirror1</a><a href="/mirror2">Mirror2</a><a href="/mirror3">Mirror3</a>'),
                start, fetch, diagnose)
        self.assertEqual(found.content, raw)
        self.assertEqual((actual, expected), (md5, md5))
        diagnose.assert_called_once()
        self.assertIn("MD5_MISMATCH", logs.getvalue())
        fetch.assert_any_call("https://example.com/get/right", "https://example.com/mirror3")

    def test_http_failure_tries_next_mirror(self):
        initial = response("https://example.com/start", '<a href="/download/a">Download</a><a href="/download/b">Download</a>')
        fetch = Mock(side_effect=[pipeline.DownloadError("403 forbidden"), response("https://example.com/download/b", b"AT&TFORMexample")])
        with redirect_stdout(io.StringIO()):
            found, _, _ = resolver.resolve(initial, initial.url, fetch, Mock())
        self.assertEqual(found.url, "https://example.com/download/b")
        self.assertEqual(fetch.call_count, 2)

    def test_explicit_identifier_only_and_case_normalization(self):
        value = "abcdef12" * 4
        self.assertEqual(resolver.url_md5(f"https://example.com/get?md5={value.upper()}"), value)
        self.assertEqual(resolver.url_md5(f"https://example.com/file/{value}"), "")
        self.assertEqual(resolver.url_md5(f"https://example.com/?id={value}"), "")
        with self.assertRaises(resolver.ResolutionError):
            resolver.url_md5("https://example.com/?md5=bad")

    def test_conflicting_identifier_skips_candidate_without_fetch(self):
        start = "https://example.com/?md5=" + "a" * 32
        initial = response(start, '<a href="/download?md5=' + "b" * 32 + '">Mirror</a>')
        fetch = Mock()
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(resolver.ResolutionError, "MD5_ID_MISMATCH"):
            resolver.resolve(initial, start, fetch, Mock())
        fetch.assert_not_called()

    def test_loop_and_request_depth_limits(self):
        initial = response("https://example.com/start", '<a href="/download/next">Download</a>')
        looping = Mock(return_value=response("https://example.com/download/next", '<a href="/start">Download</a>'))
        with redirect_stdout(io.StringIO()), self.assertRaises(resolver.ResolutionError):
            resolver.resolve(initial, initial.url, looping, Mock())
        self.assertEqual(looping.call_count, 1)
        for kwargs, reason in [({"max_depth": 0}, "DEPTH_LIMIT"), ({"max_requests": 0}, "REQUEST_LIMIT")]:
            with self.subTest(reason=reason), self.assertRaisesRegex(resolver.ResolutionError, reason):
                resolver.resolve(initial, initial.url, Mock(), Mock(), **kwargs)

    def test_link_selection_preserves_query_and_skips_other_content(self):
        html = '''<base href="https://example.com/files/">
        <a href="book?id=7&md5=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Book</a>
        <a href="next">Mirror 2</a><a href="paper.epub">EPUB</a>
        <a href="https://other.example/file" download>save</a>
        <a href="/login">Login</a><a href="javascript:alert(1)">Download</a>'''
        links = resolver.download_links(html.encode(), "https://example.com/start")
        self.assertEqual(len(links), 4)
        self.assertIn("https://example.com/files/book?id=7&md5=" + "a" * 32, links)
        self.assertIn("https://other.example/file", links)


class PipelineResolverTests(unittest.TestCase):
    setUp = test_unknown_binary.UnknownBinaryTests.setUp

    def test_pipeline_saves_only_verified_final_file(self):
        raw = b"%PDF-1.6\nfixture"
        digest = hashlib.md5(raw).hexdigest()
        start = f"https://example.com/?md5={digest}"
        replies = [response(start, '<a href="/mirror">Mirror</a>'),
                   response("https://example.com/mirror", '<a href="/get.php">GET</a>'),
                   response("https://example.com/get.php", raw)]
        with patch.object(pipeline, "_fetch_url", side_effect=replies), redirect_stdout(io.StringIO()):
            result = pipeline.download_and_process(start)
        self.assertTrue(result["md5_verified"])
        self.assertEqual(result["source_page"], start)
        self.assertEqual(result["source_url"], replies[-1].url)
        self.assertEqual((pipeline.DOWNLOADS_DIR / result["local_path"]).read_bytes(), raw)

    def test_checksum_mismatch_never_registers_or_saves_file(self):
        with patch.object(pipeline, "_fetch_url", return_value=response("https://example.com/file", b"%PDF-1.6\nwrong")), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(pipeline.DownloadError, "MD5_MISMATCH"):
                pipeline.download_and_process("https://example.com/file", expected_md5="a" * 32)
        self.assertFalse(pipeline.DOWNLOADS_DIR.exists())
        with pipeline.db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_download_api_accepts_md5_and_displays_result(self):
        import app
        raw = b"%PDF-1.6\napi fixture"
        digest = hashlib.md5(raw).hexdigest()
        with patch.object(pipeline, "_fetch_url", return_value=response("https://example.com/file", raw)), redirect_stdout(io.StringIO()):
            reply = app.app.test_client().post("/api/images/download", json={"url": "https://example.com/file", "expected_md5": digest})
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json["md5_verified"])
        self.assertEqual(reply.json["md5"], digest)
        # Receiving a stored file must not re-fetch an expired remote URL.
        with patch.object(pipeline.net, "fetch_image") as fetch:
            saved = app.app.test_client().get(f"/api/files/{reply.json['file_id']}/download")
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.data, raw)
            self.assertIn("attachment", saved.headers["Content-Disposition"])
            saved.close()
            fetch.assert_not_called()

    def test_saved_file_route_rejects_unknown_and_outside_paths(self):
        import app
        client = app.app.test_client()
        self.assertEqual(client.get("/api/files/missing/download").status_code, 404)
        with patch.object(app.db, "get_image", return_value={"local_path": "../registry.db"}):
            self.assertEqual(client.get("/api/files/outside/download").status_code, 404)

    def test_link_api_ignores_ed2k_instead_of_returning_500(self):
        import app
        page = response("https://example.com/book", '<a href="ed2k://|file|Book[1].pdf|/">Ed2k</a><a href="/get.php">GET</a>')
        with patch.object(app.net, "fetch_page", return_value=page):
            reply = app.app.test_client().post("/api/links/extract", json={"url": page.url})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json["links"], [{"url": "https://example.com/get.php", "text": "GET"}])


class QueueResolverTests(unittest.TestCase):
    def test_landing_page_queues_one_original_url_instead_of_cover(self):
        import extractor_worker as worker
        url = "https://example.com/book"
        page = response(url, '<img src="cover.jpg"><a href="/download">Download</a>')
        with patch.object(worker.net, "fetch_page", return_value=page), patch.object(worker.settings, "get_only_og_image", return_value=False), patch.object(worker.sheets, "update_row"), patch.object(worker.sheets, "append_rows") as append, redirect_stdout(io.StringIO()):
            worker.process_submission({"url": url, "_row_number": 2, "kind": "file"})
        candidates = append.call_args.args[2]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_url"], url)

    def test_existing_image_submission_still_extracts_images(self):
        import extractor_worker as worker
        url = "https://example.com/article"
        page = response(url, '<img src="cover.jpg"><a href="/download">Download</a>')
        with patch.object(worker.net, "fetch_page", return_value=page), patch.object(worker.net, "probe_image_dimensions", return_value=(800, 600)), patch.object(worker.settings, "get_only_og_image", return_value=False), patch.object(worker.sheets, "update_row"), patch.object(worker.sheets, "append_rows") as append, redirect_stdout(io.StringIO()):
            worker.process_submission({"url": url, "_row_number": 2})
        self.assertEqual(append.call_args.args[2][0]["source_url"], "https://example.com/cover.jpg")


class RedirectTests(unittest.TestCase):
    def test_mirror_requests_skip_hidden_adapter_retries(self):
        with net._new_session(False) as session:
            self.assertEqual(session.get_adapter("https://example.com").max_retries.total, 0)
        with net._new_session() as session:
            self.assertEqual(session.get_adapter("https://example.com").max_retries.total, 4)
        session = Mock()
        session.get.return_value = response("https://example.com/file", b"", 429)
        with patch.object(net, "_session", return_value=session) as factory, patch.object(net, "assert_public_url"):
            with self.assertRaisesRegex(pipeline.DownloadError, "429"):
                pipeline._fetch_url("https://example.com/file", retry_requests=False)
        factory.assert_called_once_with(False)

    def test_private_redirect_checked_before_request(self):
        first = response("https://example.com/first", b"", 302, {"Location": "http://127.0.0.1/private"})
        session = Mock()
        session.get.return_value = first
        with patch.object(net, "_session", return_value=session), patch.object(net, "assert_public_url", side_effect=[None, None, net.BlockedURLError("private")]):
            with self.assertRaises(net.BlockedURLError):
                net.fetch_image(first.url)
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_cross_origin_redirect_does_not_forward_explicit_cookies(self):
        first = response("https://example.com/first", b"", 302, {"Location": "https://mirror.example/file"})
        last = response("https://mirror.example/file", b"%PDF-1.6")
        session = Mock()
        session.get.side_effect = [first, last]
        with patch.object(net, "_session", return_value=session), patch.object(net, "assert_public_url"):
            result = net.fetch_image(first.url, cookies={"fixture": "value"})
        self.assertEqual(session.get.call_args_list[0].kwargs["cookies"], {"fixture": "value"})
        self.assertIsNone(session.get.call_args_list[1].kwargs["cookies"])
        self.assertEqual(result.history, [first])


if __name__ == "__main__":
    unittest.main()
