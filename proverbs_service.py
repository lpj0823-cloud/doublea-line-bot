import requests
from datetime import datetime
import pytz

TAIPEI_TZ = pytz.timezone("Asia/Taipei")

# 信望愛站聖經 JSON API（免費）
# 中文：和合本（unv）；英文：World English Bible（web，公共版權）
FHL_API = "https://bible.fhl.net/json/qb.php"
ZH_VERSION = "unv"   # 和合本（繁體）
EN_VERSION = "web"   # World English Bible（NIV 為版權譯本，無免費 API，改用公共版權的 WEB）


def _fetch_fhl_chapter(chapter: int, version: str) -> tuple[bool, list[dict]]:
    """向信望愛站 API 抓取箴言指定章。回傳 (成功與否, record 陣列)。"""
    r = requests.get(
        FHL_API,
        params={
            "chineses": "箴",
            "chap": chapter,
            "gb": 0,          # 0 = 繁體（Big5）
            "version": version,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    status = str(data.get("status", ""))
    if status != "success":
        # FHL 失敗時 status 會是 "Fail:..."，印出方便除錯
        print(f"[DoubleA] 箴言 API 非成功狀態（version={version}）：{status[:120]}")
        return False, []
    return True, data.get("record", [])


def _fetch_zh_chapter(chapter: int) -> str:
    """箴言指定章（繁體中文和合本）。"""
    try:
        ok, records = _fetch_fhl_chapter(chapter, ZH_VERSION)
        if not ok:
            return f"⚠️ 箴言第{chapter}章（中文）暫時無法取得，請稍後再試。"
        lines = [f"📖 箴言 第{chapter}章（和合本）\n"]
        for rec in records:
            sec = rec.get("sec", "")
            text = rec.get("bible_text", "").strip()
            if sec and text:
                lines.append(f"{sec} {text}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[DoubleA] 箴言中文API失敗：{e}")
        return f"⚠️ 箴言第{chapter}章（中文）暫時無法取得，請稍後再試。"


def _fetch_en_chapter(chapter: int) -> str:
    """箴言指定章（英文 World English Bible）。"""
    try:
        ok, records = _fetch_fhl_chapter(chapter, EN_VERSION)
        if not ok:
            return f"⚠️ Proverbs Chapter {chapter} (WEB) temporarily unavailable."
        lines = [f"📖 Proverbs Chapter {chapter} (WEB)\n"]
        for rec in records:
            sec = rec.get("sec", "")
            text = rec.get("bible_text", "").strip()
            if sec and text:
                lines.append(f"{sec} {text}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[DoubleA] 箴言英文API失敗：{e}")
        return f"⚠️ Proverbs Chapter {chapter} (WEB) temporarily unavailable."


def get_todays_proverbs(now: datetime = None) -> tuple[str, str]:
    """
    根據當天日期決定要讀哪章箴言（1日→第1章，循環31章）。
    回傳 (中文經文, 英文經文)
    """
    if now is None:
        now = datetime.now(TAIPEI_TZ)

    day = now.day
    chapter = ((day - 1) % 31) + 1

    zh = _fetch_zh_chapter(chapter)
    en = _fetch_en_chapter(chapter)

    return zh, en


def get_proverbs_header(now: datetime = None) -> str:
    """產生每日箴言的標題行。"""
    if now is None:
        now = datetime.now(TAIPEI_TZ)

    day = now.day
    chapter = ((day - 1) % 31) + 1
    date_str = now.strftime("%-m月%-d日")

    return f"🕊️ {date_str} 每日箴言 — 第 {chapter} 章"
