# UNKNOWN_BINARY 응답 진단

이미지 디코딩까지 실패한 응답은 종료하기 전에 아래 증거를 서버 콘솔에 남깁니다.

- 요청 URL, 최종 URL, 리다이렉트 URL/상태, 최종 HTTP 상태
- Content-Type, Content-Length, Content-Disposition, Content-Encoding,
  Transfer-Encoding, Server 응답 헤더
- 실제 본문 바이트 수, SHA-256, 이미지 디코더 예외 종류
- 첫 **512바이트**의 `sample_hex`, `sample_utf8`, `sample_latin1`

텍스트는 JSON으로 이스케이프해 줄바꿈·제어문자가 콘솔 출력을 깨뜨리지
않도록 합니다. UTF-8에서 해석할 수 없는 바이트는 대체 문자로 표시하며,
hex와 latin-1 값에는 해당 바이트 정보가 보존됩니다.

기본적으로 전체 본문을 Windows `%TEMP%\image-crawler-diagnostics\unknown-*.bin`
(다른 OS에서는 시스템 임시 폴더 아래)에 저장합니다. 요청마다 고유한 파일명을
사용하며 파일을 닫은 후에도 남겨둡니다. 로그와 DownloadError 메시지에 절대 경로가
표시됩니다. 앱은 이 파일을 성공한 다운로드로 등록하거나 Drive로 업로드하지 않습니다.
UNKNOWN_BINARY는 진단 자료를 남긴 뒤 기존 실패 경로로 반환됩니다.

저장되는 바이트는 `requests.Response.content` 전체입니다. HTTP gzip 등은 requests가
이미 압축을 해제했을 수 있으므로 네트워크 전송 바이트 그대로의 캡처는 아닙니다.

선택 환경변수:

| 변수 | 기본값 / 동작 |
| --- | --- |
| `UNKNOWN_BINARY_SAVE_RAW` | `1`: 원본 임시 저장. `0`이면 로그만 남김 |
| `UNKNOWN_BINARY_DIAGNOSTICS_DIR` | 미지정 시 시스템 임시 폴더의 `image-crawler-diagnostics`; 지정 시 해당 폴더에 저장 |

예를 들어 실행 전 PowerShell에서 `$env:UNKNOWN_BINARY_DIAGNOSTICS_DIR = 'C:\Temp\image-crawler-diagnostics'`
로 저장 폴더를 선택할 수 있습니다. 저장 공간/권한 문제로 실패하면 진단 로그는 유지하고
오류 메시지에 원본 저장 실패를 표시합니다. 임시 파일에는 자동 만료 정책이 없으므로
확인 후 삭제하세요. 실제 응답 덤프·본문 로그는 Git에 추가하지 마세요.

`46696e616e636961`는 ASCII `Financia`입니다. 이 접두사만으로 파일 유형이나
서버의 반환 이유를 확정할 수 없습니다. 같은 URL을 다시 처리한 후 `sample_utf8`과
`sample_latin1`로 텍스트 메시지인지 확인하고, 최종 URL·리다이렉트·Content-Disposition과
비교하세요. 512바이트 이후의 내용은 로그의 `.bin` 파일을 텍스트/hex 뷰어로 확인할 수
있습니다. 알 수 없는 파일은 자동으로 실행하거나 확장자를 추측해 정상 등록하지 않습니다.

검증: `python -m unittest discover -s tests -v`
