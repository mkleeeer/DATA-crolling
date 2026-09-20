import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

import pipeline


class UnknownBinaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.diagnostics = self.root / "diagnostics"
        patches = [
            patch.dict(os.environ, {
                "UNKNOWN_BINARY_SAVE_RAW": "1",
                "UNKNOWN_BINARY_DIAGNOSTICS_DIR": str(self.diagnostics),
            }),
            patch.object(pipeline, "DOWNLOADS_DIR", self.root / "downloads"),
            patch.object(pipeline.db, "DB_PATH", self.root / "registry.db"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        # sqlite's context manager commits/rolls back but does not close.
        # Keep explicit ownership so Windows can remove the fixture DB.
        connections = []
        original_get_conn = pipeline.db.get_conn

        def tracked_connection():
            conn = original_get_conn()
            connections.append(conn)
            return conn

        connection_patch = patch.object(pipeline.db, "get_conn", side_effect=tracked_connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        self.addCleanup(lambda: [conn.close() for conn in connections])
        pipeline.db.init_db()

    def run_download(self, raw, content_type="application/octet-stream"):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://example.com/response"
        response._content = raw
        response.headers.update({"Content-Type": content_type,
                                 "Content-Length": str(len(raw)),
                                 "Content-Disposition": 'attachment; filename="sample.bin"'})
        hop = requests.Response()
        hop.status_code = 302
        hop.url = "https://example.com/request"
        response.history = [hop]
        output = io.StringIO()
        with patch.object(pipeline.net, "fetch_image", return_value=response), redirect_stdout(output):
            try:
                result = pipeline.download_and_process(hop.url)
            except pipeline.DownloadError as exc:
                result = exc
        return result, output.getvalue()

    def diagnostics_from(self, logs):
        prefix = "[pipeline] UNKNOWN_BINARY diagnostics: "
        return json.loads(next(line[len(prefix):] for line in logs.splitlines()
                               if line.startswith(prefix)))

    def test_text_response_preserves_full_body_and_exact_512_byte_samples(self):
        raw = b"Financial sample\r\n" + "한글".encode() + b"\xff\x00\x1b" + b"A" * 600
        result, logs = self.run_download(raw)
        self.assertIsInstance(result, pipeline.DownloadError)
        details = self.diagnostics_from(logs)
        self.assertEqual(details["sample_hex"], raw[:512].hex())
        self.assertEqual(details["sample_utf8"], raw[:512].decode("utf-8", errors="replace"))
        self.assertEqual(details["sample_latin1"], raw[:512].decode("latin-1"))
        self.assertEqual(details["sample_bytes"], 512)
        self.assertEqual(details["body_bytes"], len(raw))
        self.assertEqual(details["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(details["url"], "https://example.com/request")
        self.assertEqual(details["final_url"], "https://example.com/response")
        self.assertEqual(details["redirects"][0]["status"], 302)
        self.assertEqual(details["headers"]["Content-Length"], str(len(raw)))
        self.assertIn("46696e616e636961", str(result))
        self.assertNotIn("\x1b", logs)
        saved = list(self.diagnostics.glob("*.bin"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), raw)
        self.assertIn(str(saved[0]), str(result))
        with pipeline.db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)

    def test_empty_short_and_split_utf8_samples(self):
        for raw in (b"", b"Financial", b"A" * 511 + "가".encode()):
            with self.subTest(length=len(raw)):
                result, logs = self.run_download(raw)
                self.assertIsInstance(result, pipeline.DownloadError)
                self.assertEqual(self.diagnostics_from(logs)["sample_bytes"], min(512, len(raw)))
        saved = list(self.diagnostics.glob("*.bin"))
        self.assertEqual(len(saved), 3)
        self.assertEqual({p.read_bytes() for p in saved}, {b"", b"Financial", b"A" * 511 + "가".encode()})

    def test_saving_disabled_still_logs_body(self):
        with patch.dict(os.environ, {"UNKNOWN_BINARY_SAVE_RAW": "0"}):
            result, logs = self.run_download(b"Financial sample")
        self.assertIn("UNKNOWN_BINARY_SAVE_RAW=0", str(result))
        self.assertEqual(self.diagnostics_from(logs)["sample_utf8"], "Financial sample")
        self.assertFalse(self.diagnostics.exists())

    def test_save_failure_keeps_diagnostics_and_download_error(self):
        with patch.object(pipeline.tempfile, "NamedTemporaryFile", side_effect=OSError("disk full")):
            result, logs = self.run_download(b"Financial sample")
        self.assertIsInstance(result, pipeline.DownloadError)
        self.assertIn("원본 임시 저장 실패", str(result))
        self.assertIn("disk full", logs)
        self.assertEqual(self.diagnostics_from(logs)["sample_utf8"], "Financial sample")

    def test_default_saves_in_system_temp_directory(self):
        with patch.dict(os.environ):
            os.environ.pop("UNKNOWN_BINARY_DIAGNOSTICS_DIR", None)
            os.environ.pop("UNKNOWN_BINARY_SAVE_RAW", None)
            with patch.object(pipeline.tempfile, "gettempdir", return_value=str(self.root)):
                result, logs = self.run_download(b"Financial sample")
        saved = list((self.root / "image-crawler-diagnostics").glob("*.bin"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"Financial sample")
        self.assertIn(str(saved[0]), str(result))

    def test_supported_files_keep_normal_download_path(self):
        png = io.BytesIO()
        Image.new("RGB", (3, 2)).save(png, format="PNG")
        cases = [(png.getvalue(), "image/png"), (b"%PDF-1.6\nexample", "application/pdf"),
                 (b"PK\x05\x06" + b"\x00" * 18, "application/zip"),
                 (b"PK\x03\x04mimetypeapplication/epub+zip", "application/epub+zip"),
                 (b"AT&TFORMexample", "image/vnd.djvu")]
        for raw, mime in cases:
            with self.subTest(mime=mime):
                result, logs = self.run_download(raw)
                self.assertIsInstance(result, dict)
                self.assertEqual(result["mime_type"], mime)
                self.assertEqual((pipeline.DOWNLOADS_DIR / result["local_path"]).read_bytes(), raw)
                self.assertNotIn("UNKNOWN_BINARY", logs)
        self.assertFalse(self.diagnostics.exists())


if __name__ == "__main__":
    unittest.main()
