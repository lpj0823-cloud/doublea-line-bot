import os
from collections import Counter
from datetime import datetime

import pytz
import requests

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TAIPEI_LAT = 25.0478
TAIPEI_LON = 121.5318

# 順序重要：先比對較具體的關鍵字
_EMOJI_MAP = [
    ("雷", "⛈️"), ("雪", "❄️"), ("大雨", "🌧️"), ("中雨", "🌧️"), ("小雨", "🌦️"),
    ("毛毛雨", "🌦️"), ("陣雨", "🌦️"), ("雨", "🌧️"), ("霧", "🌫️"),
    ("陰", "☁️"), ("多雲", "⛅"), ("少雲", "🌤️"), ("晴", "☀️"),
]


def _emoji(desc: str) -> str:
    for keyword, emoji in _EMOJI_MAP:
        if keyword in desc:
            return emoji
    return "🌤️"


def get_current_weather() -> dict:
    """取得台北現在天氣（Current Weather API）。"""
    key = os.environ["WEATHER_API_KEY"]
    r = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={
            "lat": TAIPEI_LAT, "lon": TAIPEI_LON,
            "appid": key, "units": "metric", "lang": "zh_tw",
        },
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    desc = d["weather"][0]["description"]
    return {
        "temp": round(d["main"]["temp"]),
        "feels_like": round(d["main"]["feels_like"]),
        "description": desc,
        "humidity": d["main"]["humidity"],
        "emoji": _emoji(desc),
    }


def get_daily_forecast(num_days: int = 5) -> list[dict]:
    """
    5 日 / 3 小時預報 → 彙整為每日摘要，最多回傳 num_days 天。
    每日欄位：label, emoji, description, temp_min, temp_max, pop（降雨機率 %）
    """
    key = os.environ["WEATHER_API_KEY"]
    r = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={
            "lat": TAIPEI_LAT, "lon": TAIPEI_LON,
            "appid": key, "units": "metric", "lang": "zh_tw", "cnt": 40,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    now = datetime.now(TAIPEI_TZ)
    by_date: dict = {}
    for item in data["list"]:
        dt = datetime.fromtimestamp(item["dt"], tz=TAIPEI_TZ)
        by_date.setdefault(dt.date(), []).append(item)

    summaries = []
    for date, items in sorted(by_date.items()):
        if len(summaries) >= num_days:
            break
        temps = [it["main"]["temp"] for it in items]
        pops = [it.get("pop", 0) for it in items]
        descs = [it["weather"][0]["description"] for it in items]
        desc = Counter(descs).most_common(1)[0][0]

        diff = (date - now.date()).days
        if diff == 0:
            label = "今天"
        elif diff == 1:
            label = "明天"
        elif diff == 2:
            label = "後天"
        else:
            label = date.strftime("%-m/%-d")

        summaries.append({
            "date": date,
            "label": label,
            "temp_min": round(min(temps)),
            "temp_max": round(max(temps)),
            "pop": round(max(pops) * 100),
            "description": desc,
            "emoji": _emoji(desc),
        })
    return summaries
