import io
import os
import re
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, render_template, send_file, Response, abort

import db
import download_worker
import drive
import extractor_worker
import listing
import net
import pipeline
import pdf_worker
import settings
import sheets
from url_input import extract_urls
from queue_config import POLL_SECONDS, SPREADSHEET_ID, SPREADSHEET_URL, PDF_SHEET_NAME, PDF_SHEET_URL
from scrape import (
    extract_file_links, extract_images_from_html, extract_links_from_html, file_ext_of, file_name_key,
    filter_navigation_links, http_link,
)

app = Flask(__name__)
db.init_db()

# ---------------------------------------------------------------------------
# Background queue workers, controlled from the web UI instead of running as
# always-on console windows. Image workers start on demand; PDF starts with app.py.
# ---------------------------------------------------------------------------

_workers = {
    "extractor": {"thread": None, "stop": None, "run_once": extractor_worker.run_once, "busy": threading.Lock()},
    "download": {"thread": None, "stop": None, "run_once": download_worker.run_once, "busy": threading.Lock()},
    "pdf": {"thread": None, "stop": None, "run_once": pdf_worker.run_once, "busy": threading.Lock()},
}
_workers_lock = threading.Lock()


def _run_once_exclusive(name: str, **kwargs):
    """Only one pass (auto-loop or manual button) runs at a time per worker,
    so a manual click can't grab the same sheet row the loop is mid-processing.
    Returns None (not 0) when skipped because another pass is already running
    — a big backlog can take minutes, and collapsing "already busy" into the
    same 0 that "ran and found nothing pending" produces made the button look
    broken on every repeat click while a long batch was still working."""
    w = _workers[name]
    if not w["busy"].acquire(blocking=False):
        return None
    try:
        return w["run_once"](**kwargs)
    finally:
        w["busy"].release()


def _worker_loop(name: str, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            _run_once_exclusive(name, stop_event=stop_event)
        except Exception as e:
            print(f"[{name}] loop error: {e}")
        stop_event.wait(POLL_SECONDS)


@app.route("/api/workers/status")
def api_workers_status():
    with _workers_lock:
        return jsonify({
            name: "running" if w["thread"] and w["thread"].is_alive() else "stopped"
            for name, w in _workers.items()
        })


def start_worker(name):
    with _workers_lock:
        w = _workers[name]
        if w["thread"] and w["thread"].is_alive():
            w["stop"].clear()
            return
        stop_event = threading.Event()
        thread = threading.Thread(target=_worker_loop, args=(name, stop_event), daemon=True)
        w["stop"] = stop_event
        w["thread"] = thread
        thread.start()


@app.route("/api/workers/<name>/start", methods=["POST"])
def api_workers_start(name):
    if name not in _workers:
        return jsonify({"success": False, "error": "알 수 없는 워커"}), 404
    start_worker(name)
    return jsonify({"success": True, "status": "running"})


@app.route("/api/workers/<name>/stop", methods=["POST"])
def api_workers_stop(name):
    if name not in _workers:
        return jsonify({"success": False, "error": "알 수 없는 워커"}), 404
    with _workers_lock:
        w = _workers[name]
        if w["stop"]:
            w["stop"].set()
    return jsonify({"success": True, "status": "stopped"})


@app.route("/api/workers/<name>/run-once", methods=["POST"])
def api_workers_run_once(name):
    """Process whatever's in the queue right now, once, regardless of
    whether the continuous auto-worker is running — for when a URL just
    landed in the sheet and the next 10s poll feels too slow to wait for."""
    if name not in _workers:
        return jsonify({"success": False, "error": "알 수 없는 워커"}), 404
    try:
        handled = _run_once_exclusive(name)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    if handled is None:
        return jsonify({
            "success": False, "busy": True,
            "error": "이미 처리 중입니다 (앞선 배치가 아직 끝나지 않음). 잠시 후 다시 시도하세요.",
        }), 409
    return jsonify({"success": True, "handled": handled})


@app.route("/api/queue/status")
def api_queue_status():
    def counts(rows, headers):
        c = {}
        for r in rows:
            status = (r.get("status") or "(없음)").strip() or "(없음)"
            c[status] = c.get(status, 0) + 1
        return c

    try:
        submissions = sheets.read_rows(SPREADSHEET_ID, "submissions", sheets.SUBMISSIONS_HEADERS)
        images = sheets.read_rows(SPREADSHEET_ID, "images", sheets.IMAGES_HEADERS)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "spreadsheet_url": SPREADSHEET_URL,
        "submissions": {"total": len(submissions), "counts": counts(submissions, sheets.SUBMISSIONS_HEADERS)},
        "images": {"total": len(images), "counts": counts(images, sheets.IMAGES_HEADERS)},
    })


_QUEUE_SHEETS = {
    "submissions": {
        "headers": sheets.SUBMISSIONS_HEADERS,
        "worker": extractor_worker,
        "worker_name": "extractor",
    },
    "images": {
        "headers": sheets.IMAGES_HEADERS,
        "worker": download_worker,
        "worker_name": "download",
    },
}

# Rows in these statuses are done and not worth listing by default — the UI
# only shows what's still actionable unless asked to include everything.
_TERMINAL_STATUSES = {"done", "downloaded", "discarded"}


@app.route("/api/settings/extraction-mode")
def api_get_extraction_mode():
    return jsonify({"only_og_image": settings.get_only_og_image()})


@app.route("/api/settings/extraction-mode", methods=["POST"])
def api_set_extraction_mode():
    data = request.get_json(force=True) or {}
    value = settings.set_only_og_image(bool(data.get("only_og_image")))
    return jsonify({"success": True, "only_og_image": value})


@app.route("/api/queue/list")
def api_queue_list():
    sheet = request.args.get("sheet", "images")
    if sheet not in _QUEUE_SHEETS:
        return jsonify({"error": "sheet는 submissions 또는 images"}), 400
    show_all = request.args.get("all") == "1"
    cfg = _QUEUE_SHEETS[sheet]
    try:
        rows = sheets.read_rows(SPREADSHEET_ID, sheet, cfg["headers"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not show_all:
        rows = [r for r in rows if (r.get("status") or "pending") not in _TERMINAL_STATUSES]
    rows.sort(key=lambda r: r["_row_number"], reverse=True)

    folders = sorted({(r.get("folder") or "") for r in rows if r.get("folder")})
    return jsonify({"rows": rows[:300], "truncated": len(rows) > 300, "folders": folders})


@app.route("/api/queue/action", methods=["POST"])
def api_queue_action():
    data = request.get_json(force=True) or {}
    sheet = data.get("sheet")
    action = data.get("action")
    row_numbers = set(int(n) for n in (data.get("row_numbers") or []))
    if sheet not in _QUEUE_SHEETS:
        return jsonify({"success": False, "error": "sheet는 submissions 또는 images"}), 400
    if not row_numbers:
        return jsonify({"success": False, "error": "row_numbers가 필요합니다."}), 400

    cfg = _QUEUE_SHEETS[sheet]
    try:
        if action == "discard":
            sheets.update_rows(
                SPREADSHEET_ID, sheet,
                {n: {"status": "discarded"} for n in row_numbers},
                cfg["headers"],
            )
            return jsonify({"success": True, "handled": len(row_numbers)})
        elif action == "process":
            w = _workers[cfg["worker_name"]]
            if not w["busy"].acquire(blocking=False):
                return jsonify({"success": False, "error": "이 워커가 이미 처리 중입니다. 잠시 후 다시 시도하세요."}), 409
            try:
                handled = cfg["worker"].process_rows(row_numbers)
            finally:
                w["busy"].release()
            return jsonify({"success": True, "handled": handled})
        else:
            return jsonify({"success": False, "error": "action은 process 또는 discard"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/")
def index():
    return render_template("index.html", spreadsheet_url=SPREADSHEET_URL)


@app.route("/pdf")
def pdf_page():
    return render_template("pdf.html", pdf_sheet_url=PDF_SHEET_URL)


@app.route("/api/pdf-queue/status")
def api_pdf_queue_status():
    with _workers_lock:
        w = _workers["pdf"]
        running = bool(w["thread"] and w["thread"].is_alive() and not w["stop"].is_set())
    return jsonify({**pdf_worker.status(), "running": running, "spreadsheet_url": PDF_SHEET_URL})


@app.route("/api/google-auth/status")
def api_google_auth_status():
    return jsonify(drive.auth_status())


@app.route("/api/google-auth/start", methods=["POST"])
def api_google_auth_start():
    drive.start_authorization()
    return jsonify({"success": True, **drive.auth_status()})


@app.route("/api/pdf-queue/add", methods=["POST"])
def api_pdf_queue_add():
    data = request.get_json(force=True) or {}
    items = data.get("submissions")
    if "text" in data:
        text = data["text"]
        if not isinstance(text, str) or len(text) > 1_000_000:
            return jsonify({"success": False, "error": "붙여넣을 텍스트는 100만 자 이하로 입력하세요."}), 400
        urls = extract_urls(text)
        if not urls:
            return jsonify({"success": False, "error": "텍스트에서 유효한 http:// 또는 https:// URL을 찾지 못했습니다."}), 400
        items = [{"url": url, "folder": data.get("folder") or "PDF"} for url in urls]
    if items is None:
        urls = data.get("urls")
        items = [{"url": value, "folder": data.get("folder") or "PDF"} for value in urls] if isinstance(urls, list) else []
    if not isinstance(items, list) or not items or len(items) > 200:
        return jsonify({"success": False, "error": "URL을 1~200개 입력하세요."}), 400

    rows = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("url")
        if not isinstance(value, str):
            continue
        url = value.strip()
        if not url:
            continue
        if not http_link("", url):
            return jsonify({"success": False, "error": "http:// 또는 https:// URL을 입력하세요."}), 400
        if url in seen:
            continue
        seen.add(url)
        row = {
            "url": url,
            "folder": str(item.get("folder") or data.get("folder") or "PDF"),
            "status": "pending",
        }
        for key, value in (
            ("source_page", item.get("source_page")),
            ("title", item.get("title")),
            ("expected_md5", item.get("expected_md5")),
        ):
            if value:
                row[key] = str(value)[:150] if key == "title" else str(value)
        rows.append(row)
    if not rows:
        return jsonify({"success": False, "error": "URL이 필요합니다."}), 400
    try:
        sheets.append_rows(SPREADSHEET_ID, PDF_SHEET_NAME, rows, sheets.PDF_HEADERS)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    return jsonify({"success": True, "added": len(rows)})


@app.route("/api/pdfs/recent")
def api_pdfs_recent():
    return jsonify({"pdfs": db.list_by_mime_prefix("application/pdf")})


@app.route("/api/files/<file_id>/download")
def api_download_saved_file(file_id):
    record = db.get_image(file_id)
    if not record:
        return jsonify({"error": "저장된 파일을 찾을 수 없습니다."}), 404
    root = pipeline.DOWNLOADS_DIR.resolve()
    path = (root / record["local_path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return jsonify({"error": "저장된 파일을 찾을 수 없습니다."}), 404
    return send_file(path, mimetype=record["mime_type"] or "application/octet-stream",
                     as_attachment=True, download_name=record["filename"])


@app.route("/api/links/extract", methods=["POST"])
def api_links_extract():
    """"Link Gopher" style bulk-link listing — fetch a page and return every
    <a href> on it ("links", the original full list), plus just the document
    files among them ("files", deduplicated, with a format and a readable
    name), so a resource page's files can be picked without opening each."""
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "url이 필요합니다."}), 400
    try:
        resp = net.fetch_page(url)
    except Exception as e:
        return jsonify({"success": False, "error": f"페이지를 가져오지 못했습니다: {e}"}), 502
    if resp.status_code == 403:
        # Whole-site bot protection (Akamai, Cloudflare, ...) answers every
        # non-browser request this way — say so plainly instead of a bare
        # "403 Client Error", so it isn't mistaken for a bug in this app.
        server = resp.headers.get("Server", "")
        return jsonify({"success": False, "error": (
            f"403 접근 거부 — 이 사이트가 브라우저가 아닌 프로그램의 접근을 막고 있습니다"
            f"{f' (보안 서버: {server})' if server else ''}. 이 앱으로는 이 사이트의 페이지와 파일을 가져올 수 없습니다."
        )}), 502
    try:
        resp.raise_for_status()
    except Exception as e:
        return jsonify({"success": False, "error": f"페이지를 가져오지 못했습니다: {e}"}), 502
    links = filter_navigation_links(extract_links_from_html(resp.text, resp.url))
    files, unprobed = _files_on_page(resp.text, resp.url)
    # A board list page: its files sit one level down on each post — hand
    # back the post list so the UI can gather files from the chosen posts.
    posts, posts_source = listing.find_posts(resp.text, resp.url)

    return jsonify({
        "success": True, "links": links, "total": len(links),
        "files": files, "unprobed": unprobed,
        "posts": posts, "posts_source": posts_source,
    })


# One request's worth of posts — the UI sends a long post list in chunks
# so it can show progress and no single request runs for minutes.
_MAX_POSTS_PER_REQUEST = 10


@app.route("/api/links/post-files", methods=["POST"])
def api_links_post_files():
    """Open each given post page and return the files on it:
    {"results": [{"url", "files": [...], "error"}]} in the same order."""
    data = request.get_json(force=True) or {}
    urls = [u.strip() for u in (data.get("urls") or []) if isinstance(u, str) and u.strip()]
    if not urls:
        return jsonify({"success": False, "error": "urls가 필요합니다."}), 400
    if len(urls) > _MAX_POSTS_PER_REQUEST:
        return jsonify({"success": False, "error": f"한 번에 최대 {_MAX_POSTS_PER_REQUEST}개까지입니다."}), 400

    def one(post_url):
        try:
            page = net.fetch_page(post_url)
            page.raise_for_status()
            files, _ = _files_on_page(page.text, page.url)
            return {"url": post_url, "files": files, "error": ""}
        except Exception as e:
            return {"url": post_url, "files": [], "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, urls))
    return jsonify({"success": True, "results": results})


def _files_on_page(html: str, page_url: str):
    """(document files on the page, how many download links went unchecked)."""
    files, unverified = extract_file_links(html, page_url)
    # Download-script links (FileDown.do?..., download?atch_no=...) don't say
    # what they are — peek at each one's first bytes to find out. Capped so a
    # page full of download links can't stall the request.
    # The same document is often linked twice through different URLs (a
    # "/files/x.pdf" link and a "fileDown.do?..." link) — once the server
    # tells us the probed file's name, drop it if that name is already listed.
    to_probe = unverified[:_MAX_LINK_PROBES]
    known_names = {file_name_key(f["title"] or f["text"]) for f in files}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for item, (ext, server_name) in zip(to_probe, pool.map(_probe_file, [i["url"] for i in to_probe])):
            if not ext:
                continue
            item["ext"] = ext
            if server_name and not item["title"]:
                item["text"] = server_name
            key = file_name_key(item["title"] or item["text"])
            if key in known_names:
                continue
            known_names.add(key)
            files.append(item)
    for item in files:
        saved = pipeline.find_saved_by_url(item["url"])
        item["saved_as"] = saved["local_path"] if saved else ""
    return files, max(len(unverified) - _MAX_LINK_PROBES, 0)


_MAX_LINK_PROBES = 20


def _probe_file(url: str):
    """(document extension or "" if it's not a file, the filename the server
    gives it) — reads only the first KB, never the whole file. The bytes
    decide PDF; for other formats the server's filename says which one, as
    long as the body isn't an HTML page."""
    try:
        resp = net.fetch_page(url, stream=True)
    except Exception:
        return "", None
    try:
        if resp.status_code >= 400:
            return "", None
        head = next(resp.iter_content(chunk_size=1024), b"")
        server_name = pipeline.content_disposition_name(resp)
        if head.startswith(b"%PDF-"):
            return "pdf", server_name
        if head.lstrip()[:1] == b"<":
            return "", None
        return file_ext_of(server_name), server_name
    except Exception:
        return "", None
    finally:
        resp.close()


@app.route("/api/submissions/add", methods=["POST"])
def api_submissions_add():
    """Register URLs into the same submissions queue an AI would write to
    (Path B in ARCHITECTURE.txt) — this is the one on-ramp the whole app is
    built around, so web-UI features that discover URLs (Link Gopher, etc.)
    feed into it too instead of bypassing it with a bespoke direct-download
    call."""
    data = request.get_json(force=True) or {}
    items = data.get("submissions") or []
    if not items:
        return jsonify({"success": False, "error": "submissions 배열이 필요합니다."}), 400

    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        rows.append({
            "id": f"sub_{uuid.uuid4().hex[:12]}",
            "kind": "file" if item.get("kind") == "file" else "image",
            "url": url,
            "source_page": item.get("source_page") or "",
            "title": (item.get("title") or "")[:150],
            "folder": item.get("folder") or "",
            "status": "pending",
            "error": "",
            "created_at": now,
        })
    if not rows:
        return jsonify({"success": False, "error": "유효한 url이 없습니다."}), 400

    try:
        sheets.append_rows(SPREADSHEET_ID, "submissions", rows, sheets.SUBMISSIONS_HEADERS)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "added": len(rows)})


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(force=True)
    target = (data.get("url") or "").strip()
    if not target:
        return jsonify({"error": "URL을 입력해주세요."}), 400
    if not target.startswith(("http://", "https://")):
        target = "https://" + target

    try:
        resp = net.fetch_page(target)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"페이지를 불러오지 못했습니다: {e}"}), 400

    content_type = resp.headers.get("Content-Type", "")

    if content_type.startswith("image/"):
        return jsonify({"page_url": target, "images": [{"url": target, "alt": "(직접 이미지 링크)"}]})

    images = extract_images_from_html(resp.text, resp.url)
    return jsonify({"page_url": resp.url, "images": images})


@app.route("/proxy")
def proxy():
    image_url = request.args.get("url", "")
    page_url = request.args.get("ref", image_url)
    if not image_url:
        abort(400)
    try:
        r = net.fetch_image(image_url, page_url, stream=True)
        r.raise_for_status()
    except requests.RequestException:
        abort(502)
    content_type = r.headers.get("Content-Type", "image/jpeg")
    return Response(r.content, content_type=content_type)


def filename_from_url(image_url: str, fallback_ext=".jpg") -> str:
    path = urlparse(image_url).path
    name = path.rsplit("/", 1)[-1] or "image"
    name = re.sub(r"[^\w\.\-]", "_", name)
    if "." not in name:
        name += fallback_ext
    return name[:150]


@app.route("/download")
def download():
    image_url = request.args.get("url", "")
    page_url = request.args.get("ref", image_url)
    if not image_url:
        abort(400)
    try:
        r = net.fetch_image(image_url, page_url)
        r.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502

    filename = filename_from_url(image_url)
    return send_file(
        io.BytesIO(r.content),
        mimetype=r.headers.get("Content-Type", "application/octet-stream"),
        as_attachment=True,
        download_name=filename,
    )


@app.route("/download_zip", methods=["POST"])
def download_zip():
    data = request.get_json(force=True)
    urls = data.get("urls") or []
    page_url = data.get("page_url", "")
    if not urls:
        return jsonify({"error": "선택된 이미지가 없습니다."}), 400

    buffer = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for image_url in urls:
            try:
                r = net.fetch_image(image_url, page_url)
                r.raise_for_status()
            except requests.RequestException:
                continue
            name = filename_from_url(image_url)
            base, ext = name.rsplit(".", 1) if "." in name else (name, "jpg")
            candidate = name
            n = 1
            while candidate in used_names:
                candidate = f"{base}_{n}.{ext}"
                n += 1
            used_names.add(candidate)
            zf.writestr(candidate, r.content)

    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name="images.zip")


# ---------------------------------------------------------------------------
# AI-facing registry API: fetch image URLs found elsewhere (e.g. by an AI web
# search) and turn them into real files on disk, tracked in a SQLite registry
# so they can be referenced again later ("저장한 이미지 1, 3, 5번을 ...").
# ---------------------------------------------------------------------------

@app.route("/api/images/download", methods=["POST"])
def api_download_image():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "url이 필요합니다."}), 400

    try:
        record = pipeline.download_and_process(
            url=url,
            source_page=(data.get("source_page") or "").strip(),
            title=(data.get("title") or "").strip(),
            folder=(data.get("folder") or "").strip(),
            # Only ever what the caller explicitly sends for this one
            # request — e.g. the user's own already-logged-in session
            # cookies for a site they have legitimate access to.
            cookies=data.get("cookies") if isinstance(data.get("cookies"), dict) else None,
            expected_md5=data.get("expected_md5", ""),
        )
    except pipeline.DownloadError as e:
        return jsonify({"success": False, "url": url, "error": str(e)}), 400

    return jsonify({"success": True, "file_id": record["id"], **record})


@app.route("/api/images/download-batch", methods=["POST"])
def api_download_batch():
    data = request.get_json(force=True) or {}
    images = data.get("images") or []
    folder = (data.get("folder") or "").strip()
    if not images:
        return jsonify({"success": False, "error": "images 배열이 필요합니다."}), 400

    results = []
    for item in images:
        url = (item.get("url") or "").strip()
        if not url:
            results.append({"success": False, "url": url, "error": "url이 필요합니다."})
            continue
        try:
            record = pipeline.download_and_process(
                url=url,
                source_page=(item.get("source_page") or "").strip(),
                title=(item.get("title") or "").strip(),
                folder=folder,
                expected_md5=item.get("expected_md5", ""),
            )
            results.append({"success": True, "file_id": record["id"], **record})
        except pipeline.DownloadError as e:
            results.append({"success": False, "url": url, "error": str(e)})

    job_id = results[0]["job_id"] if results and results[0].get("success") else db.get_or_create_job(folder)
    succeeded = sum(1 for r in results if r["success"])
    return jsonify({
        "success": True,
        "job_id": job_id,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    })


@app.route("/api/images/<image_id>")
def api_get_image(image_id):
    record = db.get_image(image_id)
    if not record:
        return jsonify({"error": "not found"}), 404
    return jsonify(record)


@app.route("/api/jobs/<job_id>")
def api_get_job(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


def _upload_one(record: dict, parent_id: str) -> dict:
    if record["drive_file_id"]:
        return {"success": True, "file_id": record["id"], **record}
    local_path = pipeline.DOWNLOADS_DIR / record["local_path"]
    result = drive.upload_file(
        local_path=str(local_path),
        filename=record["filename"],
        mime_type=record["mime_type"] or "application/octet-stream",
        parent_id=parent_id,
    )
    db.update_image_drive(record["id"], result["drive_file_id"], result["drive_url"])
    record = db.get_image(record["id"])
    return {"success": True, "file_id": record["id"], **record}


@app.route("/api/drive/upload", methods=["POST"])
def api_drive_upload():
    data = request.get_json(force=True) or {}
    file_id = (data.get("file_id") or "").strip()
    record = db.get_image(file_id)
    if not record:
        return jsonify({"success": False, "error": "이미지를 찾을 수 없습니다."}), 404

    try:
        parent_id = data.get("folder_id") or drive.get_or_create_folder(record["job_id"])
        result = _upload_one(record, parent_id)
    except drive.DriveNotConfigured as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"업로드 실패: {e}"}), 500

    return jsonify(result)


@app.route("/api/drive/upload-job", methods=["POST"])
def api_drive_upload_job():
    data = request.get_json(force=True) or {}
    job_id = (data.get("job_id") or "").strip()
    job = db.get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "job을 찾을 수 없습니다."}), 404

    try:
        parent_id = data.get("folder_id") or drive.get_or_create_folder(job_id)
    except drive.DriveNotConfigured as e:
        return jsonify({"success": False, "error": str(e)}), 400

    results = []
    for record in job["images"]:
        try:
            results.append(_upload_one(record, parent_id))
        except Exception as e:
            results.append({"success": False, "file_id": record["id"], "error": f"업로드 실패: {e}"})

    succeeded = sum(1 for r in results if r["success"])
    return jsonify({
        "success": True,
        "job_id": job_id,
        "folder_id": parent_id,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    })


if __name__ == "__main__":
    if os.environ.get("PDF_AUTO_START", "1") != "0":
        start_worker("pdf")
    app.run(debug=False, port=5000, threaded=True)
