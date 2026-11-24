"""下載踏查 Google Sheet 為 .xlsx，給 build.py 使用。

兩種存取方式，依環境變數自動選擇：

1. 服務帳戶（Sheet 不公開時）：設 GOOGLE_SERVICE_ACCOUNT 為服務帳戶的 JSON 金鑰內容，
   並把該帳戶的 email 加為 Sheet 的「檢視者」。需要 `google-auth` 套件。
2. 公開連結（預設）：Sheet 設為「知道連結的任何人都能檢視」，不需任何金鑰。

下載內容若不是 .xlsx（例如 Sheet 未公開時 Google 回的登入頁），直接報錯，
不讓後面的流程拿一份 HTML 去解析。

    python3 tools/fetch_sheet.py out.xlsx [--sheet-id ID]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SHEET_ID = "1FTf9yk94tVLM2x2Tc_n3YnNeePBp7iLfMJvymup3O_w"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_public(sheet_id):
    return _get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx")


def fetch_service_account(sheet_id, key_json):
    from google.oauth2 import service_account
    import google.auth.transport.requests

    creds = service_account.Credentials.from_service_account_info(
        json.loads(key_json), scopes=["https://www.googleapis.com/auth/drive.readonly"])
    creds.refresh(google.auth.transport.requests.Request())
    url = (f"https://www.googleapis.com/drive/v3/files/{sheet_id}/export"
           f"?mimeType={urllib.request.quote(XLSX_MIME)}")
    return _get(url, {"Authorization": f"Bearer {creds.token}"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", help="輸出的 .xlsx 路徑")
    ap.add_argument("--sheet-id", default=os.environ.get("SHEET_ID") or DEFAULT_SHEET_ID)
    args = ap.parse_args(argv)

    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "").strip()
    mode = "服務帳戶" if key else "公開連結"
    try:
        body = fetch_service_account(args.sheet_id, key) if key else fetch_public(args.sheet_id)
    except urllib.error.HTTPError as e:
        print(f"下載失敗（{mode}）：HTTP {e.code}。"
              + ("請確認服務帳戶 email 已加為 Sheet 檢視者。" if key
                 else "請確認 Sheet 已設為「知道連結的任何人都能檢視」，或改用服務帳戶。"),
              file=sys.stderr)
        return 2
    except Exception as e:  # 網路錯誤、金鑰格式錯誤等
        print(f"下載失敗（{mode}）：{e}", file=sys.stderr)
        return 2

    # .xlsx 是 zip 檔，開頭固定是 PK\x03\x04；不是的話多半是登入頁或錯誤頁。
    if not body.startswith(b"PK\x03\x04"):
        print(f"下載到的不是 .xlsx（{mode}，{len(body)} bytes）。"
              "Sheet 可能未公開，或服務帳戶沒有檢視權限。", file=sys.stderr)
        return 2

    with open(args.out, "wb") as fh:
        fh.write(body)
    print(f"已下載（{mode}）：{args.out}，{len(body):,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
