import base64
import json
import os
import traceback

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


def get_credentials() -> Credentials:
    raw = os.environ.get("GOOGLE_TOKEN_JSON", "")
    if not raw:
        raise EnvironmentError("GOOGLE_TOKEN_JSON environment variable is not set")

    # 移除空白/換行，補齊 base64 padding（= 號不足時會出現 Incorrect padding）
    raw = raw.strip()
    missing = len(raw) % 4
    if missing:
        raw += "=" * (4 - missing)

    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except Exception:
        print("[google_auth] base64 解碼失敗，GOOGLE_TOKEN_JSON 內容（前 80 字元）：", raw[:80])
        print("[google_auth] 完整錯誤：")
        traceback.print_exc()
        raise

    try:
        token_data = json.loads(decoded)
    except Exception:
        print("[google_auth] JSON 解析失敗，解碼後內容（前 200 字元）：", decoded[:200])
        print("[google_auth] 完整錯誤：")
        traceback.print_exc()
        raise

    try:
        creds = Credentials(
            token=None,
            refresh_token=token_data["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=token_data["client_id"],
            client_secret=token_data["client_secret"],
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds
    except Exception:
        print("[google_auth] Credentials 建立或 refresh 失敗")
        print("[google_auth] token_data keys：", list(token_data.keys()))
        print("[google_auth] 完整錯誤：")
        traceback.print_exc()
        raise
