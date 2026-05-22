import json
import os
from datetime import datetime

from google import genai


def parse_event(message: str, current_time: datetime) -> dict | None:
    """
    Uses Gemini to determine if a message is a calendar event.
    Returns structured event dict, or None if not an event.
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    weekday_map = {
        "Monday": "週一", "Tuesday": "週二", "Wednesday": "週三",
        "Thursday": "週四", "Friday": "週五", "Saturday": "週六", "Sunday": "週日",
    }
    weekday_zh = weekday_map.get(current_time.strftime("%A"), "")
    now_str = current_time.strftime(f"%Y-%m-%d %H:%M {weekday_zh}")

    prompt = f"""現在時間：{now_str}（台北時間，UTC+8）

分析這則 LINE 訊息，判斷是否包含需要加入行事曆的事件。

【必須同時符合以下兩點才算是事件】
1. 有明確的時間或日期（今天幾點、明天、下週幾、某月某日...）
2. 是未來要發生的事情或約定

【不算事件】
- 「我愛你老婆」→ 情感表達
- 「今天好累」→ 描述感受
- 「記得買牛奶」→ 無具體時間的備忘
- 「昨天去看了電影」→ 已發生
- 「你有沒有空？」→ 問句，非約定

【算事件】
- 「明天下午3點要開會」→ 事件
- 「週五晚上6點我們去吃飯」→ 事件
- 「下週二帶女兒去看醫生」→ 事件（無具體時間用09:00）
- 「5/28 要去台北出差」→ 事件

訊息：「{message}」

只回傳 JSON，不要加其他文字、不要加 markdown code block：
若是事件：{{"is_event": true, "title": "事件標題", "start": "2026-05-23T15:00:00+08:00", "end": "2026-05-23T16:00:00+08:00", "location": null, "description": null}}
若不是：{{"is_event": false}}

規則：
- start/end 必須是完整 ISO 8601 含時區，例如 "2026-05-23T15:00:00+08:00"
- 若無結束時間，end = start + 1小時（看診/醫療類 + 2小時）
- 若只有日期無時間，start 用 09:00
- title 用中文，簡潔10字以內
- location 若訊息中有提到地點則填入，否則為 null"""

    response = client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt
    )

    try:
        text = response.text.strip()
        # Strip markdown code blocks if Gemini adds them
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
        if result.get("is_event"):
            return result
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    return None
