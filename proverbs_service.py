import requests
from datetime import datetime
import pytz

TAIPEI_TZ = pytz.timezone("Asia/Taipei")

# 信望愛站聖經 JSON API（免費，繁體中文新標點和合本）
FHL_API = "https://bible.fhl.net/json/qb.php"
# Bible API（免費，NIV 英文）
BIBLE_API = "https://bible-api.com"


def _fetch_zh_chapter(chapter: int) -> str:
    """從信望愛站 API 抓取箴言指定章（繁體中文新標點和合本）。"""
    try:
        r = requests.get(
            FHL_API,
            params={
                "chineses": "箴",
                "chap": chapter,
                "gb": 0,
                "version": "cunp",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        if data.get("status") != "success":
            return f"⚠️ 無法取得箴言第{chapter}章（中文）"

        lines = [f"📖 箴言 第{chapter}章（新標點和合本）\n"]
        for rec in data.get("record", []):
            sec = rec.get("sec", "")
            text = rec.get("bible_text", "").strip()
            if sec and text:
                lines.append(f"{sec} {text}")

        return "\n".join(lines)

    except Exception as e:
        print(f"[DoubleA] 箴言中文API失敗：{e}")
        return f"⚠️ 箴言第{chapter}章（中文）暫時無法取得，請稍後再試。"


def _fetch_en_chapter(chapter: int) -> str:
    """從 Bible API 抓取箴言指定章（NIV 英文）。"""
    try:
        r = requests.get(
            f"{BIBLE_API}/proverbs+{chapter}",
            params={"translation": "NIV"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        lines = [f"📖 Proverbs Chapter {chapter} (NIV)\n"]
        for verse in data.get("verses", []):
            v = verse.get("verse", "")
            text = verse.get("text", "").strip()
            if v and text:
                lines.append(f"{v} {text}")

        return "\n".join(lines)

    except Exception as e:
        print(f"[DoubleA] 箴言英文API失敗：{e}")
        return f"⚠️ Proverbs Chapter {chapter} (NIV) temporarily unavailable."


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
