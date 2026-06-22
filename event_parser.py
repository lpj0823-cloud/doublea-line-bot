import json
import os
from datetime import datetime

from google import genai
from google.genai import types


def parse_message(message: str, current_time: datetime) -> dict:
    """
    Classifies a LINE message into one of four types:
    - {"type": "calendar", "events": [{"title", "start", "end", "location"}, ...]}
    - {"type": "todo", "title": ..., "description": ...}
    - {"type": "modify"}
    - {"type": "ignore"}
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    weekday_map = {
        "Monday": "週一", "Tuesday": "週二", "Wednesday": "週三",
        "Thursday": "週四", "Friday": "週五", "Saturday": "週六", "Sunday": "週日",
    }
    weekday_zh = weekday_map.get(current_time.strftime("%A"), "")
    now_str = current_time.strftime(f"%Y-%m-%d %H:%M {weekday_zh}")

    prompt = f"""現在時間：{now_str}（台北時間，UTC+8）

分析這則 LINE 訊息，輸出以下六種 JSON 格式之一。

━━ 判斷規則 ━━

【weather】天氣查詢意圖（如「今天天氣怎樣」「明天會不會下雨」「這週天氣如何」「週末天氣」「會颱風嗎」）
→ 輸出：{{"type": "weather", "period": "today"}}
  - period: "today"（今天）、"tomorrow"（明天/後天/明後天）、"week"（這週/週末/未來幾天）

【edit】修改特定行事曆活動（使用者指定了「目標事件」的時間或標題，並說明要改什麼）
例：「把明天下午3點的會議改到4點」「把今天的禱告會改到教會」「把後天9點的課改到10點半」
→ 輸出：{{"type": "edit", "target_datetime": "ISO8601+08:00", "has_time": true, "title_hint": "會議",
         "new_start": "ISO8601+08:00", "new_location": null}}
  - target_datetime: 目標事件「目前」的時間（目前的時間）
  - has_time: 是否明確指定目標時間（true/false）
  - title_hint: 目標事件的名稱關鍵字；通用詞（「活動」「行程」「事」）填 null
  - new_start: 若改時間則填新的開始時間（ISO8601+08:00），不改則填 null；系統會自動保留原有時長
  - new_location: 若改地點則填新地點，不改則填 null

【delete】刪除/取消行事曆活動意圖（如「刪除明天下午3點的會議」「取消今天5點的活動」「刪掉後天的牙醫」）
→ 輸出：{{"type": "delete", "target_datetime": "ISO8601+08:00", "has_time": true, "title_hint": "會議"}}
  - has_time: 使用者是否明確提到時間（true/false）
  - title_hint: 活動名稱的關鍵字（如「會議」「牙醫」「課」）；通用詞（「活動」「行程」「事」）或無則填 null
  - 若沒有指定時間，target_datetime 設為當天 00:00，has_time 設 false

【query】查詢行事曆意圖（如「今天有什麼事」「明天的行程」「這週有什麼」「6月25日有什麼」「下週有沒有事」）
→ 輸出：{{"type": "query", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "label": "今天"}}
  - 單日查詢：start_date = end_date（同一天）
  - 週查詢（這週/本週/下週）：start_date = 該週週一，end_date = 該週週日
  - label 用自然中文，如「今天」「明天」「6月25日」「這週」「下週」

【modify】含有「修正/更改/改一下/調整/修改」且「沒有指定目標事件時間或標題」，暗指剛才建立的那個行事曆
→ 輸出：{{"type": "modify"}}

【calendar】含有「明確時間/日期 + 未來的事（新增/建立意圖）」
→ 輸出：{{"type": "calendar", "events": [...]}}
⚠️ events 必須是陣列，每個獨立的時間點都是一筆獨立事件
⚠️ 不可把第二個時間點當作第一個事件的 end，它們是兩個不同事件

【todo】有任務性質但沒有明確時間
→ 輸出：{{"type": "todo", "title": "任務簡短描述", "description": null}}

【ignore】日常聊天、情感、已發生的事
→ 輸出：{{"type": "ignore"}}

━━ 範例 ━━

訊息：「今天有什麼事」→ {{"type": "query", "start_date": "{now_str[:10]}", "end_date": "{now_str[:10]}", "label": "今天"}}
訊息：「這週有什麼行程」→ {{"type": "query", "start_date": "（該週週一）", "end_date": "（該週週日）", "label": "這週"}}
訊息：「明天早上7點半出發去台北，後天晚上10點回到玉里」→
{{"type": "calendar", "events": [
  {{"title": "出發去台北", "start": "ISO8601+08:00", "end": "ISO8601+08:00", "location": "台北"}},
  {{"title": "回到玉里", "start": "ISO8601+08:00", "end": "ISO8601+08:00", "location": "玉里"}}
]}}

━━ 現在分析這則訊息 ━━

訊息：「{message}」

只回傳 JSON，不要加任何說明或 markdown。

events 陣列格式：每筆事件 = {{"title": "中文標題10字內", "start": "ISO8601+08:00", "end": "ISO8601+08:00", "location": null或地點}}

規則：
- 每個獨立的時間點 = 一筆獨立事件，不可合併
- 若無結束時間：end = start + 1小時（醫療/出行類 + 2小時）
- 若只有日期無時間：start 用 09:00"""

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "type": {"type": "STRING"},
            "events": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title":    {"type": "STRING"},
                        "start":    {"type": "STRING"},
                        "end":      {"type": "STRING"},
                        "location": {"type": "STRING"},
                    },
                    "required": ["title", "start", "end"],
                },
            },
            "title":       {"type": "STRING"},
            "description": {"type": "STRING"},
            "start_date":       {"type": "STRING"},
            "end_date":         {"type": "STRING"},
            "label":            {"type": "STRING"},
            "target_datetime":  {"type": "STRING"},
            "has_time":         {"type": "BOOLEAN"},
            "title_hint":       {"type": "STRING"},
            "new_start":        {"type": "STRING"},
            "new_location":     {"type": "STRING"},
            "period":           {"type": "STRING"},
        },
        "required": ["type"],
    }

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )

    try:
        return json.loads(response.text.strip())
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"type": "ignore"}


def parse_new_datetime(text: str, current_time: datetime) -> str | None:
    """Convert natural language time to ISO8601+08:00 string. Returns None if unparseable."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    now_str = current_time.strftime("%Y-%m-%d %H:%M")
    prompt = f"""現在時間：{now_str}（台北時間 UTC+8）

將以下自然語言時間轉換為 ISO 8601 格式（+08:00 時區），只輸出 JSON：
{{"datetime": "YYYY-MM-DDTHH:MM:SS+08:00"}}

如果無法解析，輸出：{{"datetime": null}}

輸入：「{text}」"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {"datetime": {"type": "STRING"}},
            },
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    try:
        return json.loads(response.text.strip()).get("datetime")
    except Exception:
        return None


def parse_image_for_event(image_bytes: bytes, mime_type: str, current_time: datetime) -> dict:
    """用 Gemini Vision 分析圖片，提取行事曆事件。
    回傳 {"type": "calendar", "events": [...]} 或 {"type": "no_event"}。
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    now_str = current_time.strftime("%Y-%m-%d %H:%M")

    prompt = f"""現在時間：{now_str}（台北時間 UTC+8）

分析這張圖片，提取其中的行事曆事件資訊。

如果圖片包含「日期或時間 + 活動名稱」（例如：活動宣傳、行程表、邀請函、課表、截圖），
輸出：{{"type": "calendar", "events": [...]}}

若圖片沒有明確可建立的行事曆事件，輸出：{{"type": "no_event"}}

events 格式：
- title: 活動名稱（10字內）
- start: ISO8601+08:00
- end: ISO8601+08:00（若無結束時間：start + 1小時；醫療/出行類 + 2小時）
- location: 地點或 null
- 只有日期無時間 → start 用 09:00
- 年份不明確 → 使用最近的未來日期
- 每個獨立時間點 = 一筆獨立事件"""

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "type": {"type": "STRING"},
            "events": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title":    {"type": "STRING"},
                        "start":    {"type": "STRING"},
                        "end":      {"type": "STRING"},
                        "location": {"type": "STRING"},
                    },
                    "required": ["title", "start", "end"],
                },
            },
        },
        "required": ["type"],
    }

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            types.Part.from_text(text=prompt),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    try:
        return json.loads(response.text.strip())
    except Exception:
        return {"type": "no_event"}


def parse_modification(instruction: str, original: dict, current_time: datetime) -> dict | None:
    """
    Given a modification instruction and original event, returns updated start/end.
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    now_str = current_time.strftime("%Y-%m-%d %H:%M")

    prompt = f"""現在時間：{now_str}（台北時間，UTC+8）

原始行事曆事件：
- 標題：{original["title"]}
- 開始：{original["start"]}
- 結束：{original["end"]}

修改指令：「{instruction}」

根據修改指令計算修改後的新時間，只回傳 JSON，不要加其他文字：
{{"start": "新的ISO8601+08:00", "end": "新的ISO8601+08:00"}}

規則：
- 只改日期時，保留原本的時間
- 只改時間時，保留原本的日期
- end 與 start 的時間差距保持不變"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0)
        ),
    )

    try:
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except (json.JSONDecodeError, KeyError, IndexError):
        return None
