# Development log

## 2026-09-11 (KST) — Preserve UNKNOWN_BINARY evidence

- Report: an unrecognized response had signature `46696e616e636961`
  (`Financia`). `pipeline.py` logged only eight bytes and immediately reported
  an unsupported format, losing the evidence needed to diagnose the response.
- Change: log the first 512 bytes as hex, UTF-8 and latin-1, plus response
  metadata, redirect history, total byte count, SHA-256 and decoder exception
  type. Preserve all response.content bytes in a unique temporary .bin by
  default and include its path in the error. Saving can be disabled or directed
  to a chosen directory. See [usage](unknown-binary-diagnostics.md).
- Environment: system Python lacked BeautifulSoup (`ModuleNotFoundError:
  No module named 'bs4'`). Created an ignored `venv` and installed the
  requests/BeautifulSoup/Pillow versions pinned in requirements.txt for testing.
- Initial command: `venv\Scripts\python.exe -m unittest discover -s tests -v`.
  Five cases failed during fixture cleanup with `PermissionError: [WinError 32]`
  on the temporary registry.db. SQLite connection context managers do not close
  connections, and retained exception tracebacks kept the connections alive.
  The fixture now tracks and explicitly closes its own DB connections before
  temporary-directory cleanup; production DB behavior is outside this change.
- Verification: the same command passes all 6 tests, covering exact 512-byte
  previews, full-body preservation, text starting with Financia, invalid/split
  UTF-8, empty input, unique filenames, default temp location, disabled saving,
  filesystem failure, and unchanged PNG/PDF/ZIP/EPUB/DjVu download paths.
  `git diff --check` passes. Tests use generated payloads and isolated SQLite
  files; no Google Sheets/Drive data is modified.
- Limitation: the original failing URL/full response was not supplied, so its
  upstream cause remains undetermined. Reprocessing it with this version will
  produce the evidence needed for diagnosis.
- Commit setup: `git commit` initially reported `Author identity unknown`.
  Used per-command agent identity `Codex <codex@localhost>` for this commit;
  global Git settings and the user's identity were not changed.

## 2026-09-11 00:30 KST — Resolve intermediate pages and mirrors

- Confirmed the old resolver inspected only .pdf links on one HTML page and
  accepted only an immediately returned PDF. It had no MD5 identity validation
  or traversal of intermediate/mirror pages. The queue treated description
  pages as image pages and could select a cover instead of the requested file.
- Added bounded traversal of advertised file/mirror links, fallback after
  HTTP/unknown-body/checksum failures, explicit MD5 validation, resolution logs,
  and UI inputs/results. File-mode queue rows retain the original URL for the
  download worker. Existing image rows keep their previous behavior.
- Each HTTP download redirect is checked before fetching; explicit caller
  cookies are not sent to another origin. No site-specific endpoints are guessed.
- First resolver test run: 16 passed, two redirect fixtures failed at
  `Response.close()` because their fabricated responses had no raw stream and
  did not mark their supplied body consumed. Marked fixture content consumed,
  matching the non-streaming requests responses represented by these tests.
- Validation: `venv\Scripts\python.exe -m unittest discover -s tests -v`
  passes all 19 tests, including multihop fallback, MD5 mismatch, preserved
  source/final URLs, API/queue integration, image-row compatibility, cycles,
  traversal limits, and redirect address/cookie checks. `git diff --check` passes.
- Runtime verification: restarted the local server and confirmed the updated
  URL/MD5 controls in the browser, preserving the user's existing URL input.
  That input previously failed with a final-CDN read timeout. A failed direct
  endpoint cannot supply an alternative mirror list; the original description
  page is needed. No user URL, temporary key or response payload was added to Git.
- See [resolver behavior and limits](file-resolver.md). Generated fixtures
  establish behavior; the external description/mirror page and successful
  download have not been verified.

## 2026-09-11 — Recover from malformed links and expose saved files

- Runtime failures: both POST `/api/images/download` and `/api/links/extract`
  returned HTTP 500 with `ValueError: Invalid IPv6 URL`. A real description
  page contained an ed2k link with square brackets in its filename. urljoin
  attempted to parse that unsupported scheme before the link filter ran.
- Added shared HTTP(S) link validation before parsing candidates. Malformed
  authorities, invalid ports, unsupported schemes and bad base URLs no longer
  abort processing of the remaining valid links.
- Runtime resolver logs also showed a global Mirrors directory and a language
  switch being visited ahead of GET. Navigation/language links are excluded;
  direct file/GET links are ranked before other candidates, before the cap.
- The registry contained a successfully downloaded PDF, but the web list only
  showed its filename. Added `/api/files/<id>/download` and visible file-download
  links. The route serves the stored bytes without revisiting an expiring remote
  link and restricts resolved paths to the configured download directory.
- Validation: `venv\Scripts\python.exe -m unittest discover -s tests -v`
  passes all 23 tests. Includes ed2k/invalid-HTTP fixtures, link API regression,
  candidate ranking, stored-file responses and out-of-directory rejection.
  `git diff --check` passes. Live saved-file GET returned HTTP 200, attachment
  disposition, and the expected 838292 bytes with matching MD5.
- User URL query keys and actual response bodies are excluded from this log.

## 2026-09-11 — Avoid nested retries while trying mirrors

- A live POST to the local downloader exceeded the test client's 300-second
  timeout while the resolver was waiting on a file candidate. The existing
  adapter could retry a candidate four times (and honor upstream Retry-After)
  before the resolver ever saw its failure. The exact upstream wait mechanism
  was not established by the local timeout alone.
- Mirror candidates now use a separate per-thread session with adapter retries
  disabled. The resolver handles fallback to the next advertised mirror. Direct
  requests keep their existing retry behavior. This does not add a total wall
  clock deadline: socket timeouts, DNS and slow transfers still affect duration.
- Validation: all 24 unittest cases pass, including the no-retry mirror policy
  and returning a 429 failure to the resolver without retrying the same host.
- Live retry after restart returned a handled RESOLUTION_FAILED response:
  the advertised download CDN returned HTTP 503, and the alternate mirror
  connection was reset by the remote host. The requested book was not saved.
  These upstream failures remain outside the repaired link-processing path.

## 2026-09-11 01:23 KST — Separate PDF sheet and automatic processing

- The PDF page previously submitted document links into the image submissions
  queue and required extractor/download passes. Added a URL-first `pdfs` tab,
  an independent PDF worker, automatic startup with app.py, status controls,
  and multiline/Link Gopher additions targeting only the PDF queue.
- A completed file followed by a failed Sheets result write must not be treated
  as a download failure or retried automatically. Such rows retain downloading;
  the UI reports the result-write error. Tests cover this failure and continuing
  after an ordinary download failure, terminal rows, stop, and concurrent passes.
- Reused existing local OAuth credentials; verified both credential files are
  ignored. Native Sheets API confirmed headers, notes, widths and frozen row.
  Browser visual inspection of the native tab was blocked by a Google sign-in
  page; no credentials were entered. Local webapp rendered the PDF controls.
- Live automatic test: entered the user-provided URL into A2 only, launched the
  app, and observed downloading then downloaded without a manual run-once call.
  The stored PDF is 838292 bytes and its MD5 matches the URL identifier. Image
  workers remained stopped. User URL query keys are omitted from tracked files.
- Validation: all 29 tests pass with
  `venv\Scripts\python.exe -m unittest discover -s tests -v`.

## 2026-09-11 01:40 KST — Manual PDF queue processing

- Added a PDF-page button using the existing exclusive run-once endpoint to
  read saved sheet rows immediately, including when automatic processing is off.
  UI distinguishes no pending work, handled rows, failures and an already busy
  worker. It disables the button during the request and refreshes status/files.
- Validation: 29 existing unit tests pass; Flask test client renders the new
  button, returns 200 for a manual empty pass and 409 while the worker is busy.
  `git diff --check` passes.
- Deployment limitation: the running Flask process caches the old template.
  Automatic approval review rejected the guarded stop/restart command as
  blocked by policy, without a more specific reason. The command did not run;
  existing server and automatic worker remain active. Application restart is
  required for the button to appear in that process.

## 2026-09-11 02:07 KST — Separate PDF automatic controls

- Replaced the changing-label toggle with persistent automatic start and stop
  buttons beside the manual run-once button. Controls reflect the current state
  and are disabled during changes or when the server state is unavailable.
- Validation: all 29 unit tests pass. Flask test-client checks confirm separate
  controls render, stop clears running state and start restores it without
  contacting remote services. JavaScript syntax and Git whitespace checks pass.
- A guarded restart of the current local app was again rejected by automatic
  approval review as blocked by policy, with no specific reason. The command
  did not run. The live app still has the functional single start/stop toggle;
  separate buttons require an application restart.

## 2026-09-20 — Integrate main crawler improvements with the PDF resolver

- Merged the main branch's three-tab link finder, post-list bulk collection,
  original filename selection, and content-based duplicate prevention with the
  dedicated PDF sheet worker, mirror traversal, MD5 checks, saved-file route,
  and unknown-response diagnostics.
- Link-finder selections now enter the PDF sheet directly and retain their
  source page and human-readable title. The PDF worker forwards that metadata
  to the shared pipeline, then immediately runs one pass without requiring the
  image extractor/download workers.
- Preserved the existing PDF sheet's A–J layout; `source_page` and `title` are
  appended as K/L. Existing sheets therefore keep their status/result columns.
- Reused the shared safe HTTP-link validator in file and post-list detection so
  malformed peer-to-peer or invalid-authority links cannot turn a page scan
  into an HTTP 500 response.
- Validation: all 30 unittest cases pass, including a new integration test for
  metadata flow and legacy sheet-column compatibility. Python compilation and
  `git diff --check` also pass.
