"""
簡單的記憶體速率限制器（Rate Limiter）
防止使用者短時間內大量呼叫 AI 或外部 API，避免費用暴增。
"""
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

TAIPEI_TZ = pytz.timezone("Asia/Taipei")

# 每個 chat_id 的請求記錄：{ chat_id: [datetime, ...] }
_request_log: dict[str, list[datetime]] = defaultdict(list)

# 限制設定
LIMITS = {
    "ai":         {"max": 10, "window_minutes": 1},   # AI 對話：每分鐘最多10次
    "restaurant": {"max": 3,  "window_minutes": 1},   # 餐廳查詢：每分鐘最多3次
    "proverbs":   {"max": 2,  "window_minutes": 60},  # 箴言：每小時最多2次
}


def check_rate_limit(chat_id: str, action: str) -> tuple[bool, str]:
    """
    檢查是否超過速率限制。
    回傳 (allowed: bool, message: str)
    - allowed=True 表示可以繼續
    - allowed=False 表示超過限制，message 是要回覆使用者的提示
    """
    if action not in LIMITS:
        return True, ""

    limit = LIMITS[action]
    max_requests = limit["max"]
    window = timedelta(minutes=limit["window_minutes"])
    now = datetime.now(TAIPEI_TZ)
    cutoff = now - window

    key = f"{chat_id}:{action}"

    # 清除過期記錄
    _request_log[key] = [t for t in _request_log[key] if t > cutoff]

    # 檢查是否超過限制
    if len(_request_log[key]) >= max_requests:
        if limit["window_minutes"] == 1:
            wait_msg = "1 分鐘後"
        elif limit["window_minutes"] == 60:
            wait_msg = "1 小時後"
        else:
            wait_msg = f"{limit['window_minutes']} 分鐘後"

        action_name = {
            "ai": "AI 對話",
            "restaurant": "餐廳查詢",
            "proverbs": "箴言查詢",
        }.get(action, action)

        return False, f"⚠️ {action_name}請求太頻繁，請 {wait_msg} 再試！"

    # 記錄這次請求
    _request_log[key].append(now)
    return True, ""
