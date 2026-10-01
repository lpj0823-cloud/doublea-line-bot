"""網頁版 Google 重新授權（不需要在電腦上執行 auth_setup.py）。

流程：
1. 管理員開 /reauth?token=<SELFTEST_TOKEN>，點「登入 Google 授權」
2. 用 Google 帳號登入並同意後，瀏覽器會跳到 http://localhost:8765/?code=...
   （這頁會顯示「無法連線」是正常的）
3. 把網址列整串網址複製，貼回 /reauth 的表單送出
4. 伺服器用這個 code 換新的 refresh token，存到永久磁碟（DATA_DIR/google_token.json）

OAuth 用戶端是「電腦版應用程式」類型，Google 允許 http://localhost 任意連接埠當作回傳網址。
"""
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from google_auth import SCOPES, load_token_data, save_token_data

REDIRECT_URI = "http://localhost:8765/"


def _client() -> tuple[str, str]:
    data = load_token_data()
    return data["client_id"], data["client_secret"]


def build_auth_url() -> str:
    client_id, _ = _client()
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def extract_code(pasted: str) -> str | None:
    pasted = (pasted or "").strip()
    if not pasted:
        return None
    if "code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        codes = qs.get("code")
        return codes[0] if codes else None
    return pasted  # 也接受只貼 code 本身


def exchange_code(code: str) -> None:
    client_id, client_secret = _client()
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    body = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    if r.status_code != 200 or not body.get("refresh_token"):
        err = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        raise RuntimeError(f"換取授權失敗：{err}")
    save_token_data({
        "refresh_token": body["refresh_token"],
        "client_id": client_id,
        "client_secret": client_secret,
    })
