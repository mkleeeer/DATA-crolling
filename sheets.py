import threading
import time

from googleapiclient.errors import HttpError

import drive

SUBMISSIONS_HEADERS = ["id", "url", "source_page", "title", "folder", "status", "error", "created_at", "kind"]
IMAGES_HEADERS = [
    "id", "submission_id", "folder", "seq", "source_url", "source_page", "title", "status",
    "filename", "local_path", "original_path", "mime_type", "width", "height",
    "drive_file_id", "drive_url", "error", "created_at", "updated_at",
]
# Keep the original ten columns in place so existing `pdfs` tabs remain
# readable; metadata added by the integrated link finder lives in K/L.
PDF_HEADERS = ["url", "folder", "expected_md5", "status", "filename", "local_path",
               "file_id", "md5", "error", "updated_at", "source_page", "title"]

# googleapiclient's service object wraps an httplib2 connection that is not
# safe to share across threads — concurrent calls on the same service (e.g.
# the web UI polling /api/queue/status and /api/queue/list at once, both
# handled by Flask's threaded server) corrupt the shared socket and surface
# as random "[SSL: WRONG_VERSION_NUMBER]" errors. Keep one service per thread.
_local = threading.local()

# Google Sheets write quota is tight (~60 requests/min/user by default). A
# burst of appends (one page can yield dozens of image candidates) can hit
# 429s, so every call here retries with backoff instead of taking down the
# whole worker loop over a transient rate limit.
#
# Each processed submission costs 2-3 writes (status=processing, then
# status=done/error, plus an images append on success), so clearing a
# backlog of pending submissions in one run_once() can itself burst past
# the quota. Retries need to be patient enough to outlast a full quota
# window (~60s), not just a transient blip — backoff is capped at 30s so a
# single call never waits absurdly long, but 6 retries still adds up to
# ~61s total, crossing one whole reset window.
RETRYABLE_STATUSES = {429, 500, 503}
MAX_RETRIES = 6
_MAX_BACKOFF_SECONDS = 30


def get_service():
    if getattr(_local, "service", None) is None:
        _local.service = drive.build_service("sheets", "v4")
    return _local.service


def _execute(request):
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in RETRYABLE_STATUSES and attempt < MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, _MAX_BACKOFF_SECONDS))
                continue
            raise


def create_spreadsheet(title: str) -> str:
    service = get_service()
    body = {
        "properties": {"title": title},
        "sheets": [
            {"properties": {"title": "submissions"}},
            {"properties": {"title": "images"}},
        ],
    }
    result = _execute(service.spreadsheets().create(body=body, fields="spreadsheetId"))
    spreadsheet_id = result["spreadsheetId"]
    _set_row(spreadsheet_id, "submissions", 1, SUBMISSIONS_HEADERS)
    _set_row(spreadsheet_id, "images", 1, IMAGES_HEADERS)
    return spreadsheet_id


def _col_letter(idx: int) -> str:
    letters = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _set_row(spreadsheet_id: str, sheet_name: str, row_number: int, values: list) -> None:
    service = get_service()
    _execute(service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A{row_number}",
        valueInputOption="RAW",
        body={"values": [[str(v) for v in values]]},
    ))


def append_row(spreadsheet_id: str, sheet_name: str, row: dict, headers: list) -> None:
    append_rows(spreadsheet_id, sheet_name, [row], headers)


def append_rows(spreadsheet_id: str, sheet_name: str, rows: list, headers: list) -> None:
    """Write many rows in a single API call — looping append_row per row is
    what blows through Sheets' write quota when one page yields dozens of
    image candidates."""
    if not rows:
        return
    values = [
        [str(row.get(h, "") if row.get(h) is not None else "") for h in headers]
        for row in rows
    ]
    service = get_service()
    _execute(service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ))


def read_rows(spreadsheet_id: str, sheet_name: str, headers: list) -> list:
    service = get_service()
    result = _execute(service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A2:Z"
    ))
    rows = result.get("values", [])
    out = []
    for i, row in enumerate(rows):
        row = row + [""] * (len(headers) - len(row))
        record = dict(zip(headers, row))
        record["_row_number"] = i + 2
        out.append(record)
    return out


def update_row(spreadsheet_id: str, sheet_name: str, row_number: int, updates: dict, headers: list) -> None:
    update_rows(spreadsheet_id, sheet_name, {row_number: updates}, headers)


def update_rows(spreadsheet_id: str, sheet_name: str, row_updates: dict, headers: list) -> None:
    """Write the same or different field updates to many rows in a single API
    call — looping update_row per row (one batchUpdate request each) is what
    made bulk actions like "선택 항목 버리기" on 100+ rows feel frozen."""
    if not row_updates:
        return
    service = get_service()
    data = []
    for row_number, updates in row_updates.items():
        for key, value in updates.items():
            col_letter = _col_letter(headers.index(key))
            data.append({"range": f"{sheet_name}!{col_letter}{row_number}", "values": [[str(value)]]})
    _execute(service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ))
