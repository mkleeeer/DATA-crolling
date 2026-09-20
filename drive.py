from pathlib import Path
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR = Path(__file__).parent
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / "token.json"
# drive.file: the app can only see/manage files *it* creates, not the user's whole Drive.
# spreadsheets: read/write the queue sheet used by the extractor/download workers.
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]


class DriveNotConfigured(Exception):
    pass


_credential_lock = threading.Lock()
_auth_state_lock = threading.Lock()
_auth_state = {"running": False, "error": ""}


def auth_status():
    with _auth_state_lock:
        return {**_auth_state, "configured": CLIENT_SECRET_FILE.exists(),
                "authorized": TOKEN_FILE.exists()}


def start_authorization():
    """Only an explicit UI action opens the browser, never a polling worker."""
    with _auth_state_lock:
        if _auth_state["running"]:
            return
        _auth_state.update(running=True, error="")

    def authorize():
        error = ""
        try:
            with _credential_lock:
                if not CLIENT_SECRET_FILE.exists():
                    raise DriveNotConfigured("client_secret.json이 없습니다. 앱 폴더에 Google OAuth 설정 파일을 넣어주세요.")
                flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
                creds = flow.run_local_server(port=0, timeout_seconds=120)
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            error = "Google 인증을 완료하지 못했습니다. 연결 버튼을 눌러 다시 승인해주세요."
        finally:
            with _auth_state_lock:
                _auth_state.update(running=False, error=error)

    threading.Thread(target=authorize, daemon=True).start()


def get_credentials() -> Credentials:
    # Do not hold a queue/API request open while waiting for user interaction.
    if auth_status()["running"]:
        raise DriveNotConfigured("Google 승인 대기 중입니다. 열린 브라우저에서 승인을 완료해주세요.")
    with _credential_lock:
        return _load_credentials()


def _load_credentials() -> Credentials:
    if not CLIENT_SECRET_FILE.exists():
        raise DriveNotConfigured(
            f"{CLIENT_SECRET_FILE.name}이(가) 없습니다. Google Cloud Console에서 OAuth 클라이언트(데스크톱 앱)를 "
            f"만들어 다운로드한 뒤 {CLIENT_SECRET_FILE} 경로에 저장하세요."
        )

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise DriveNotConfigured("Google 계정 연결이 필요합니다. PDF 화면의 Google 연결 버튼을 눌러주세요.")
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_service():
    return build("drive", "v3", credentials=get_credentials())


def get_or_create_folder(name: str, parent_id: str | None = None) -> str:
    service = get_service()
    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, fields="files(id, name)", spaces="drive").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_file(local_path: str, filename: str, mime_type: str, parent_id: str | None = None) -> dict:
    service = get_service()
    metadata = {"name": filename}
    if parent_id:
        metadata["parents"] = [parent_id]
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=False)
    file = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    return {"drive_file_id": file["id"], "drive_url": file["webViewLink"]}
