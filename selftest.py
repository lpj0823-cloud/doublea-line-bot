"""機器人自我檢查：逐項確認環境變數、Google 授權、行事曆、待辦、箴言 API、LINE 連線與排程。
用法：
  1) 瀏覽器開  https://<你的網址>/selftest?token=<SELFTEST_TOKEN>   （需先在 Railway 設 SELFTEST_TOKEN）
  2) 或在 Railway / 本機執行  python selftest.py
只回報「正常 / 異常」與原因，不會輸出任何金鑰內容。
"""
import os
from datetime import datetime

import pytz

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
REQUIRED_ENV = ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "GOOGLE_TOKEN_JSON", "GEMINI_API_KEY"]
OPTIONAL_ENV = ["ANTHROPIC_API_KEY", "PUSH_CHAT_ID", "ENABLE_PROVERB_PUSH", "GOOGLE_MAPS_API_KEY"]


def _check(name, fn):
    try:
        return {"item": name, "ok": True, "detail": str(fn())}
    except Exception as e:  # noqa: BLE001
        return {"item": name, "ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}


def run_selftest(scheduler=None) -> list[dict]:
    now = datetime.now(TAIPEI_TZ)
    results = []

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    results.append({"item": "必要環境變數", "ok": not missing,
                    "detail": "齊全" if not missing else f"缺少：{', '.join(missing)}"})
    results.append({"item": "選用環境變數（已設定的）", "ok": True,
                    "detail": ", ".join(k for k in OPTIONAL_ENV if os.environ.get(k)) or "無"})

    def google_auth():
        from google_auth import get_credentials
        get_credentials()
        return "Google 授權（refresh token）有效"
    results.append(_check("Google 授權", google_auth))

    def calendar():
        from calendar_service import list_events_for_date
        return f"今天 {len(list_events_for_date(now))} 筆行程"
    results.append(_check("行事曆讀取", calendar))

    def tasks():
        from todo_service import get_pending_tasks
        return f"待辦 {len(get_pending_tasks())} 筆"
    results.append(_check("Google Tasks 待辦", tasks))

    def proverbs():
        from proverbs_service import get_todays_proverbs
        zh, en = get_todays_proverbs(now)
        bad = [n for n, t in (("中文", zh), ("英文", en)) if "暫時無法取得" in t or "unavailable" in t]
        if bad:
            raise RuntimeError(f"{'、'.join(bad)}箴言 API 取得失敗")
        return f"中文 {len(zh)} 字、英文 {len(en)} 字（送出時會自動分段）"
    results.append(_check("箴言 API（信望愛站）", proverbs))

    def line_api():
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
        cfg = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
        with ApiClient(cfg) as c:
            info = MessagingApi(c).get_bot_info()
        return f"LINE 連線正常：{info.display_name}"
    results.append(_check("LINE Messaging API", line_api))

    def line_quota():
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
        cfg = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
        with ApiClient(cfg) as c:
            api = MessagingApi(c)
            q = api.get_message_quota()
            used = api.get_message_quota_consumption().total_usage
        return f"本月推播額度類型 {q.type}，上限 {q.value if q.value is not None else '無上限'}，已用 {used}"
    results.append(_check("LINE 本月推播額度", line_quota))

    def push_target():
        from state_service import load_chat_id
        cid = os.environ.get("PUSH_CHAT_ID") or load_chat_id()
        if not cid:
            raise RuntimeError("尚無推播對象：請在群組傳任一訊息，或設定 PUSH_CHAT_ID")
        kind = {"C": "群組", "R": "聊天室", "U": "私訊"}.get(cid[:1], "未知")
        return f"推播對象類型：{kind}" + ("（⚠️ 是私訊，排程會送到私訊而非群組）" if cid[:1] == "U" else "")
    results.append(_check("排程推播對象", push_target))

    def persistence():
        from paths import data_dir, is_persistent
        if not is_persistent():
            raise RuntimeError(f"資料存在 {data_dir()}，重新部署會被清空；請掛 Volume 並設 DATA_DIR")
        return f"資料存在永久磁碟 {data_dir()}"
    results.append(_check("購物／筆記／生日／記帳資料保存", persistence))

    def weather():
        from weather_service import get_current_weather
        w = get_current_weather()
        return f"台北 {w['temp']}°C {w['description']}"
    results.append(_check("天氣 API", weather))

    def restaurants():
        from restaurant_service import DEFAULT_LOCATION_NAME, search_nearby_restaurants
        return f"{DEFAULT_LOCATION_NAME} 找到 {len(search_nearby_restaurants())} 家"
    results.append(_check("餐廳 API", restaurants))

    if scheduler is not None:
        jobs = [f"{j.name}@{j.next_run_time:%m/%d %H:%M}" for j in scheduler.get_jobs()]
        results.append({"item": "排程器", "ok": bool(jobs), "detail": "；".join(jobs) or "沒有任何排程"})
    return results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    for r in run_selftest():
        print(("✅" if r["ok"] else "❌"), r["item"], "—", r["detail"])
