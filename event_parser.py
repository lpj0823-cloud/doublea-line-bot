import json
import os
from datetime import datetime

import anthropic
from google import genai
from google.genai import types

# 可用環境變數 ANTHROPIC_MODEL 覆寫。預設用目前有效的模型；
# 原本寫死的 "claude-sonnet-4-6" 並非有效的模型 ID，會導致每次呼叫都失敗。
# 目前可用選項（2026-09）：claude-haiku-4-5-20251001（便宜快速）、claude-sonnet-5（品質較高）。
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def _claude_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _call_claude(prompt: str) -> str:
    client = _claude_client()
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
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

分析這則 LINE 訊息，輸出以下幾種 JSON 格式之一。

【weather】天氣查詢
輸出：{{"type": "weather", "period": "today"}}
period: today / tomorrow / week

【edit】修改特定行事曆活動（關鍵字：改到、改成、更正、延到、延後、提前、換地點…）
輸出：{{"type": "edit", "target_datetime": "ISO8601+08:00", "has_time": true, "title_hint": null, "new_start": null, "new_location": null}}
target_datetime = 原本活動的時間（不知道幾點就用當天 09:00 並設 has_time=false）；new_start = 新的時間；title_hint = 活動名稱關鍵字。
例：「更正明天的行程，本來明天要去依伶家改到星期五早上10點」→ target_datetime=明天09:00、has_time=false、title_hint="依伶"、new_start=星期五10:00

【delete】刪除行事曆活動
輸出：{{"type": "delete", "target_datetime": "ISO8601+08:00", "has_time": true, "title_hint": null}}

【query】查詢行事曆
輸出：{{"type": "query", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "label": "今天"}}

【modify】修改剛才建立的行事曆
輸出：{{"type": "modify"}}

【calendar】新增行事曆事件
輸出：{{"type": "calendar", "events": [{{"title": "標題", "start": "ISO8601+08:00", "end": "ISO8601+08:00", "location": null}}]}}

【todo】有任務性質但沒有明確時間
輸出：{{"type": "todo", "title": "任務描述", "description": null}}

【ignore】日常聊天或一般問題
輸出：{{"type": "ignore"}}

規則：
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


def parse_image_for_event(
    image_bytes: bytes,
    mime_type: str,
    current_time: datetime,
    text_hint: str | None = None,
) -> dict:
    """用 Gemini Vision 分析圖片，提取行事曆事件。

    text_hint：使用者傳照片後接著輸入的觸發語文字（例如「加到行事曆10/5」）。
    若照片本身沒有明確日期／標題，優先採用 text_hint 裡的日期／時間／標題線索，
    而不是只看照片內容。
    """
    client = _gemini_client()
    now_str = current_time.strftime("%Y-%m-%d %H:%M")

    hint_block = ""
    if text_hint and text_hint.strip():
        hint_block = f"""

使用者傳這張照片後，接著輸入了這段文字：「{text_hint.strip()}」
- 如果照片本身沒有清楚的日期／時間，但這段文字裡有（例如「10/5」「明天下午3點」），請優先採用文字裡的日期／時間。
- 如果照片沒有清楚的活動標題，可依照片內容或這段文字，給一個合理的標題（例如「午餐」「聚餐」）。"""

    prompt = f"""現在時間：{now_str}（台北時間 UTC+8）

分析這張圖片，提取其中的行事曆事件資訊。{hint_block}

如果圖片或上面的文字包含「日期或時間 + 活動內容」，輸出：
{{"type": "calendar", "events": [{{"title": "活動名稱", "start": "ISO8601+08:00", "end": "ISO8601+08:00", "location": null}}]}}

若圖片和文字都沒有明確可建立的行事曆事件（例如純粹一張食物照，也沒有提到任何日期），輸出：{{"type": "no_event"}}

規則：
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
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "start": {"type": "STRING"},
                        "end": {"type": "STRING"},
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
    now_str = current_time.strftime("%Y-%m-%d %H:%M")

    prompt = f"""現在時間：{now_str}（台北時間，UTC+8）

原始行事曆事件：
- 標題：{original["title"]}
- 開始：{original["start"]}
- 結束：{original["end"]}

修改指令：「{instruction}」

根據修改指令計算修改後的新時間，只回傳 JSON（不要加```）：
{{"start": "新的ISO8601+08:00", "end": "新的ISO8601+08:00"}}

規則：
- 只改日期時，保留原本的時間
- 只改時間時，保留原本的日期
- end 與 start 的時間差距保持不變"""

    try:
        text = _call_claude(prompt)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception:
        return None
