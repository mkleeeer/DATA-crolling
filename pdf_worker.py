"""URL-first PDF/document queue; reuses the existing resolver and registry."""
import threading
import time
from datetime import datetime

import pipeline
import sheets
from queue_config import PDF_SHEET_NAME, SPREADSHEET_ID

_state_lock = threading.Lock()
_state = {"checked_at": None, "error": "", "counts": {}}
_run_lock = threading.Lock()


def status():
    with _state_lock:
        return {**_state, "counts": dict(_state["counts"])}


def _publish(rows, errors):
    counts = {}
    for row in rows:
        if (row.get("url") or "").strip():
            key = (row.get("status") or "").strip() or "pending"
            counts[key] = counts.get(key, 0) + 1
    with _state_lock:
        _state.update(checked_at=datetime.now().isoformat(timespec="seconds"),
                      error="; ".join(errors), counts=counts)


def process_pdf(row, on_progress=lambda: None):
    number = row["_row_number"]
    url = (row.get("url") or "").strip()
    if not url:
        return
    sheets.update_row(SPREADSHEET_ID, PDF_SHEET_NAME, number,
                      {"status": "downloading", "error": "", "updated_at": datetime.now().isoformat(timespec="seconds")},
                      sheets.PDF_HEADERS)
    row["status"] = "downloading"
    on_progress()
    try:
        kwargs = {
            "folder": row.get("folder") or "PDF",
            "expected_md5": row.get("expected_md5") or "",
        }
        if row.get("source_page"):
            kwargs["source_page"] = row["source_page"]
        if row.get("title"):
            kwargs["title"] = row["title"]
        record = pipeline.download_and_process(url, **kwargs)
    except Exception as exc:
        sheets.update_row(SPREADSHEET_ID, PDF_SHEET_NAME, number,
                          {"status": "failed", "error": str(exc)[:1000],
                           "updated_at": datetime.now().isoformat(timespec="seconds")}, sheets.PDF_HEADERS)
        row["status"] = "failed"
        return
    # Keep a failed Sheets result write separate from a failed download.
    # If this write fails, the row remains downloading rather than being
    # automatically picked up again and producing a duplicate file.
    sheets.update_row(SPREADSHEET_ID, PDF_SHEET_NAME, number, {
        "status": "downloaded", "error": "", "filename": record["filename"],
        "local_path": str(pipeline.DOWNLOADS_DIR / record["local_path"]),
        "file_id": record["id"], "md5": record.get("md5", ""),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, sheets.PDF_HEADERS)
    row["status"] = "downloaded"


def run_once(stop_event=None):
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        rows = sheets.read_rows(SPREADSHEET_ID, PDF_SHEET_NAME, sheets.PDF_HEADERS)
        handled = 0
        errors = []
        _publish(rows, errors)
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            if not (row.get("url") or "").strip() or (row.get("status") or "").strip() not in ("", "pending"):
                continue
            try:
                process_pdf(row, lambda: _publish(rows, errors))
            except Exception as exc:
                errors.append(f"{row['_row_number']}행 상태 기록 실패: {type(exc).__name__}")
            handled += 1
            _publish(rows, errors)
            if stop_event is not None:
                stop_event.wait(0.4)
            else:
                time.sleep(0.4)
        _publish(rows, errors)
        return handled
    except Exception as exc:
        with _state_lock:
            _state.update(checked_at=datetime.now().isoformat(timespec="seconds"), error=str(exc))
        raise
    finally:
        _run_lock.release()
