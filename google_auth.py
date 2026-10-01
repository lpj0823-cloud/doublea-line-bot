import base64
import json
import os
import traceback

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from paths import data_path

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]

# /reauth 網頁重新授權後，新 token 存在永久磁碟上的這個檔案，優先於環境變數使用
TOKEN_FILE = data_path("google_token.json")


def is_auth_expired_error(e: Exception) -> bool:
    """Google refresh token 過期或被撤銷（需要重新授權）。"""
    s = str(e)
    return "invalid_grant" in s or "expired or revoked" in s


def auth_error_hint(e: Exception) -> str:
    if is_auth_expired_error(e):
        return "⚠️ Google 授權已過期，行事曆／待辦暫時無法使用，請管理員重新授權。"
    return "⚠️ Google 服務暫時無法連線，請稍後再試。"


def _decode_env_token() -> dict:
    raw = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
    if not raw:
        raise EnvironmentError("GOOGLE_TOKEN_JSON environment variable is not set")
    # 補齊 base64 padding（= 號不足時會出現 Incorrect padding）
    missing = len(raw) % 4
    if missing:
        raw += "=" * (4 - missing)
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except Exception:
        print("[google_auth] base64 解碼失敗")
        traceback.print_exc()
        raise
    try:
        return json.loads(decoded)
    except Exception:
        print("[google_auth] JSON 解析失敗")
        traceback.print_exc()
        raise


def load_token_data() -> dict:
    """優先讀 /reauth 存下的新 token（永久磁碟），否則用環境變數 GOOGLE_TOKEN_JSON。"""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("refresh_token") and data.get("client_id"):
                return data
        except Exception as e:
            print(f"[google_auth] 讀取 {TOKEN_FILE} 失敗，改用環境變數：{e}")
    return _decode_env_token()


def save_token_data(data: dict) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_credentials() -> Credentials:
    token_data = load_token_data()
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
    except Exception as e:
        if is_auth_expired_error(e):
            print(f"[google_auth] Google 授權已過期，請到 /reauth 重新授權：{e}")
        else:
            print("[google_auth] Credentials 建立或 refresh 失敗")
            traceback.print_exc()
        raise
