import json
import os
from datetime import datetime

import anthropic
from google import genai
from google.genai import types


def _claude_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _call_claude(prompt: str) -> str:
    client = _claude_client()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def _gemini_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def parse_message(message: str, current_time: datetime) -> dict:
    weekday_map = {
        "Monday": "週一", "Tuesday": "週二", "Wednesday": "週三",
        "Thursday": "週四", "Friday": "週五", "Saturday": "週六", "Sunday": "週日",
    }
    weekday_zh = weekday_map.get(current_time.strftime("%A"), "")
    now_str = current_time.strftime(f"%Y-%m-%d %H:%M {weekday_zh}")

    prompt = f"""現在時間：{now_str}（台北時間，UTC+8）

分析這則 LINE 訊息，輸出以下六種 JSON 格式之一。

━━ 判斷規則 ━━

【weather】天氣查詢意圖
→ 輸出：{{"type": "weather", "period": "today"}}
  - period: "today"、"tomorrow"、"week"

【edit】修改特定行事曆活動
→ 輸出：{{"type": "edit", "target_datetime": "ISO8601+08:00", "has_time": true, "title_hint": null, "new_start": null, "new_location": null}}

【delete】刪除行事曆活動
→ 輸出：{{"type": "delete", "target_datetime": "ISO8601+08:00", "has_time": true, "title_hint": null}}

【query】查詢行事曆
→ 輸出：{{"type": "query", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "label": "今天"}}

【modify】修改剛才建立的行事曆（沒有指定目標）
→ 輸出：{{"type": "modify"}}

【calendar】新增行事曆事件
→ 輸出：{{"type": "calendar", "events": [{{"title": "標題", "start": "ISO8601+08:00", "end": "ISO8601+08:00", "location": null}}]}}

【todo】有任務性質但沒有明確時間
→ 輸出：{{"type": "todo", "title": "任務描述", "description": null}}

【ignore】日常聊天或一般問題
→ 輸出：{{"type": "ignore"}}

━━ 規則 ━━
- 每個獨立時間點 = 一筆獨立事件
- 若無結束時間：end = start + 1小時
- 若只有日期無時間：start 用 09:00
- 只回傳 JSON，不要加任何說明或```符號

訊息：「{message}」"""

    try:
        text = _call_claude(prompt)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception:
        return {"type": "ignore"}


def parse_new_datetime(text: str, current_time: datetime) -> str | None:
    now_str = current_time.strftime("%Y-%m-%d %H:%M")
    prompt = f"""現在時間：{now_str}（台北時間 UTC+8）

將以下自然語言時間轉換為 ISO 8601 格式（+08:00 時區），只輸出 JSON（不要加```）：
{{"datetime": "YYYY-MM-DDTHH:MM:SS+08:00"}}

如果無法解析，輸出：{{"datetime": null}}

輸入：「{text}」"""

    try:
        result = _call_claude(prompt)
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        return json.loads(result.strip()).get("datetime")
    except Exception:
        return None


def parse_image_for_event(image_bytes: bytes, mime_type: str, current_time: datetime) -> dict:
    client = _gemini_client()
    now_str = current_time.strftime("%Y-%m-%d %H:%M")

    prompt = f"""現在時間：{now_str}（台北時間 UTC+8）

分析這張圖片，提取其中的行事曆事件資訊。

如果圖片包含「日期或時間 + 活動名稱」，輸出：{{"type": "calendar", "events": [...]}}
若圖片沒有明確可建立的行事曆事件，輸出：{{"type": "no_event"}}

events 格式：title, start(ISO8601+08:00), end(ISO8601+08:00), location(或null)
- 若無結束時間：start + 1小時
- 只有日期無時間 → start 用 09:00
- 每個獨立時間點 = 一筆獨立事件"""

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "type": {"type": "STRING"},
            "events": {
                "type": "ARRAY",
                "items": {
                    "type":
