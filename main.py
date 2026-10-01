import base64
import hashlib
import hmac
import json
import os
import re
import random
import time
import urllib.parse

import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessageAction,
    MessagingApi,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)

from calendar_service import (
    create_calendar_event, delete_event_by_id, find_and_delete_event, find_and_update_event,
    list_events_for_date, list_events_for_range, list_upcoming_events, update_calendar_event,
    update_event_field_by_id,
)
from weather_service import get_current_weather, get_daily_forecast
from event_parser import parse_image_for_event, parse_message, parse_modification, parse_new_datetime
from state_service import (
    add_reminder,
    clear_pending_edit,
    clear_pending_image,
    get_due_reminders,
    load_chat_id,
    load_last_event,
    load_state,
    save_state,
    load_pending_edit,
    load_pending_image,
    mark_reminder_sent,
    save_chat_id,
    save_last_event,
    save_pending_edit,
    save_pending_image,
)
from todo_service import (
    TASKS_URL,
    add_task,
    complete_task_by_index,
    complete_task_by_keyword,
    get_pending_tasks,
)
from shopping_service import (
    add_items,
    clear_done,
    get_items,
    mark_done_by_index,
    mark_done_by_keyword,
)
from notes_service import add_note, delete_note_by_index, get_notes
from proverbs_service import get_todays_proverbs, get_proverbs_header, get_daily_verses
from rate_limiter import check_rate_limit
from paths import is_persistent
from google_auth import auth_error_hint, is_auth_expired_error
from birthday_service import (
    add_birthday,
    delete_birthday_by_index,
    get_birthdays,
    get_todays_birthdays,
    parse_birthday_date,
)
from restaurant_service import search_nearby_restaurants, format_restaurant_results
from finance_service import (
    create_project, get_project, list_projects, delete_project,
    parse_expense_input, add_expense, get_expenses,
    get_last_expense_any, attach_location, mark_photo_noted,
    format_expense_confirmation, format_photo_reminder,
    format_location_attached, format_project_list, format_project_detail,
    parse_receipt_image, format_receipt_result, add_receipt_expense,
)

load_dotenv()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"].strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
REMINDER_MINUTES = 120
BOT_NAME = "培正家AI小幫手"
BOT_MENTION = f"@{BOT_NAME}"

# 圖片＋文字說明才觸發行事曆：
# 傳照片後，只有在 PENDING_IMAGE_TTL_SECONDS 秒內接著輸入下列關鍵字，
# 才會用那張照片建立行事曆事件；單純傳照片（例如吃飯照）不會自動建立行程。
PENDING_IMAGE_TTL_SECONDS = 300  # 5 分鐘
CALENDAR_TRIGGER_KEYWORDS = (
    "行事曆",
    "建立行程",
    "加行程",
    "排行程",
    "加到行程",
    "排進行事曆",
)


def _is_calendar_trigger(text: str) -> bool:
    return any(kw in text for kw in CALENDAR_TRIGGER_KEYWORDS)

scheduler = AsyncIOScheduler(timezone=TAIPEI_TZ)


def _gerr(e: Exception, default: str) -> str:
    """Google 授權過期時給明確提示，其他錯誤用原本的訊息。"""
    return auth_error_hint(e) if is_auth_expired_error(e) else default


def _push_target() -> str | None:
    """排程推播的目的地：優先用環境變數 PUSH_CHAT_ID（建議設成「培正家」群組 ID），
    否則用最近一次記住的群組。"""
    return os.environ.get("PUSH_CHAT_ID") or load_chat_id()


def _env_on(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── 推播額度 ─────────────────────────────────────────────────────────────────
# LINE 免費方案每月 200 則；推播到群組時「則數 = 群組人數」，6 人群組推一次就算 5～6 則。
# 因此預設每天只推一次「早安摘要」（多個對話框放在同一次推播，只算一次人數）。
# 想要 18:00 晚間摘要或「開始前 2 小時提醒」，升級方案後把下列環境變數設成 true：
#   ENABLE_EVENING_PUSH=true、ENABLE_EVENT_REMINDER=true
# 這兩種「選用推播」會先檢查剩餘額度，確保月底前每天早安摘要還推得出去。

def _line_get(path: str) -> dict:
    r = requests.get(
        f"https://api.line.me{path}",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _recipients(chat_id: str) -> int:
    """一則推播會被算成幾則（群組 = 成員數）。查不到就保守估 6。"""
    try:
        if chat_id.startswith("C"):
            return int(_line_get(f"/v2/bot/group/{chat_id}/members/count").get("count", 6))
        if chat_id.startswith("R"):
            return int(_line_get(f"/v2/bot/room/{chat_id}/members/count").get("count", 6))
        return 1
    except Exception as e:
        print(f"[DoubleA] 查詢群組人數失敗：{e}")
        return 6


def _quota_remaining() -> int | None:
    """本月剩餘可推播則數；無上限或查不到時回傳 None。"""
    try:
        quota = _line_get("/v2/bot/message/quota")
        if quota.get("type") != "limited":
            return None
        used = _line_get("/v2/bot/message/quota/consumption").get("totalUsage", 0)
        return int(quota.get("value", 0)) - int(used)
    except Exception as e:
        print(f"[DoubleA] 查詢推播額度失敗：{e}")
        return None


def _optional_push_allowed(chat_id: str) -> bool:
    """選用推播（晚間摘要、行程提醒）：剩餘額度要先保留到月底每天的早安摘要。"""
    remaining = _quota_remaining()
    if remaining is None:
        return True
    now = datetime.now(TAIPEI_TZ)
    next_month = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_left = (next_month.date() - now.date()).days  # 含今天
    per_push = _recipients(chat_id)
    reserve = per_push * days_left
    ok = remaining - per_push >= reserve
    if not ok:
        print(f"[DoubleA] 額度保留給早安摘要：剩 {remaining} 則、需保留 {reserve} 則，略過選用推播")
    return ok


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 預設每天只推一次：07:00 早安摘要（今日行程＋天氣＋壽星＋精選箴言，同一次推播）。
    scheduler.add_job(morning_digest_job, CronTrigger(hour=7, minute=0, timezone=TAIPEI_TZ))
    jobs = ["07:00 早安摘要"]
    if _env_on("ENABLE_EVENING_PUSH"):
        scheduler.add_job(evening_digest_job, CronTrigger(hour=18, minute=0, timezone=TAIPEI_TZ))
        jobs.append("18:00 晚間摘要")
    if _env_on("ENABLE_EVENT_REMINDER"):
        scheduler.add_job(reminder_check_job, "interval", minutes=5)
        jobs.append("行程前 2 小時提醒")
    scheduler.start()
    print(f"[DoubleA] 排程器已啟動：{'、'.join(jobs)}；資料永久保存：{is_persistent()}")
    yield
    scheduler.shutdown()
    print("[DoubleA] 排程器已停止")


app = FastAPI(title="DoubleA LINE Bot", lifespan=lifespan)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


# ── LINE push ─────────────────────────────────────────────────────────────────

def _push_line(chat_id: str, text: str) -> None:
    try:
        chunks = _chunk_text(text)
        for i in range(0, len(chunks), 5):
            with ApiClient(line_config) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(
                        to=chat_id,
                        messages=[TextMessage(text=c) for c in chunks[i:i + 5]],
                    )
                )
    except Exception as e:
        print(f"[DoubleA] push_message 失敗：{e}")
        raise


def _reply_line(reply_token: str, text: str) -> None:
    """使用 reply_token 回覆（免費，不計入月限額）。"""
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    # 超過 5000 字自動分段（一次回覆最多 5 個對話框），避免被 LINE 拒收後改用付費推播
                    messages=[TextMessage(text=c) for c in _chunk_text(text)[:5]],
                )
            )
    except Exception as e:
        print(f"[DoubleA] reply_message 失敗：{e}")
        raise


def _send_line_msg(chat_id: str, msg_obj, reply_token: str | None, _used: list[bool] | None) -> None:
    """reply_token 未用過時用 reply_message（免費），否則 fallback 到 push_message。"""
    if reply_token and _used is not None and not _used[0]:
        try:
            with ApiClient(line_config) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(reply_token=reply_token, messages=[msg_obj])
                )
            _used[0] = True
            return
        except Exception as e:
            print(f"[DoubleA] reply_message 失敗，改用 push：{e}")
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).push_message(PushMessageRequest(to=chat_id, messages=[msg_obj]))


LINE_TEXT_LIMIT = 4500  # LINE 單則文字上限 5000 字，保守取 4500


def _chunk_text(text: str, limit: int = LINE_TEXT_LIMIT) -> list[str]:
    """把長文字依換行切成多段，每段不超過 limit 字。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # 單行就超長（例如超長網址）→ 硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def _send_texts(chat_id: str, texts: list[str], reply_token: str | None = None,
                _used: list[bool] | None = None) -> None:
    """一次送出多段文字：每段自動切在 5000 字以內；
    reply_token 尚未用過時，前 5 則走免費 reply，其餘（或 reply 失敗時）走 push。"""
    msgs: list[str] = []
    for t in texts:
        msgs.extend(_chunk_text(t))
    rest = msgs
    if reply_token and _used is not None and not _used[0]:
        try:
            with ApiClient(line_config) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text=m) for m in msgs[:5]],
                    )
                )
            _used[0] = True
            rest = msgs[5:]
        except Exception as e:
            print(f"[DoubleA] reply_message 失敗，改用 push：{e}")
    for i in range(0, len(rest), 5):
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=chat_id, messages=[TextMessage(text=m) for m in rest[i:i + 5]])
            )


def _push_delete_picker(chat_id: str, events: list[dict], reply_token: str | None = None, _used: list[bool] | None = None) -> None:
    lines = ["以下是未來 7 天的行程，請點選要刪除的：\n"]
    qr_items = []
    for ev in events:
        time_part = f"{ev['start_str']} " if ev.get("start_str") else ""
        lines.append(f"📅 {ev['date_str']} {time_part}【{ev['title']}】")
        if len(qr_items) < 13:
            label = f"{ev['date_str']} {time_part}{ev['title']}"[:20]
            qr_items.append(QuickReplyItem(action=MessageAction(label=label, text=f"確認刪除 {ev['id']}")))
    msg = TextMessage(text="\n".join(lines), quick_reply=QuickReply(items=qr_items))
    _send_line_msg(chat_id, msg, reply_token, _used)


def _push_edit_picker(chat_id: str, events: list[dict], reply_token: str | None = None, _used: list[bool] | None = None) -> None:
    lines = ["以下是未來 7 天的行程，請點選要修改的：\n"]
    qr_items = []
    for ev in events:
        time_part = f"{ev['start_str']} " if ev.get("start_str") else ""
        lines.append(f"📅 {ev['date_str']} {time_part}【{ev['title']}】")
        if len(qr_items) < 13:
            label = f"{ev['date_str']} {time_part}{ev['title']}"[:20]
            qr_items.append(QuickReplyItem(action=MessageAction(label=label, text=f"選擇修改 {ev['id']} {ev['title']}")))
    msg = TextMessage(text="\n".join(lines), quick_reply=QuickReply(items=qr_items))
    _send_line_msg(chat_id, msg, reply_token, _used)


def _push_field_picker(chat_id: str, event_id: str, event_title: str, reply_token: str | None = None, _used: list[bool] | None = None) -> None:
    qr_items = [
        QuickReplyItem(action=MessageAction(label="✏️ 標題", text=f"修改欄位 title {event_id} {event_title}")),
        QuickReplyItem(action=MessageAction(label="🕐 時間", text=f"修改欄位 start {event_id} {event_title}")),
        QuickReplyItem(action=MessageAction(label="📍 地點", text=f"修改欄位 location {event_id} {event_title}")),
    ]
    msg = TextMessage(text=f"要修改【{event_title}】的哪個欄位？", quick_reply=QuickReply(items=qr_items))
    _send_line_msg(chat_id, msg, reply_token, _used)


def _push_shopping_list(chat_id: str, items: list[dict], reply_token: str | None = None, _used: list[bool] | None = None) -> None:
    pending = [it for it in items if not it["done"]]
    done = [it for it in items if it["done"]]
    if not pending and not done:
        _send_line_msg(chat_id, TextMessage(text="🛒 購物清單是空的！\n\n用「+買 物品名稱」新增品項"), reply_token, _used)
        return
    lines = ["🛒 購物清單\n"]
    for i, it in enumerate(pending, 1):
        lines.append(f"{i}. {it['name']}")
    if done:
        lines.append("")
        for it in done:
            lines.append(f"✅ {it['name']}")
    if pending:
        lines.append("\n點下方按鈕標記買到")
    qr_items = []
    for i, it in enumerate(pending, 1):
        if len(qr_items) >= 12:
            break
        label = f"✅ {i}. {it['name']}"[:20]
        qr_items.append(QuickReplyItem(action=MessageAction(label=label, text=f"買到 {i}")))
    if done:
        qr_items.append(QuickReplyItem(action=MessageAction(label="🗑 清除已買", text="清除已買")))
    if qr_items:
        msg = TextMessage(text="\n".join(lines), quick_reply=QuickReply(items=qr_items))
    else:
        msg = TextMessage(text="\n".join(lines))
    _send_line_msg(chat_id, msg, reply_token, _used)


def _push_notes(chat_id: str, notes: list[dict], reply_token: str | None = None, _used: list[bool] | None = None) -> None:
    if not notes:
        _send_line_msg(chat_id, TextMessage(text="📓 筆記本是空的！\n\n用「+記 內容」新增筆記"), reply_token, _used)
        return
    lines = [f"📓 筆記本（{len(notes)} 筆）\n"]
    for i, note in enumerate(notes, 1):
        lines.append(f"{i}. [{note['created_at']}]\n   {note['content']}")
    qr_items = []
    for i, note in enumerate(notes, 1):
        if len(qr_items) >= 13:
            break
        label = f"🗑 {i}. {note['content']}"[:20]
        qr_items.append(QuickReplyItem(action=MessageAction(label=label, text=f"刪筆記 {i}")))
    if qr_items:
        msg = TextMessage(text="\n\n".join(lines), quick_reply=QuickReply(items=qr_items))
    else:
        msg = TextMessage(text="\n\n".join(lines))
    _send_line_msg(chat_id, msg, reply_token, _used)


def _push_birthday_list(chat_id: str, birthdays: list[dict], reply_token: str | None = None, _used: list[bool] | None = None) -> None:
    if not birthdays:
        _send_line_msg(chat_id, TextMessage(text="🎂 生日清單是空的！\n\n用「+生日 名字 月/日」新增"), reply_token, _used)
        return
    lines = [f"🎂 生日清單（{len(birthdays)} 筆）\n"]
    for i, b in enumerate(birthdays, 1):
        year_part = f"（{b['year']}年）" if b.get("year") else ""
        lines.append(f"{i}. {b['name']}｜{b['month']}月{b['day']}日{year_part}")
    qr_items = []
    for i, b in enumerate(birthdays, 1):
        if len(qr_items) >= 13:
            break
        label = f"🗑 {i}. {b['name']}"[:20]
        qr_items.append(QuickReplyItem(action=MessageAction(label=label, text=f"刪生日 {i}")))
    if qr_items:
        msg = TextMessage(text="\n".join(lines), quick_reply=QuickReply(items=qr_items))
    else:
        msg = TextMessage(text="\n".join(lines))
    _send_line_msg(chat_id, msg, reply_token, _used)


def birthday_reminder_job() -> None:
    chat_id = _push_target()
    if not chat_id:
        return
    now = datetime.now(TAIPEI_TZ)
    birthdays = get_todays_birthdays(now.month, now.day)
    if not birthdays:
        return
    sections = []
    for b in birthdays:
        lines = [f"🎂 今天是【{b['name']}】的生日！"]
        if b.get("year"):
            age = now.year - b["year"]
            lines.append(f"（{b['year']} 年生，今年滿 {age} 歲）")
        sections.append("\n".join(lines))
    msg = "\n\n".join(sections) + "\n\n🎉 祝生日快樂、平安喜樂！"
    try:
        _push_line(chat_id, msg)
        print(f"[DoubleA] 生日提醒發送：{', '.join(b['name'] for b in birthdays)}")
    except Exception as e:
        print(f"[DoubleA] 生日提醒發送失敗：{e}")


def _format_day_events(events: list[dict]) -> str:
    lines = []
    for ev in events:
        time_part = f"{ev['start_str']} " if ev.get("start_str") else ""
        loc = ev.get("location", "")
        loc_part = f" 📍{loc}" if (loc and loc != "null") else ""
        lines.append(f"📅 {time_part}【{ev['title']}】{loc_part}")
    return "\n".join(lines)


def morning_calendar_job() -> None:
    """每日 06:00 排程：推播『今天』的行事曆。"""
    chat_id = _push_target()
    if not chat_id:
        print("[DoubleA] 早上6點排程：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    date_str = now.strftime("%-m月%-d日")
    try:
        events = list_events_for_date(now)
    except Exception as e:
        print(f"[DoubleA] 早上6點行事曆取得失敗：{e}")
        return
    if events:
        msg = f"☀️ 早安！今天是 {date_str}，今日行程：\n\n{_format_day_events(events)}"
    else:
        msg = f"☀️ 早安！今天是 {date_str}\n\n今天沒有行程，祝您有美好的一天！"
    try:
        _push_line(chat_id, msg)
        print(f"[DoubleA] 早上6點今日行事曆已發送：{len(events)} 筆")
    except Exception as e:
        print(f"[DoubleA] 早上6點推播失敗：{e}")


def evening_calendar_job() -> None:
    """每日 18:00 排程：推播『明天』的行事曆。"""
    chat_id = _push_target()
    if not chat_id:
        print("[DoubleA] 晚上6點排程：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    tomorrow = now + timedelta(days=1)
    date_str = tomorrow.strftime("%-m月%-d日")
    try:
        events = list_events_for_date(tomorrow)
    except Exception as e:
        print(f"[DoubleA] 晚上6點行事曆取得失敗：{e}")
        return
    if events:
        msg = f"🌙 提醒：明天 {date_str} 的行程：\n\n{_format_day_events(events)}"
    else:
        msg = f"🌙 晚安！明天（{date_str}）目前沒有安排行程。"
    try:
        _push_line(chat_id, msg)
        print(f"[DoubleA] 晚上6點明日行事曆已發送：{len(events)} 筆")
    except Exception as e:
        print(f"[DoubleA] 晚上6點推播失敗：{e}")


def proverbs_job() -> None:
    """每日 07:05 排程：推播今日箴言（中文＋英文各一則，自動分段避免超過 5000 字）。"""
    chat_id = _push_target()
    if not chat_id:
        print("[DoubleA] 箴言排程：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    header = get_proverbs_header(now)
    try:
        zh_text, en_text = get_daily_verses(now)
    except Exception as e:
        print(f"[DoubleA] 箴言排程：取得經文失敗 {e}")
        return
    try:
        _send_texts(chat_id, [f"🕊️ {now.strftime('%-m月%-d日')} 今日箴言\n\n{zh_text}\n\n{en_text}\n\n（傳「箴言」看整章）"])
        print("[DoubleA] 箴言發送成功")
    except Exception as e:
        print(f"[DoubleA] 箴言發送失敗：{e}")


def _push_bundle(chat_id: str, texts: list[str]) -> None:
    """把多段文字放在「同一次」推播（最多 5 個對話框），額度只算一次群組人數。"""
    bubbles: list[str] = []
    for t in texts:
        bubbles.extend(_chunk_text(t))
    if len(bubbles) > 5:
        print(f"[DoubleA] 推播內容超過 5 個對話框，只送前 5 個（原 {len(bubbles)} 個）")
        bubbles = bubbles[:5]
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=chat_id, messages=[TextMessage(text=b) for b in bubbles])
        )


def _weather_line() -> str | None:
    try:
        current = get_current_weather()
        forecasts = get_daily_forecast(1)
        if not forecasts:
            return f"🌦 天氣｜{current['emoji']} {current['description']}，現在 {current['temp']}°C"
        fc = forecasts[0]
        pop_str = f"🌂 降雨 {fc['pop']}%" if fc["pop"] > 0 else "☀️ 不會下雨"
        return (
            f"🌦 今日天氣｜{current['emoji']} {current['description']}\n"
            f"🌡 現在 {current['temp']}°C，今日 {fc['temp_min']}～{fc['temp_max']}°C　{pop_str}"
        )
    except Exception as e:
        print(f"[DoubleA] 天氣取得失敗：{e}")
        return None


def build_morning_digest(now: datetime) -> list[str]:
    """早安摘要內容：[行程＋天氣＋壽星, 精選箴言]（兩個對話框）。"""
    head = f"☀️ 早安！今天是 {now.strftime('%-m月%-d日')}"
    try:
        events = list_events_for_date(now)
        head += "\n\n" + ("📅 今日行程\n" + _format_day_events(events) if events else "📅 今天沒有行程")
    except Exception as e:
        print(f"[DoubleA] 早安摘要：行事曆取得失敗 {e}")
        head += "\n\n" + auth_error_hint(e)
    w = _weather_line()
    if w:
        head += "\n\n" + w
    try:
        bdays = get_todays_birthdays(now.month, now.day)
        if bdays:
            lines = []
            for b in bdays:
                age = f"（滿 {now.year - b['year']} 歲）" if b.get("year") else ""
                lines.append(f"🎂 今天是【{b['name']}】的生日{age}！")
            head += "\n\n" + "\n".join(lines) + "\n🎉 祝生日快樂、平安喜樂！"
    except Exception as e:
        print(f"[DoubleA] 早安摘要：生日取得失敗 {e}")
    parts = [head]
    if _env_on("ENABLE_PROVERB_PUSH", "true"):
        try:
            zh_v, en_v = get_daily_verses(now)
            parts.append(f"🕊️ 今日箴言\n\n{zh_v}\n\n{en_v}\n\n（傳「箴言」看整章）")
        except Exception as e:
            print(f"[DoubleA] 早安摘要：箴言取得失敗 {e}")
    return parts


def morning_digest_job() -> None:
    """每日 07:00：早安摘要（一次推播，只算一次群組人數）。"""
    chat_id = _push_target()
    if not chat_id:
        print("[DoubleA] 早安摘要：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    try:
        _push_bundle(chat_id, build_morning_digest(now))
        print("[DoubleA] 早安摘要已發送")
    except Exception as e:
        print(f"[DoubleA] 早安摘要推播失敗：{e}")


def evening_digest_job() -> None:
    """（選用）每日 18:00：明天行程＋未完成待辦。需 ENABLE_EVENING_PUSH=true。"""
    chat_id = _push_target()
    if not chat_id or not _optional_push_allowed(chat_id):
        return
    now = datetime.now(TAIPEI_TZ)
    tomorrow = now + timedelta(days=1)
    msg = f"🌙 晚安！明天 {tomorrow.strftime('%-m月%-d日')}"
    try:
        events = list_events_for_date(tomorrow)
        msg += "\n\n" + ("📅 明天行程\n" + _format_day_events(events) if events else "📅 明天沒有行程")
    except Exception as e:
        msg += "\n\n" + auth_error_hint(e)
    try:
        tasks = get_pending_tasks()
        if tasks:
            msg += "\n\n📋 還沒完成的待辦\n" + "\n".join(f"{i}. {t['title']}" for i, t in enumerate(tasks, 1))
    except Exception as e:
        print(f"[DoubleA] 晚間摘要：待辦取得失敗 {e}")
    try:
        _push_bundle(chat_id, [msg])
        print("[DoubleA] 晚間摘要已發送")
    except Exception as e:
        print(f"[DoubleA] 晚間摘要推播失敗：{e}")


def reminder_check_job() -> None:
    """（選用）每 5 分鐘檢查到期的行程提醒。需 ENABLE_EVENT_REMINDER=true。"""
    now = datetime.now(TAIPEI_TZ)
    for r in get_due_reminders(now):
        rid = r.get("_doc_id", r.get("_id"))
        try:
            start_dt = datetime.fromisoformat(r["start"])
            if start_dt.tzinfo is None:
                start_dt = TAIPEI_TZ.localize(start_dt)
            if start_dt < now:  # 行程已開始（例如伺服器停機錯過），不補發
                mark_reminder_sent(rid)
                continue
            if not _optional_push_allowed(r["chat_id"]):
                mark_reminder_sent(rid)
                continue
            send_event_reminder(r["chat_id"], r["title"], r["start"])
            mark_reminder_sent(rid)
        except Exception as e:
            print(f"[DoubleA] 行程提醒發送失敗：{e}")


def morning_briefing_job() -> None:
    chat_id = load_chat_id()
    if not chat_id:
        print("[DoubleA] 早安排程：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    date_str = now.strftime("%-m月%-d日")
    try:
        events = list_events_for_date(now)
    except Exception as e:
        print(f"[DoubleA] 早安排程：行事曆取得失敗 {e}")
        return
    if not events:
        msg = f"早安！☀️ 今天是 {date_str}\n\n今天沒有行程，祝您有美好的一天！"
    else:
        lines = [f"早安！☀️ 今天是 {date_str}，以下是今日行程：\n"]
        for ev in events:
            time_part = f"{ev['start_str']} " if ev.get("start_str") else ""
            loc = ev.get("location", "")
            loc_part = f" 📍{loc}" if (loc and loc != "null") else ""
            lines.append(f"📅 {time_part}【{ev['title']}】{loc_part}")
        msg = "\n".join(lines)
    try:
        current = get_current_weather()
        forecasts = get_daily_forecast(1)
        if forecasts:
            fc = forecasts[0]
            pop_str = f"🌂 {fc['pop']}%" if fc["pop"] > 0 else "☀️ 不下雨"
            msg += (
                f"\n\n─────────────\n"
                f"🌦 今日天氣｜{current['emoji']} {current['description']}\n"
                f"🌡 現在 {current['temp']}°C，今日 {fc['temp_min']}～{fc['temp_max']}°C　{pop_str}"
            )
    except Exception as e:
        print(f"[DoubleA] 早安排程：天氣取得失敗 {e}")
    try:
        _push_line(chat_id, msg)
        print(f"[DoubleA] 早安通知已發送：{len(events)} 筆行程")
    except Exception as e:
        print(f"[DoubleA] 早安通知發送失敗：{e}")


def daily_reminder_job() -> None:
    chat_id = load_chat_id()
    if not chat_id:
        print("[DoubleA] 下午提醒排程：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    sections = []
    try:
        cal_events = list_events_for_date(now)
        remaining = [ev for ev in cal_events if ev.get("start_str", "") > "17:00"]
        if remaining:
            lines = ["📅 今晚行事曆\n"]
            for ev in remaining:
                loc = ev.get("location", "")
                loc_part = f" 📍{loc}" if (loc and loc != "null") else ""
                lines.append(f"・{ev['start_str']} 【{ev['title']}】{loc_part}")
            sections.append("\n".join(lines))
    except Exception as e:
        print(f"[DoubleA] 下午提醒排程：行事曆取得失敗 {e}")
    try:
        tasks = get_pending_tasks()
        if tasks:
            lines = ["📋 待辦提醒\n"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t['title']}")
            lines.append(f"\n🔗 {TASKS_URL}")
            sections.append("\n".join(lines))
    except Exception as e:
        print(f"[DoubleA] 下午提醒排程：待辦取得失敗 {e}")
    if not sections:
        print("[DoubleA] 下午提醒排程：無待辦也無晚間行程，略過")
        return
    msg = "🌆 下午好！來看看今天還有什麼：\n\n" + "\n\n".join(sections)
    try:
        _push_line(chat_id, msg)
        print(f"[DoubleA] 下午提醒已發送")
    except Exception as e:
        print(f"[DoubleA] 下午提醒發送失敗：{e}")


def _generate_share_link(ev: dict) -> str:
    start_dt = datetime.fromisoformat(ev["start"])
    end_dt = datetime.fromisoformat(ev["end"])
    if start_dt.tzinfo is None:
        start_dt = TAIPEI_TZ.localize(start_dt)
    if end_dt.tzinfo is None:
        end_dt = TAIPEI_TZ.localize(end_dt)
    start_utc = start_dt.astimezone(pytz.utc)
    end_utc = end_dt.astimezone(pytz.utc)
    dates = f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}/{end_utc.strftime('%Y%m%dT%H%M%SZ')}"
    params: dict = {"action": "TEMPLATE", "text": ev["title"], "dates": dates}
    if ev.get("location"):
        params["location"] = ev["location"]
    if ev.get("description"):
        # 只放前 60 字：整段原文 URL 編碼後每個中文字變 9 字元，會讓確認訊息超過 LINE 5000 字上限
        params["details"] = ev["description"][:60]
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_calendar_confirmation(event_data: dict, event_link: str) -> str:
    start_dt = datetime.fromisoformat(event_data["start"])
    date_str = start_dt.strftime("%-m月%-d日 %H:%M")
    loc = event_data.get("location")
    location_line = f"\n📍 {loc}" if (loc and loc != "null") else ""
    link_line = f"\n\n🔗 {event_link}" if event_link else ""
    share_link = _generate_share_link(event_data)
    return (
        f"📅 已加入行事曆！\n\n"
        f"【{event_data['title']}】\n"
        f"🗓 {date_str}{location_line}"
        f"{link_line}\n\n"
        f"✅ Ginny 已收到邀請\n"
        f"{'⏰ 將於開始前 2 小時提醒' if _env_on('ENABLE_EVENT_REMINDER') else '⏰ 當天 07:00 早安摘要會提醒'}\n\n"
        f"📤 分享給其他人（點擊即可加入行事曆）\n{share_link}"
    )


def _format_multi_calendar_confirmation(results: list[dict]) -> str:
    count = len(results)
    lines = [f"📅 已加入 {count} 個行事曆！\n"]
    for i, r in enumerate(results, 1):
        ev = r["event_data"]
        start_dt = datetime.fromisoformat(ev["start"])
        date_str = start_dt.strftime("%-m月%-d日 %H:%M")
        loc = ev.get("location")
        loc_part = f" 📍{loc}" if (loc and loc != "null") else ""
        lines.append(f"{i}.【{ev['title']}】{date_str}{loc_part}")
    lines.append("\n✅ Ginny 已收到邀請\n" + (
        "⏰ 將於各活動開始前 2 小時提醒" if _env_on("ENABLE_EVENT_REMINDER") else "⏰ 當天 07:00 早安摘要會提醒"))
    return "\n".join(lines)


def _format_todo_list(tasks: list) -> str:
    if not tasks:
        return "✅ 目前沒有待辦事項！"
    lines = ["📋 待辦清單\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['title']}")
    lines.append(f"\n🔗 {TASKS_URL}")
    return "\n".join(lines)


def _format_calendar_query_result(label: str, events: list[dict], is_range: bool = False) -> str:
    if not events:
        return f"📅 {label}沒有行程喔！"

    def _ev_line(ev: dict, prefix: str = "・") -> str:
        time_part = f"{ev['start_str']} " if ev.get("start_str") else ""
        loc = ev.get("location", "")
        loc_part = f"（{loc}）" if (loc and loc != "null") else ""
        return f"{prefix}{time_part}{ev['title']}{loc_part}"

    if is_range:
        from collections import OrderedDict
        by_date: dict = OrderedDict()
        for ev in events:
            key = ev.get("date_str", "")
            by_date.setdefault(key, []).append(ev)
        lines = [f"📅 {label}的行程：\n"]
        for date_str, day_events in by_date.items():
            lines.append(f"【{date_str}】")
            for ev in day_events:
                lines.append(_ev_line(ev))
        return "\n".join(lines)
    else:
        lines = [f"📅 {label}的行程：\n"]
        for ev in events:
            lines.append(_ev_line(ev))
        return "\n".join(lines)


def _format_weather_today(current: dict, forecast: dict) -> str:
    pop_str = f"🌂 降雨機率 {forecast['pop']}%" if forecast["pop"] > 0 else "☀️ 今天不太會下雨"
    return (
        f"🌦 台北今日天氣\n\n"
        f"{current['emoji']} {current['description']}\n"
        f"🌡 現在 {current['temp']}°C（體感 {current['feels_like']}°C）\n"
        f"📊 今日 {forecast['temp_min']}～{forecast['temp_max']}°C\n"
        f"{pop_str}\n"
        f"💧 濕度 {current['humidity']}%"
    )


def _format_weather_single(forecast: dict) -> str:
    pop_str = f"🌂 降雨機率 {forecast['pop']}%" if forecast["pop"] > 0 else "☀️ 不太會下雨"
    return (
        f"🌦 台北{forecast['label']}天氣\n\n"
        f"{forecast['emoji']} {forecast['description']}\n"
        f"🌡 {forecast['temp_min']}～{forecast['temp_max']}°C\n"
        f"{pop_str}"
    )


def _format_weather_week(forecasts: list[dict]) -> str:
    lines = ["🌦 台北近期天氣\n"]
    for f in forecasts:
        rain = f" ☔{f['pop']}%" if f["pop"] > 0 else ""
        lines.append(f"【{f['label']}】{f['emoji']} {f['description']} {f['temp_min']}～{f['temp_max']}°C{rain}")
    return "\n".join(lines)


# ── Reminder helpers ──────────────────────────────────────────────────────────

def schedule_event_reminder(chat_id: str, event_data: dict) -> None:
    if not _env_on("ENABLE_EVENT_REMINDER"):
        return  # 免費方案預設關閉（推播額度不夠），升級後設 ENABLE_EVENT_REMINDER=true
    start_dt = datetime.fromisoformat(event_data["start"])
    reminder_dt = start_dt - timedelta(minutes=REMINDER_MINUTES)
    now = datetime.now(TAIPEI_TZ)
    if reminder_dt <= now:
        print(f"[DoubleA] 活動太近，略過排程提醒")
        return
    add_reminder(chat_id, event_data, reminder_dt)
    print(f"[DoubleA] 提醒已排程：{reminder_dt.strftime('%-m月%-d日 %H:%M')}")


def send_event_reminder(chat_id: str, title: str, start_str: str) -> None:
    start_dt = datetime.fromisoformat(start_str)
    date_str = start_dt.strftime("%-m月%-d日 %H:%M")
    text = (
        f"⏰ 提醒！\n\n"
        f"【{title}】\n"
        f"🗓 {date_str} 即將開始\n"
        f"還有 2 小時！"
    )
    _push_line(chat_id, text)


# ── Commands ──────────────────────────────────────────────────────────────────

def _menu_text() -> str:
    return (
        "🙏 平安！我是培正家AI小幫手。\n"
        "以下功能隨時傳訊息給我就能用：\n\n"
        "📅 行事曆\n"
        "・「明天下午3點開會」→ 自動加入\n"
        "・「今天有什麼行程」「這週有什麼」→ 查詢\n"
        "・「刪除行程」「修改行程」→ 選單操作\n\n"
        "📋 待辦：「記得買菜」新增；「待辦」查看；「完成 買菜」或「del 1」完成\n"
        "🛒 購物：「+買 牛奶」；「購物清單」；「買到 1」\n"
        "📓 筆記：「+記 內容」；「筆記」；「刪筆記 1」\n"
        "🎂 生日：「+生日 媽媽 3/15」；「生日清單」；「刪生日 1」\n"
        "💰 記帳：「+專案 日本旅行」；「+花費 日本旅行 午餐 850」；「專案清單」\n"
        "🍽️ 餐廳：「附近餐廳」或「附近 火鍋」\n"
        "🌦️ 天氣：「今天天氣」「明天天氣」「這週天氣」\n"
        "📖 箴言：傳「箴言」看整章\n"
        "🙏 平安：今日行程＋待辦＋精選箴言\n\n"
        + _schedule_desc()
    )


def _schedule_desc() -> str:
    s = "⏰ 自動提醒：每天 07:00 早安摘要（今日行程＋天氣＋壽星＋精選箴言）"
    if _env_on("ENABLE_EVENING_PUSH"):
        s += "、18:00 明天行程＋待辦"
    if _env_on("ENABLE_EVENT_REMINDER"):
        s += "、行程開始前 2 小時提醒"
    return s + "。其他時間傳「平安」就能隨時查看。"


_FILLER_RE = re.compile(
    r"(傳送|請問|給我|幫我|麻煩|看一下|看看|查詢|有哪些|有什麼|一下|我的|顯示|列出|所有|目前|現在|還有|哪些|傳|請|看|查|的)"
)
_PUNCT_RE = re.compile(r"[\s「」『』\"'“”!！?？。,，、~～:：]")

# 純「查詢／動作」類指令的各種說法 → 標準指令
_EXACT_ALIASES: dict[str, tuple[str, ...]] = {
    "待辦": ("待辦", "待辦清單", "待辦事項", "待办", "代辦", "代辦清單", "代辦事項", "todo", "todolist", "任務", "任務清單"),
    "購物清單": ("購物清單", "購物", "購物單", "購物列表", "採買清單", "要買的"),
    "筆記": ("筆記", "筆記清單", "筆記列表", "記事本", "所有筆記"),
    "生日清單": ("生日清單", "生日", "生日表", "生日列表", "生日提醒"),
    "專案清單": ("專案清單", "專案", "專案列表", "記帳", "記帳清單", "花費清單"),
    "清除已買": ("清除已買", "清除買完", "清掉已買", "清空已買", "清除已買的"),
    "刪除行程": ("刪除行程", "刪行程", "取消行程", "移除行程"),
    "修改行程": ("修改行程", "改行程", "編輯行程", "更改行程"),
    "選單": ("選單", "功能", "功能表", "功能選單", "說明", "幫助", "help", "menu", "指令", "怎麼用", "?", "？"),
    "群組ID": ("群組id", "chatid", "群組編號", "聊天室id", "群組代號"),
    "重新授權": ("重新授權", "授權", "google授權", "重新登入", "reauth"),
    "自我檢查": ("自我檢查", "自檢", "健康檢查", "系統檢查", "檢查功能", "selftest"),
}
_ALIAS_LOOKUP = {a.lower(): canon for canon, alts in _EXACT_ALIASES.items() for a in alts}
# 別名本身含「查、看、的…」這類贅字時，比對前會被去掉，所以也登記去掉贅字後的版本
_ALIAS_LOOKUP.update({
    _FILLER_RE.sub("", a.lower()): canon
    for canon, alts in _EXACT_ALIASES.items() for a in alts
    if _FILLER_RE.sub("", a.lower()) and _FILLER_RE.sub("", a.lower()) not in _ALIAS_LOOKUP
})

# 「指令 + 內容」類的各種說法 → 標準格式（(pattern, replacement, 是否為寬鬆比對)）
_PREFIX_RULES: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"^(?:新增購物|加購物|加買|加入購物清單|購物)\s*[:：]?\s*(.+)$"), r"+買 \1", False),
    (re.compile(r"^(?:記筆記|新增筆記|加筆記|筆記)\s*[:：]?\s*(.+)$"), r"+記 \1", False),
    (re.compile(r"^(?:新增生日|加生日|記生日)\s*[:：]?\s*(.+)$"), r"+生日 \1", False),
    (re.compile(r"^(?:新增專案|建立專案|加專案|開專案)\s*[:：]?\s*(.+)$"), r"+專案 \1", False),
    (re.compile(r"^(?:新增花費|加花費|記花費|記一筆)\s*[:：]?\s*(.+)$"), r"+花費 \1", False),
    (re.compile(r"^(?:刪除筆記|刪筆記|刪掉筆記)\s*(\d+)$"), r"刪筆記 \1", False),
    (re.compile(r"^(?:刪除生日|刪生日|刪掉生日)\s*(\d+)$"), r"刪生日 \1", False),
    (re.compile(r"^(?:刪除專案|刪專案|刪掉專案)\s*(.+)$"), r"刪專案 \1", False),
    (re.compile(r"^(?:查專案|看專案|專案明細|專案內容)\s*(.+)$"), r"專案 \1", False),
    (re.compile(r"^(?i:done|del|完成了?|已完成|做完了?|搞定了?)\s*(\d+)$"), r"del \1", False),
    (re.compile(r"^(?i:done|完成了?|已完成|做完了?|搞定了?)\s*(.+)$"), r"完成 \1", True),
    (re.compile(r"^(?:買到了?|買好了?|已買|買齊了?)\s*(.+)$"), r"買到 \1", True),
]


def _canonicalize(text: str) -> tuple[str, bool]:
    """把各種說法轉成標準指令。回傳 (標準指令文字, 是否為『寬鬆比對』)。
    寬鬆比對（例如「完成買菜」）找不到對應項目時，會放行給 AI，避免誤攔一般聊天。
    不是指令就原樣回傳。"""
    t = text.strip().replace("＋", "+").replace("\u3000", " ")
    if not t:
        return text, False
    # 「+ 買 牛奶」→「+買 牛奶」
    t = re.sub(r"^\+\s*(買|記|生日|專案|花費)", r"+\1", t)
    if t.startswith("+"):
        return t, False

    clean = _FILLER_RE.sub("", _PUNCT_RE.sub("", t)).lower()
    if clean in _ALIAS_LOOKUP and len(t) <= 14:
        return _ALIAS_LOOKUP[clean], False

    if len(t) <= 60:
        for pat, repl, loose in _PREFIX_RULES:
            m = pat.match(t)
            if m:
                return pat.sub(repl, t), loose

    # 附近餐廳／附近火鍋／附近有什麼好吃的
    if t.startswith("附近") and len(t) <= 14:
        kw = re.sub(r"(有什麼|有沒有|好吃的|推薦|餐廳|的|店|找|查|附近|\s|[?？!！。])", "", t)
        return ("附近餐廳" if not kw else f"附近 {kw}"), False
    return text, False


def _loose_match(text: str, keyword: str, max_len: int = 8) -> bool:
    """寬鬆比對：短訊息（去掉空白與標點後）只要含關鍵字就算，
    例如「傳箴言」「箴言！」「傳「箴言」」「今日箴言」都會觸發；長訊息不會誤觸。"""
    t = re.sub(r"[\s「」『』\"'“”!！?？。,，、~～]", "", text)
    return keyword in t and len(t) <= max_len


def handle_command(text: str, chat_id: str, reply_token: str | None = None) -> bool:
    _used: list[bool] = [False]
    text, _loose = _canonicalize(text)  # 放寬文字解讀：各種說法先轉成標準指令

    def _respond(msg: str) -> None:
        _send_line_msg(chat_id, TextMessage(text=msg), reply_token, _used)

    # 「平安」= 今日行程 + 待辦 + 今日精選箴言（中英各 3 節）；完整功能選單改傳「選單」
    if _loose_match(text, "平安", 5) and "平安夜" not in text:
        _now = datetime.now(TAIPEI_TZ)
        head = f"🙏 平安！今天是 {_now.strftime('%-m月%-d日')}"
        try:
            evs = list_events_for_date(_now)
            head += "\n\n" + ("📅 今日行程\n" + _format_day_events(evs) if evs else "📅 今天沒有行程")
        except Exception as e:
            print(f"[DoubleA] 平安：行事曆取得失敗 {e}")
            head += "\n\n" + _gerr(e, "📅 行事曆暫時無法取得")
        try:
            tasks = get_pending_tasks()
            if tasks:
                head += "\n\n📋 待辦\n" + "\n".join(f"{i}. {t['title']}" for i, t in enumerate(tasks, 1))
            else:
                head += "\n\n📋 目前沒有待辦事項 ✅"
        except Exception as e:
            print(f"[DoubleA] 平安：待辦取得失敗 {e}")
            if not is_auth_expired_error(e):
                head += "\n\n📋 待辦暫時無法取得"
        parts = [head]
        try:
            zh_v, en_v = get_daily_verses(_now)
            parts.append(f"🕊️ 今日箴言\n\n{zh_v}\n\n{en_v}\n\n（傳「箴言」看整章、「選單」看全部功能）")
        except Exception as e:
            print(f"[DoubleA] 平安：箴言取得失敗 {e}")
            parts.append("📖 箴言暫時無法取得，可稍後傳「箴言」再試。")
        try:
            _send_texts(chat_id, parts, reply_token, _used)
        except Exception as e:
            print(f"[DoubleA] 平安：送出失敗 {e}")
        return True

    # 「選單」= 功能選單
    if text.strip() == "選單":
        _respond(_menu_text())
        return True

    # 「重新授權」= 產生 15 分鐘有效的 Google 重新授權連結（只有群組裡的家人拿得到）
    if text.strip() == "重新授權":
        import secrets
        nonce = secrets.token_urlsafe(18)
        save_state({"reauth_nonce": nonce, "reauth_exp": time.time() + 900})
        _respond(
            "🔑 Google 重新授權連結（15 分鐘內有效，請勿轉傳）：\n"
            f"{_public_base_url()}/reauth?token={nonce}\n\n"
            "用電腦或手機瀏覽器打開，照頁面上 ①～④ 步驟完成即可。"
        )
        return True

    # 「自我檢查」= 逐項檢查 Google、LINE 額度、天氣、餐廳、資料保存、排程
    if text.strip() == "自我檢查":
        try:
            from selftest import run_selftest
            rs = run_selftest(scheduler)
            ok = all(r["ok"] for r in rs)
            lines = [f"🩺 自我檢查：{'全部正常 ✅' if ok else '有項目異常 ❌'}\n"]
            lines += [f"{'✅' if r['ok'] else '❌'} {r['item']}：{r['detail'][:80]}" for r in rs]
            _respond("\n".join(lines))
        except Exception as e:
            _respond(f"⚠️ 自我檢查執行失敗：{type(e).__name__}")
        return True

    # 「群組ID」= 回覆目前聊天室的 ID，用來設定 Railway 的 PUSH_CHAT_ID
    if text.strip() == "群組ID":
        _respond(f"這個聊天室的 ID：\n{chat_id}\n\n把它填到 Railway 環境變數 PUSH_CHAT_ID，排程推播就會固定送到這裡。")
        return True

    # 「箴言」= 即時回覆今日箴言（中、英分開送，避免超過 LINE 5000 字上限）
    if _loose_match(text, "箴言"):
        try:
            now = datetime.now(TAIPEI_TZ)
            zh_text, en_text = get_todays_proverbs(now)
            _send_texts(chat_id, [f"{get_proverbs_header(now)}\n\n{zh_text}", en_text], reply_token, _used)
        except Exception as e:
            print(f"[DoubleA] 箴言指令失敗：{e}")
            try:
                _respond("⚠️ 箴言暫時無法取得，請稍後再試。")
            except Exception:
                pass
        return True

    if text.strip() == "待辦":
        try:
            tasks = get_pending_tasks()
            _respond(_format_todo_list(tasks))
        except Exception as e:
            _respond(_gerr(e, "⚠️ 無法取得待辦清單，請稍後再試。"))
        return True

    if text.startswith("完成 ") or text.startswith("done "):
        keyword = text.split(" ", 1)[1].strip()
        try:
            title = complete_task_by_keyword(keyword)
            if not title and _loose:
                return False  # 寬鬆說法（如「完成買菜」）找不到待辦 → 交給 AI，不攔一般聊天
            if title:
                msg = f"✅ 已完成：【{title}】\n\n{_cheer_complete()}"
            else:
                msg = f"❓ 找不到包含「{keyword}」的待辦事項"
            _respond(msg)
        except Exception as e:
            _respond(f"⚠️ 標記失敗：{e}")
        return True

    if text.lower().startswith("del "):
        try:
            n = int(text.split(" ", 1)[1].strip())
            title = complete_task_by_index(n)
            if title:
                msg = f"✅ 已完成：【{title}】\n\n{_cheer_complete()}"
            else:
                msg = f"❓ 找不到第 {n} 項待辦事項"
            _respond(msg)
        except ValueError:
            _respond("❓ 格式錯誤，請輸入「del 1」")
        except Exception as e:
            _respond(f"⚠️ 標記失敗：{e}")
        return True

    if text.strip() == "刪除行程":
        try:
            events = list_upcoming_events(days=7)
            if not events:
                _respond("📅 未來 7 天沒有行程可以刪除。")
            else:
                _push_delete_picker(chat_id, events, reply_token, _used)
        except Exception as e:
            _respond(_gerr(e, "⚠️ 無法取得行程，請稍後再試。"))
        return True

    if text.startswith("確認刪除 "):
        event_id = text.split("確認刪除 ", 1)[1].strip()
        try:
            title = delete_event_by_id(event_id)
            _respond(f"✅ 已刪除：【{title}】")
        except Exception as e:
            _respond(f"⚠️ 刪除失敗：{e}")
        return True

    if text.strip() == "修改行程":
        try:
            events = list_upcoming_events(days=7)
            if not events:
                _respond("📅 未來 7 天沒有行程可以修改。")
            else:
                _push_edit_picker(chat_id, events, reply_token, _used)
        except Exception as e:
            _respond(_gerr(e, "⚠️ 無法取得行程，請稍後再試。"))
        return True

    if text.startswith("選擇修改 "):
        rest = text[len("選擇修改 "):]
        parts = rest.split(" ", 1)
        event_id = parts[0]
        event_title = parts[1] if len(parts) > 1 else "（無標題）"
        try:
            _push_field_picker(chat_id, event_id, event_title, reply_token, _used)
        except Exception as e:
            _respond(f"⚠️ 錯誤：{e}")
        return True

    if text.startswith("修改欄位 "):
        rest = text[len("修改欄位 "):]
        parts = rest.split(" ", 2)
        if len(parts) >= 2:
            field = parts[0]
            event_id = parts[1]
            event_title = parts[2] if len(parts) > 2 else "（無標題）"
            field_name = {"title": "標題", "start": "時間", "location": "地點"}.get(field, field)
            hint = {"title": "（例如：家庭聚會）", "start": "（例如：明天下午3點）", "location": "（例如：教會）"}.get(field, "")
            save_pending_edit(chat_id, {"event_id": event_id, "field": field, "title": event_title})
            _respond(f"請輸入【{event_title}】的新{field_name}：\n{hint}\n\n輸入「取消」可放棄修改")
        return True

    # ── 購物清單 ──────────────────────────────────────────────────────────────

    if text.startswith("+買"):
        raw = text[len("+買"):].strip()
        names = [n.strip() for n in re.split(r"[,、\n\s]+", raw) if n.strip()]
        try:
            added = add_items(names)
            if added:
                _respond("🛒 已加入購物清單：\n" + "\n".join(f"• {n}" for n in added))
        except Exception as e:
            _respond(f"⚠️ 新增失敗：{e}")
        return True

    if text.strip() == "購物清單":
        try:
            _push_shopping_list(chat_id, get_items(), reply_token, _used)
        except Exception as e:
            _respond(f"⚠️ 無法取得購物清單：{e}")
        return True

    if text.strip() == "清除已買":
        try:
            count = clear_done()
            if count:
                _respond(f"✅ 已清除 {count} 個買完的品項")
            else:
                _respond("❓ 沒有已買完的品項可清除")
        except Exception as e:
            _respond(f"⚠️ 清除失敗：{e}")
        return True

    if text.startswith("買到 "):
        rest = text[len("買到 "):].strip()
        try:
            if rest.isdigit():
                name = mark_done_by_index(int(rest))
                not_found_msg = f"❓ 找不到第 {rest} 項"
            else:
                name = mark_done_by_keyword(rest)
                not_found_msg = f"❓ 找不到包含「{rest}」的品項"
            if not name and _loose:
                return False  # 寬鬆說法（如「買了新車」）找不到品項 → 交給 AI
            if not name:
                _respond(not_found_msg)
            else:
                items = get_items()
                pending = [it for it in items if not it["done"]]
                if not pending:
                    _respond(f"✅ 買到了：{name}\n\n🎉 全部買完了！輸入「清除已買」可清空清單。")
                else:
                    _push_shopping_list(chat_id, items, reply_token, _used)
        except Exception as e:
            _respond(f"⚠️ 標記失敗：{e}")
        return True

    # ── 記事本 ────────────────────────────────────────────────────────────────

    if text.startswith("+記"):
        content = text[len("+記"):].strip()
        if not content:
            _respond("❓ 請輸入筆記內容，例如：+記 重要事項")
            return True
        try:
            note = add_note(content)
            _respond(f"📓 已記下：\n\n{note['content']}")
        except Exception as e:
            _respond(f"⚠️ 新增失敗：{e}")
        return True

    if text.strip() == "筆記":
        try:
            _push_notes(chat_id, get_notes(), reply_token, _used)
        except Exception as e:
            _respond(f"⚠️ 無法取得筆記：{e}")
        return True

    if text.startswith("刪筆記 "):
        rest = text[len("刪筆記 "):].strip()
        if rest.isdigit():
            try:
                removed = delete_note_by_index(int(rest))
                if removed:
                    _respond(f"🗑 已刪除筆記：\n\n{removed}")
                else:
                    _respond(f"❓ 找不到第 {rest} 筆筆記")
            except Exception as e:
                _respond(f"⚠️ 刪除失敗：{e}")
        else:
            _respond("❓ 請輸入筆記編號，例如：刪筆記 1")
        return True

    # ── 生日提醒 ──────────────────────────────────────────────────────────────

    if text.startswith("+生日"):
        rest = text[len("+生日"):].strip()
        parts = rest.split(None, 1)
        if len(parts) < 2:
            _respond("格式：+生日 名字 日期\n範例：+生日 媽媽 3/15\n　　　+生日 Ginny 1990/6/10")
            return True
        name, date_str = parts[0], parts[1]
        month, day, year = parse_birthday_date(date_str)
        if not month:
            _respond(f"⚠️ 無法解析日期「{date_str}」\n格式範例：3/15 或 1990/3/15")
            return True
        try:
            entry = add_birthday(name, month, day, year)
            year_part = f"（{year} 年）" if year else ""
            _respond(f"🎂 已記錄：{entry['name']}｜{month}月{day}日{year_part}")
        except Exception as e:
            _respond(f"⚠️ 新增失敗：{e}")
        return True

    if text.strip() == "生日清單":
        try:
            _push_birthday_list(chat_id, get_birthdays(), reply_token, _used)
        except Exception as e:
            _respond(f"⚠️ 無法取得生日清單：{e}")
        return True

    if text.startswith("刪生日 "):
        rest = text[len("刪生日 "):].strip()
        if rest.isdigit():
            try:
                name = delete_birthday_by_index(int(rest))
                if name:
                    _respond(f"✅ 已刪除：{name} 的生日記錄")
                else:
                    _respond(f"❓ 找不到第 {rest} 筆")
            except Exception as e:
                _respond(f"⚠️ 刪除失敗：{e}")
        else:
            _respond("❓ 請輸入編號，例如：刪生日 1")
        return True

    # ── 記帳 ──────────────────────────────────────────────────────────────────

    if text.startswith("+專案"):
        rest = text[len("+專案"):].strip()
        if not rest:
            _respond("格式：+專案 名稱 說明\n例：+專案 日本旅行 6/30-7/4")
            return True
        parts = rest.split(None, 1)
        name = parts[0]
        description = parts[1] if len(parts) > 1 else ""
        try:
            existing = get_project(name)
            if existing:
                _respond(f"⚠️ 專案「{name}」已存在！\n用「專案 {name}」查看明細")
            else:
                create_project(name, description)
                desc_str = f"\n📝 {description}" if description else ""
                _respond(f"📂 已建立專案：【{name}】{desc_str}\n\n用「+花費 {name} 類別 人名:品項:金額」開始記帳！")
        except Exception as e:
            _respond(f"⚠️ 建立失敗：{e}")
        return True

    if text.startswith("+花費"):
        rest = text[len("+花費"):].strip()
        if not rest:
            _respond("格式：+花費 專案 類別 地點 人名:品項:金額\n例：+花費 日本旅行 午餐 大阪餐廳 培正:拉麵:850 Ginny:壽司:1200")
            return True
        try:
            parsed = parse_expense_input(rest)
            if not parsed:
                _respond("⚠️ 格式錯誤\n例：+花費 日本旅行 午餐 大阪餐廳 培正:拉麵:850 Ginny:壽司:1200")
                return True
            expense, doc_id = add_expense(parsed)
            _respond(format_expense_confirmation(expense, parsed["project"]))
        except ValueError as e:
            _respond(f"⚠️ {e}")
        except Exception as e:
            _respond(f"⚠️ 記帳失敗：{e}")
        return True

    if text.strip() == "專案清單":
        try:
            projects = list_projects()
            _respond(format_project_list(projects))
        except Exception as e:
            _respond(f"⚠️ 無法取得專案清單：{e}")
        return True

    if text.startswith("專案 "):
        name = text[len("專案 "):].strip()
        try:
            project = get_project(name)
            if not project:
                _respond(f"❓ 找不到專案「{name}」\n用「專案清單」查看所有專案")
            else:
                expenses = get_expenses(name)
                _respond(format_project_detail(project, expenses))
        except Exception as e:
            _respond(f"⚠️ 查詢失敗：{e}")
        return True

    if text.startswith("刪專案 "):
        name = text[len("刪專案 "):].strip()
        try:
            project = get_project(name)
            if not project:
                _respond(f"❓ 找不到專案「{name}」")
            else:
                delete_project(name)
                _respond(f"✅ 已刪除專案：【{name}】及所有花費記錄")
        except Exception as e:
            _respond(f"⚠️ 刪除失敗：{e}")
        return True

    # ── 餐廳推薦 ──────────────────────────────────────────────────────────────

    if text.strip() == "附近餐廳" or text.startswith("附近 "):
        allowed, rate_msg = check_rate_limit(chat_id, "restaurant")
        if not allowed:
            _respond(rate_msg)
            return True
        keyword = "" if text.strip() == "附近餐廳" else text[3:].strip()
        try:
            results = search_nearby_restaurants(keyword=keyword)
            _respond(format_restaurant_results(results, keyword))
        except Exception as e:
            _respond(f"⚠️ 餐廳查詢失敗：{e}")
        return True

    # 天氣查詢：純關鍵字判斷，不用 AI 分類。
    # 「天氣」是完全規則型的意圖，讓 AI 猜只會多一次呼叫、多一個分類失誤點。
    if "天氣" in text or text.strip().lower() == "weather":
        try:
            if any(k in text for k in ("這週", "本週", "一週", "未來", "這禮拜", "本禮拜")):
                forecasts = get_daily_forecast(5)
                reply = _format_weather_week(forecasts)
            elif any(k in text for k in ("明天", "後天")):
                forecasts = get_daily_forecast(3)
                target = next((f for f in forecasts if f["label"] in ("明天", "後天")), None)
                reply = _format_weather_single(target) if target else "⚠️ 無法取得近日天氣資料"
            else:
                current = get_current_weather()
                forecasts = get_daily_forecast(1)
                reply = _format_weather_today(current, forecasts[0]) if forecasts else (
                    f"{current['emoji']} 台北現在 {current['temp']}°C，{current['description']}"
                )
        except Exception as e:
            reply = "⚠️ 天氣查詢失敗，請稍後再試。"
        _respond(reply)
        return True

    # 行事曆查詢（今天/明天/這週…行程）：常見詞先用程式算日期範圍，不用 AI。
    # 只在確定是「查詢」而不是「新增／修改／刪除」時才接手，其餘交給 AI 判斷。
    _CALENDAR_CREATE_VERBS = ("加到", "加入", "新增", "增加", "建立", "排入", "排進", "排到", "記到", "記入")
    _CALENDAR_EDIT_VERBS = (
        "刪除", "刪掉", "取消", "修改", "更正", "更改", "改到", "改成", "改為", "改期",
        "調整", "延到", "延後", "延期", "提前", "移到", "換到", "挪到",
    )
    _CALENDAR_QUERY_HINTS = ("有什麼", "有哪些", "有沒有", "查", "看", "?", "？", "嗎", "幾點", "列出", "給我")
    _plain = re.sub(r"[\s「」!！。,，]", "", text)
    if (
        ("行程" in text or "行事曆" in text)
        and not any(v in text for v in _CALENDAR_CREATE_VERBS)
        and not any(v in text for v in _CALENDAR_EDIT_VERBS)
        # 只接手「像查詢」的句子：很短（例如「明天行程」）或有查詢語氣；長句交給 AI
        and (len(_plain) <= 8 or any(h in text for h in _CALENDAR_QUERY_HINTS))
    ):
        now = datetime.now(TAIPEI_TZ)
        today = now.date()
        start_d = end_d = label = None
        if "今天" in text:
            start_d = end_d = today
            label = "今天"
        elif "明天" in text:
            start_d = end_d = today + timedelta(days=1)
            label = "明天"
        elif "後天" in text:
            start_d = end_d = today + timedelta(days=2)
            label = "後天"
        elif any(k in text for k in ("這週", "本週", "這禮拜", "本禮拜")):
            start_d = today
            end_d = today + timedelta(days=(7 - today.isoweekday()) % 7)
            label = "這週"
        elif any(k in text for k in ("下週", "下星期", "下禮拜")):
            start_d = today + timedelta(days=(7 - today.isoweekday()) % 7 + 1)
            end_d = start_d + timedelta(days=6)
            label = "下週"
        elif any(k in text for k in ("本月", "這個月")):
            start_d = today
            if today.month == 12:
                end_d = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                end_d = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
            label = "本月"

        if label:
            try:
                start_dt = datetime(start_d.year, start_d.month, start_d.day)
                end_dt = datetime(end_d.year, end_d.month, end_d.day)
                is_range = start_d != end_d
                events = list_events_for_range(start_dt, end_dt) if is_range else list_events_for_date(start_dt)
                reply = _format_calendar_query_result(label, events, is_range)
            except Exception as e:
                reply = _gerr(e, "⚠️ 查詢行事曆失敗，請稍後再試。")
            _respond(reply)
            return True

    return False


# ── Emotional value ───────────────────────────────────────────────────────────

_COMPLETE_CHEERS = [
    "🌟 今天又完成一件事了，超棒的！",
    "💪 一件一件解決，你們真的很厲害！",
    "✨ 搞定！腦袋可以去做更重要的事了。",
    "🎉 完成！每一個小進步都值得慶祝。",
    "👏 做到了！今天又往前進了一步。",
    "🙌 太好了，又少一件煩惱！",
    "⚡ 效率一流，這件事正式關閉！",
]

_CALENDAR_CHEERS = [
    "🧠 記下來了！腦袋的空間留給更重要的事。",
    "📆 安排好了，就不用一直惦記著這件事了。",
    "👍 掌握住了，時間到我來提醒你們。",
    "✅ 好，這件事交給行事曆管，放心吧！",
]

_TODO_CHEERS = [
    "📝 記下來了！不用怕忘記了。",
    "👌 收到，這件事不會漏掉的。",
    "🗂 放進清單了，想到的時候可以來查。",
    "💡 好，記著了！完成後發「del N」或「完成 關鍵字」標記。",
]


def _cheer_complete() -> str:
    return random.choice(_COMPLETE_CHEERS)


def _cheer_calendar() -> str:
    return random.choice(_CALENDAR_CHEERS)


def _cheer_todo() -> str:
    return random.choice(_TODO_CHEERS)


# ── Event time fixer ──────────────────────────────────────────────────────────

def _fix_event_times(ev: dict) -> None:
    try:
        start_dt = datetime.fromisoformat(ev["start"])
        end_dt = datetime.fromisoformat(ev["end"])
        if end_dt <= start_dt:
            ev["end"] = (start_dt + timedelta(hours=1)).isoformat()
    except (KeyError, ValueError):
        pass


# ── Quick pre-filter ──────────────────────────────────────────────────────────

_TIME_KEYWORDS = [
    "今天", "明天", "後天", "大後天",
    "下週", "下星期", "這週", "這星期", "本週",
    "週一", "週二", "週三", "週四", "週五", "週六", "週日",
    "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日",
    "早上", "上午", "中午", "下午", "晚上", "凌晨",
    "點鐘", "點半", "幾點", "時候", "月", "號",
]
_TASK_KEYWORDS = [
    "記得", "要去", "要買", "要訂", "幫我", "幫你", "幫忙",
    "買", "訂", "查", "預約", "安排", "提醒",
    "預定", "預訂", "帶去", "帶來", "拿去", "拿來", "送去", "送來",
    "寄", "付", "繳", "聯絡", "通知", "確認", "回覆", "回電",
    "領", "取", "辦", "處理", "申請",
]
_MODIFY_KEYWORDS = ["修正", "更改", "改一下", "調整", "修改", "改成", "改到"]
_QUERY_KEYWORDS = ["有什麼事", "有什麼行程", "有什麼活動", "行程", "有沒有事", "有沒有行程", "查一下行程"]
_DELETE_KEYWORDS = ["刪除", "刪掉", "取消", "移除"]
_WEATHER_KEYWORDS = ["天氣", "下雨", "氣溫", "溫度", "颱風", "會不會雨"]


def _should_notify(text: str) -> bool:
    for kw in _MODIFY_KEYWORDS + _DELETE_KEYWORDS:
        if kw in text:
            return True
    time_hit = any(kw in text for kw in _TIME_KEYWORDS)
    task_hit = any(kw in text for kw in _TASK_KEYWORDS)
    query_hit = any(kw in text for kw in _QUERY_KEYWORDS)
    weather_hit = any(kw in text for kw in _WEATHER_KEYWORDS)
    return time_hit or task_hit or query_hit or weather_hit


# ── Message processing ────────────────────────────────────────────────────────

def process_message(text: str, chat_id: str, reply_token: str | None = None) -> None:
    print(f"[DoubleA] 收到訊息：{text}")
    # 只記住群組/聊天室（C、R 開頭）；私訊（U 開頭）不覆蓋，避免排程推播被送到私訊
    if chat_id[:1] in ('C', 'R') or not load_chat_id():
        save_chat_id(chat_id)
    now = datetime.now(TAIPEI_TZ)

    _used_reply: list[bool] = [False]

    def _respond(msg: str) -> None:
        if reply_token and not _used_reply[0]:
            try:
                _reply_line(reply_token, msg)
                _used_reply[0] = True
                return
            except Exception as _e:
                print(f"[DoubleA] reply 失敗，改用 push：{_e}")
        try:
            _push_line(chat_id, msg)
        except Exception as _e:
            print(f"[DoubleA] _respond push 最終失敗：{_e}")

    # 圖片＋文字說明才觸發行事曆：
    # 剛剛有傳照片、且這則文字包含觸發語 → 用那張照片建立行程，其餘文字判斷都跳過。
    if _is_calendar_trigger(text) and load_pending_image(chat_id):
        process_image_as_calendar(chat_id, reply_token, text)
        return

    if handle_command(text, chat_id, reply_token):
        return

    pending = load_pending_edit(chat_id)
    if pending:
        clear_pending_edit(chat_id)
        if text.strip() in ("取消", "取消修改", "cancel"):
            _respond("已取消修改。")
            return
        field = pending["field"]
        event_id = pending["event_id"]
        event_title = pending["title"]
        try:
            if field == "start":
                new_dt_str = parse_new_datetime(text, now)
                if not new_dt_str:
                    _respond("⚠️ 無法解析時間，請重新輸入（例如：明天下午3點）")
                    return
                update_event_field_by_id(event_id, "start", new_dt_str)
                new_dt = datetime.fromisoformat(new_dt_str)
                if new_dt.tzinfo is None:
                    new_dt = TAIPEI_TZ.localize(new_dt)
                else:
                    new_dt = new_dt.astimezone(TAIPEI_TZ)
                _respond(f"✅ 已修改：【{event_title}】\n🗓 新時間：{new_dt.strftime('%-m月%-d日 %H:%M')}")
            elif field == "title":
                update_event_field_by_id(event_id, "title", text.strip())
                _respond(f"✅ 已修改標題：\n【{text.strip()}】")
            elif field == "location":
                update_event_field_by_id(event_id, "location", text.strip())
                _respond(f"✅ 已修改地點：【{event_title}】\n📍 {text.strip()}")
        except Exception as e:
            print(f"[DoubleA] 行程欄位更新失敗：{e}")
            _respond("⚠️ 修改失敗，請稍後再試。")
        return

    # AI 速率限制檢查
    allowed, rate_msg = check_rate_limit(chat_id, "ai")
    if not allowed:
        _respond(rate_msg)
        return

    # 已移除「⏳ 收到！處理中…」推播：
    #  1) 它用 push（推播）發送，會消耗 LINE 每月 push 額度；
    #  2) push 額度用盡時會回傳 429，且原本這行沒有 try/except 保護，
    #     會讓整個 process_message 崩潰，導致行事曆/待辦/天氣完全沒反應。
    # 最終回覆本來就會用免費的 reply_token 送出，因此這則「處理中」通知可省略。

    result = parse_message(text, now)
    # 防呆：AI 有時（尤其多行訊息）會回傳 list 而非 dict，
    # 若不處理，下一行的 result.get() 會 AttributeError 讓整個機器人沒反應。
    if isinstance(result, list):
        _events = [r for r in result if isinstance(r, dict) and r.get("start")]
        if _events:
            result = {"type": "calendar", "events": _events}
        else:
            result = next((r for r in result if isinstance(r, dict)), {"type": "ignore"})
    if not isinstance(result, dict):
        result = {"type": "ignore"}
    msg_type = result.get("type", "ignore")
    print(f"[DoubleA] 分類：{msg_type}　{result}")

    if msg_type == "weather":
        period = result.get("period", "today")
        try:
            if period == "today":
                current = get_current_weather()
                forecasts = get_daily_forecast(1)
                reply = _format_weather_today(current, forecasts[0]) if forecasts else (
                    f"{current['emoji']} 台北現在 {current['temp']}°C，{current['description']}"
                )
            elif period == "tomorrow":
                forecasts = get_daily_forecast(3)
                target = next((f for f in forecasts if f["label"] in ("明天", "後天")), None)
                reply = _format_weather_single(target) if target else "⚠️ 無法取得近日天氣資料"
            else:
                forecasts = get_daily_forecast(5)
                reply = _format_weather_week(forecasts)
        except Exception as e:
            reply = "⚠️ 天氣查詢失敗，請稍後再試。"
        _respond(reply)

    elif msg_type == "edit":
        target_dt_str = result.get("target_datetime", "")
        has_time = result.get("has_time", True)
        title_hint = result.get("title_hint") or None
        updates: dict = {}
        if result.get("new_start"):
            updates["new_start"] = result["new_start"]
        loc = result.get("new_location")
        if loc and loc != "null":
            updates["new_location"] = loc
        if not updates:
            _respond("⚠️ 無法解析要修改的內容，請重新描述")
        else:
            try:
                target_dt = datetime.fromisoformat(target_dt_str)
                updated = find_and_update_event(target_dt, has_time, title_hint, updates)
                if updated:
                    lines = [f"✅ 已修改：【{updated['title']}】"]
                    if updated.get("new_start"):
                        new_dt = datetime.fromisoformat(updated["new_start"])
                        if new_dt.tzinfo is None:
                            new_dt = TAIPEI_TZ.localize(new_dt)
                        else:
                            new_dt = new_dt.astimezone(TAIPEI_TZ)
                        lines.append(f"🗓 新時間：{new_dt.strftime('%-m月%-d日 %H:%M')}")
                    if updated.get("new_location"):
                        lines.append(f"📍 新地點：{updated['new_location']}")
                    lines.append(f"🔗 {updated['link']}")
                    reply = "\n".join(lines)
                else:
                    reply = "❓ 找不到這個活動，請確認時間是否正確"
            except Exception as e:
                reply = "⚠️ 行事曆修改失敗，請稍後再試。"
            _respond(reply)

    elif msg_type == "delete":
        target_dt_str = result.get("target_datetime", "")
        has_time = result.get("has_time", True)
        title_hint = result.get("title_hint") or None
        try:
            target_dt = datetime.fromisoformat(target_dt_str)
            deleted = find_and_delete_event(target_dt, has_time, title_hint)
            reply = f"✅ 已刪除：【{deleted}】" if deleted else "❓ 找不到這個活動，請確認時間是否正確"
        except Exception as e:
            reply = "⚠️ 刪除行事曆失敗，請稍後再試。"
        _respond(reply)

    elif msg_type == "modify":
        last = load_last_event()
        if not last:
            _respond("❓ 找不到最近的行事曆事件，無法修改")
            return
        updates = parse_modification(text, last, now)
        if not updates:
            _respond("⚠️ 無法解析修改內容，請重新描述")
            return
        try:
            link = update_calendar_event(last["id"], updates)
            save_last_event(last["id"], {**last, **updates})
            start_dt = datetime.fromisoformat(updates["start"])
            date_str = start_dt.strftime("%-m月%-d日 %H:%M")
            reply = f"✅ 行事曆已更新！\n\n【{last['title']}】\n🗓 {date_str}\n\n🔗 {link}"
        except Exception as e:
            reply = "⚠️ 行事曆修改失敗，請稍後再試。"
        _respond(reply)

    elif msg_type == "calendar":
        events = result.get("events", [])
        if not events and result.get("title"):
            events = [result]
        if not events:
            _respond("⚠️ 無法解析行事曆事件，請重新描述。")
        elif len(events) == 1:
            ev = events[0]
            ev["description"] = text
            _fix_event_times(ev)
            try:
                created = create_calendar_event(ev)
                save_last_event(created["id"], ev)
                schedule_event_reminder(chat_id, ev)
                reply = _format_calendar_confirmation(ev, created["link"]) + f"\n\n{_cheer_calendar()}"
            except Exception as e:
                reply = _gerr(e, "⚠️ 行事曆寫入失敗，請稍後再試。")
            _respond(reply)
        else:
            succeeded, failed = [], []
            _last_err = None
            for ev in events:
                ev["description"] = text
                _fix_event_times(ev)
                try:
                    created = create_calendar_event(ev)
                    save_last_event(created["id"], ev)
                    schedule_event_reminder(chat_id, ev)
                    succeeded.append({"event_data": ev, "link": created["link"]})
                except Exception as e:
                    failed.append(ev.get("title", "未知事件"))
                    _last_err = e
            if succeeded:
                reply = _format_multi_calendar_confirmation(succeeded) + f"\n\n{_cheer_calendar()}"
                if failed:
                    reply += f"\n\n⚠️ 以下事件建立失敗：{'、'.join(failed)}"
            else:
                reply = _gerr(_last_err, "⚠️ 行事曆寫入失敗，請稍後再試。") if _last_err else "⚠️ 行事曆寫入失敗，請稍後再試。"
            try:
                _send_texts(chat_id, [reply], reply_token, _used_reply)
            except Exception as _e:
                print(f"[DoubleA] 多筆行程確認訊息送出失敗：{_e}")

    elif msg_type == "todo":
        try:
            task = add_task(result["title"], result.get("description"))
            reply = (
                f"📌 已記錄到 Google Tasks！\n\n"
                f"【{task['title']}】\n\n"
                f"🔗 {TASKS_URL}\n\n"
                f"完成後發「del N」或「完成 {task['title']}」即可標記\n\n"
                f"{_cheer_todo()}"
            )
        except Exception as e:
            reply = _gerr(e, "⚠️ 待辦事項記錄失敗，請稍後再試。")
        _respond(reply)

    elif msg_type == "query":
        start_date_str = result.get("start_date", "")
        end_date_str = result.get("end_date", "") or start_date_str
        label = result.get("label", "查詢日期")
        try:
            start_dt = datetime.fromisoformat(start_date_str)
            end_dt = datetime.fromisoformat(end_date_str)
            is_range = start_date_str != end_date_str
            events = list_events_for_range(start_dt, end_dt) if is_range else list_events_for_date(start_dt)
            reply = _format_calendar_query_result(label, events, is_range)
        except Exception as e:
            reply = _gerr(e, "⚠️ 查詢行事曆失敗，請稍後再試。")
        _respond(reply)

    else:
        print(f"[DoubleA] 略過（ignore）")


# ── Image OCR ────────────────────────────────────────────────────────────────

def _download_line_content(message_id: str) -> tuple[bytes, str]:
    resp = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    mime_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return resp.content, mime_type


def process_image(message_id: str, chat_id: str, reply_token: str | None = None) -> None:
    """背景任務：收到圖片。

    預設「不會」自動判斷成行事曆事件 —— 只做收據辨識／記帳照片提醒。
    要用這張照片建立行程，必須在 PENDING_IMAGE_TTL_SECONDS 秒內
    接著輸入觸發語（例如「加到行事曆」），由 process_image_as_calendar 處理。
    這樣可避免每次上傳吃飯照片都被誤判自動建立行程。
    """
    print(f"[DoubleA] process_image 開始：message_id={message_id} chat_id={chat_id}")
    if chat_id[:1] in ('C', 'R') or not load_chat_id():
        save_chat_id(chat_id)
    save_pending_image(chat_id, message_id)

    _used_reply: list[bool] = [False]

    def _respond(msg: str) -> None:
        if reply_token and not _used_reply[0]:
            try:
                _reply_line(reply_token, msg)
                _used_reply[0] = True
                return
            except Exception as _e:
                print(f"[DoubleA] image reply 失敗，改用 push：{_e}")
        try:
            _push_line(chat_id, msg)
        except Exception as _e:
            print(f"[DoubleA] image push 最終失敗：{_e}")

    try:
        image_bytes, mime_type = _download_line_content(message_id)
    except Exception as e:
        _respond("⚠️ 圖片下載失敗，請稍後再試。")
        return

    # 只嘗試辨識收據／記帳提醒，不主動判斷行事曆
    try:
        receipt = parse_receipt_image(image_bytes, mime_type)
        if "error" not in receipt and receipt.get("total", 0) > 0:
            # 成功辨識收據 → 找最近的專案記入
            last = get_last_expense_any(chat_id)
            if last:
                expense, doc_id = last
                project_name = expense.get("project", "")
                add_receipt_expense(receipt, project_name)
                _respond(format_receipt_result(receipt, project_name))
            else:
                store = receipt.get('store', '不明')
                total = receipt.get('total', 0)
                _respond(
                    f"🧾 偵測到收據！\n\n"
                    f"🏪 {store}\n"
                    f"💰 合計：${total:,.0f}\n\n"
                    "⚠️ 請先建立專案再傳收據\n"
                    "例：+專案 日本旅行 6/30-7/4"
                )
        else:
            # 不是收據 → 提醒存相簿（若有進行中的記帳專案）
            last = get_last_expense_any(chat_id)
            if last:
                expense, doc_id = last
                mark_photo_noted(doc_id)
                project_name = expense.get("project", "")
                _respond(format_photo_reminder(expense, project_name))
    except Exception as e:
        print(f"[DoubleA] 圖片處理失敗：{e}")


def process_image_as_calendar(
    chat_id: str, reply_token: str | None = None, trigger_text: str | None = None
) -> None:
    """使用者傳完照片後接著輸入「加到行事曆」等觸發語 → 才真正解析圖片並建立行程。

    trigger_text：使用者輸入的觸發語原文（例如「加到行事曆10/5」），
    若照片本身沒有日期／標題，會優先採用這段文字裡的日期／時間線索。
    """
    print(f"[DoubleA] process_image_as_calendar 開始：chat_id={chat_id}")
    now = datetime.now(TAIPEI_TZ)
    _used_reply: list[bool] = [False]

    def _respond(msg: str) -> None:
        if reply_token and not _used_reply[0]:
            try:
                _reply_line(reply_token, msg)
                _used_reply[0] = True
                return
            except Exception as _e:
                print(f"[DoubleA] calendar-from-image reply 失敗，改用 push：{_e}")
        try:
            _push_line(chat_id, msg)
        except Exception as _e:
            print(f"[DoubleA] calendar-from-image push 最終失敗：{_e}")

    pending = load_pending_image(chat_id)
    if not pending:
        _respond("⚠️ 沒有找到最近的照片，請先傳照片，再輸入「加到行事曆」。")
        return

    ts = pending.get("ts", 0)
    if time.time() - ts > PENDING_IMAGE_TTL_SECONDS:
        clear_pending_image(chat_id)
        _respond("⚠️ 照片已超過時效，請重新傳一次照片，再輸入「加到行事曆」。")
        return

    message_id = pending["message_id"]

    # 不再推播「⏳ 正在分析…」：每次推播到群組都會扣掉群組人數的額度。

    try:
        image_bytes, mime_type = _download_line_content(message_id)
    except Exception as e:
        print(f"[DoubleA] process_image_as_calendar 圖片下載失敗：{e!r}")
        _respond("⚠️ 圖片下載失敗，請稍後再試。")
        return

    try:
        result = parse_image_for_event(image_bytes, mime_type, now, text_hint=trigger_text)
    except Exception as e:
        print(f"[DoubleA] process_image_as_calendar 圖片分析失敗：{e!r}")
        _respond("⚠️ 圖片分析失敗，請稍後再試。")
        return

    if result.get("type") != "calendar":
        _respond("⚠️ 沒有從這張照片偵測到可建立的行程內容。")
        return

    events = result.get("events") or []
    if not events:
        _respond("⚠️ 沒有從這張照片偵測到可建立的行程內容。")
        return

    clear_pending_image(chat_id)

    if len(events) == 1:
        ev = events[0]
        _fix_event_times(ev)
        try:
            created = create_calendar_event(ev)
            save_last_event(created["id"], ev)
            schedule_event_reminder(chat_id, ev)
            reply = _format_calendar_confirmation(ev, created["link"])
            reply += f"\n\n📸 已從照片建立！"
        except Exception as e:
            _respond("⚠️ 偵測到行程但建立失敗，請稍後再試。")
            return
        _respond(reply)
    else:
        succeeded, failed = [], []
        for ev in events:
            _fix_event_times(ev)
            try:
                created = create_calendar_event(ev)
                save_last_event(created["id"], ev)
                schedule_event_reminder(chat_id, ev)
                succeeded.append({"event_data": ev, "link": created["link"]})
            except Exception as e:
                failed.append(ev.get("title", "未知"))
        if succeeded:
            reply = _format_multi_calendar_confirmation(succeeded)
            reply += "\n\n📸 已從照片建立！"
            if failed:
                reply += f"\n\n⚠️ 以下建立失敗：{'、'.join(failed)}"
            _respond(reply)
        elif failed:
            _respond("⚠️ 偵測到行程但建立失敗，請稍後再試。")


# ── Location（記帳 GPS 附加）────────────────────────────────────────────────────

def process_location(title: str, address: str, lat: float, lon: float,
                     chat_id: str, reply_token: str | None = None) -> None:
    """背景任務：使用者傳送位置 → 附加 GPS 到最近一筆花費記錄。"""
    print(f"[DoubleA] process_location：{title} ({lat},{lon}) chat_id={chat_id}")
    if chat_id[:1] in ('C', 'R') or not load_chat_id():
        save_chat_id(chat_id)

    _used_reply: list[bool] = [False]

    def _respond(msg: str) -> None:
        if reply_token and not _used_reply[0]:
            try:
                _reply_line(reply_token, msg)
                _used_reply[0] = True
                return
            except Exception as _e:
                print(f"[DoubleA] location reply 失敗，改用 push：{_e}")
        try:
            _push_line(chat_id, msg)
        except Exception as _e:
            print(f"[DoubleA] location push 最終失敗：{_e}")

    try:
        last = get_last_expense_any(chat_id)
        if not last:
            _respond("📍 收到位置！但目前沒有花費記錄可附加。\n請先用「+花費 ...」記帳，再傳位置給我。")
            return
        expense, doc_id = last
        attach_location(doc_id, title, float(lat), float(lon), address)
        gps = {"title": title, "lat": lat, "lon": lon, "address": address}
        _respond(format_location_attached(expense, gps))
    except Exception as e:
        print(f"[DoubleA] 位置附加失敗：{e}")
        _respond("⚠️ 位置記錄失敗，請稍後再試。")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    sig_value = signature.removeprefix("sha256=")
    computed = base64.b64encode(
        hmac.new(LINE_CHANNEL_SECRET.lower().encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    if not hmac.compare_digest(computed, sig_value):
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid body")

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        source = event.get("source", {})
        source_type = source.get("type", "user")
        chat_id = source.get("groupId") or source.get("roomId") or source.get("userId", "")
        reply_token = event.get("replyToken")

        if msg.get("type") == "text":
            text = msg.get("text", "").strip()
            if not text:
                continue
            if source_type in ("group", "room"):
                # 原本要求群組訊息必須包含 @培正家AI小幫手 標籤才處理，
                # 但實測發現用 LINE「回覆訊息」功能帶出來的 @標籤只是畫面上的引用
                # 顯示，並不會真的包進 webhook 收到的文字內容，導致訊息永遠被略過。
                # 家人用習慣是直接回覆或直接說話，因此改為群組訊息一律處理；
                # 若訊息剛好還是包含手打的標籤文字，先把它拿掉再往下走，
                # 避免標籤文字干擾 AI 判斷或關鍵字比對。
                if BOT_MENTION.lower() in text.lower():
                    text = re.sub(re.escape(BOT_MENTION), "", text, flags=re.IGNORECASE).strip()
                if not text:
                    continue
            background_tasks.add_task(process_message, text, chat_id, reply_token)
        elif msg.get("type") == "image":
            message_id = msg.get("id", "")
            if message_id:
                background_tasks.add_task(process_image, message_id, chat_id, reply_token)
        elif msg.get("type") == "location":
            title = msg.get("title", "")
            address = msg.get("address", "")
            lat = msg.get("latitude", 0)
            lon = msg.get("longitude", 0)
            background_tasks.add_task(process_location, title, address, lat, lon, chat_id, reply_token)

    return JSONResponse(content={"status": "ok"})


@app.post("/morning-briefing")
async def morning_briefing():
    chat_id = load_chat_id()
    if not chat_id:
        return JSONResponse(content={"status": "no_chat_id"})
    now = datetime.now(TAIPEI_TZ)
    sections = []
    try:
        cal_events = list_events_for_date(now)
        if cal_events:
            lines = ["📅 今日行事曆\n"]
            for ev in cal_events:
                time_prefix = f"{ev['start_str']} " if ev["start_str"] else ""
                lines.append(f"・{time_prefix}{ev['title']}")
            sections.append("\n".join(lines))
    except Exception as e:
        print(f"[DoubleA] 早安行事曆取得失敗：{e}")
    try:
        tasks = get_pending_tasks()
        if tasks:
            lines = ["📋 待辦清單\n"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t['title']}")
            lines.append(f"\n🔗 {TASKS_URL}")
            sections.append("\n".join(lines))
    except Exception as e:
        print(f"[DoubleA] 早安待辦取得失敗：{e}")
    msg = "🌅 早安！今天沒有特別安排，好好享受吧！" if not sections else "🌅 早安！今天的安排：\n\n" + "\n\n".join(sections)
    _push_line(chat_id, msg)
    return JSONResponse(content={"status": "ok"})


@app.post("/daily-reminder")
async def daily_reminder():
    chat_id = load_chat_id()
    if not chat_id:
        return JSONResponse(content={"status": "no_chat_id"})
    now = datetime.now(TAIPEI_TZ)
    sections = []
    try:
        cal_events = list_events_for_date(now)
        remaining = [ev for ev in cal_events if ev["start_str"] > "17:00"]
        if remaining:
            lines = ["📅 今晚行事曆\n"]
            for ev in remaining:
                lines.append(f"・{ev['start_str']} {ev['title']}")
            sections.append("\n".join(lines))
    except Exception as e:
        print(f"[DoubleA] 下午行事曆取得失敗：{e}")
    try:
        tasks = get_pending_tasks()
        if tasks:
            lines = ["📋 待辦提醒\n"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t['title']}")
            lines.append(f"\n🔗 {TASKS_URL}")
            sections.append("\n".join(lines))
    except Exception as e:
        print(f"[DoubleA] 下午待辦取得失敗：{e}")
    if not sections:
        return JSONResponse(content={"status": "nothing_to_send"})
    msg = "🌆 下午好！來看看今天還有什麼：\n\n" + "\n\n".join(sections)
    _push_line(chat_id, msg)
    return JSONResponse(content={"status": "ok"})


@app.post("/check-reminders")
async def check_reminders():
    now = datetime.now(TAIPEI_TZ)
    due = get_due_reminders(now)
    for r in due:
        try:
            send_event_reminder(r["chat_id"], r["title"], r["start"])
            mark_reminder_sent(r.get("_doc_id", r.get("_id")))
        except Exception as e:
            print(f"[DoubleA] 提醒發送失敗：{e}")
    return JSONResponse(content={"status": "ok", "sent": len(due)})


@app.get("/selftest")
def selftest_endpoint(token: str = ""):
    """自我檢查：需在環境變數設定 SELFTEST_TOKEN，並以 ?token= 帶入；未設定則停用。"""
    expected = os.environ.get("SELFTEST_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="Not Found")
    from selftest import run_selftest
    results = run_selftest(scheduler)
    return {"all_ok": all(r["ok"] for r in results), "results": results}


def _check_admin_token(token: str) -> None:
    """允許兩種通行碼：環境變數 SELFTEST_TOKEN，或在 LINE 傳「重新授權」取得的一次性連結（15 分鐘內有效）。"""
    token = token or ""
    expected = os.environ.get("SELFTEST_TOKEN", "")
    if expected and hmac.compare_digest(token, expected):
        return
    st = load_state()
    nonce, exp = st.get("reauth_nonce") or "", st.get("reauth_exp") or 0
    if nonce and token and time.time() < exp and hmac.compare_digest(token, nonce):
        return
    raise HTTPException(status_code=404, detail="Not Found")


def _public_base_url() -> str:
    host = os.environ.get("PUBLIC_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "web-production-040883.up.railway.app"
    return host if host.startswith("http") else f"https://{host}"


def _reauth_page(token: str, notice: str = "") -> str:
    from reauth_service import build_auth_url
    try:
        auth_url = build_auth_url()
        link = f'<a class="btn" href="{auth_url}" target="_blank" rel="noopener">① 登入 Google 並授權</a>'
    except Exception as e:
        link = f"<p>⚠️ 讀不到 Google 用戶端設定：{type(e).__name__}</p>"
    tok = urllib.parse.quote(token)
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>重新授權 Google｜培正家AI小幫手</title>
<style>body{{font-family:sans-serif;max-width:640px;margin:32px auto;padding:0 16px;line-height:1.7}}
.btn{{display:inline-block;background:#0f6b5c;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none}}
textarea{{width:100%;min-height:90px;font-size:14px}} button{{font-size:16px;padding:8px 18px}}
.n{{padding:10px 14px;border-radius:8px;background:#eef6f3}}</style></head><body>
<h1>重新授權 Google 行事曆／待辦</h1>
{f'<p class="n">{notice}</p>' if notice else ''}
<p>{link}</p>
<p>② 用 Google 帳號登入、按「繼續／允許」。最後瀏覽器會跳到一個<b>「無法連線」</b>的 localhost 頁面，這是正常的。</p>
<p>③ 把那一頁<b>網址列的整串網址</b>複製，貼到下面送出。</p>
<form method="post" action="/reauth?token={tok}">
<textarea name="pasted" placeholder="http://localhost:8765/?code=...&scope=..."></textarea>
<p><button type="submit">④ 送出完成授權</button></p></form>
</body></html>"""


@app.get("/reauth", response_class=HTMLResponse)
def reauth_get(token: str = ""):
    _check_admin_token(token)
    return _reauth_page(token)


@app.post("/reauth", response_class=HTMLResponse)
async def reauth_post(request: Request, token: str = ""):
    _check_admin_token(token)
    from reauth_service import exchange_code, extract_code
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8"))
    code = extract_code((form.get("pasted") or [""])[0])
    if not code:
        return _reauth_page(token, "⚠️ 沒有找到授權碼，請貼上 localhost 那一頁網址列的整串網址。")
    try:
        exchange_code(code)
        n = len(list_events_for_date(datetime.now(TAIPEI_TZ)))
        msg = f"✅ 授權成功！已讀到今天 {n} 筆行程，行事曆與待辦恢復正常。"
        if not is_persistent():
            msg += "（注意：尚未設定永久磁碟 DATA_DIR，重新部署後需再授權一次）"
        print("[DoubleA] Google 重新授權成功")
        return _reauth_page(token, msg)
    except Exception as e:
        print(f"[DoubleA] Google 重新授權失敗：{e}")
        return _reauth_page(token, f"⚠️ 授權失敗：{e}。授權碼只能用一次，請從 ① 重新開始。")


@app.get("/health")
def health():
    return {"status": "ok", "bot": "DoubleA", "env": "cloud" if os.environ.get("K_SERVICE") else "local"}


# ── 首頁／隱私權政策：純粹為了滿足 Google OAuth consent screen 發佈到
# 「正式環境」時要求填寫的「應用程式首頁」與「隱私權政策網址」，
# 本機器人僅供培正家家人私人使用，不對外公開招募使用者。

@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>培正家AI小幫手</title></head><body style="font-family:sans-serif;max-width:600px;margin:40px auto;line-height:1.7">
<h1>培正家AI小幫手</h1>
<p>這是培正家的私人 LINE 家庭助理機器人，僅供家人使用，協助管理行事曆、待辦事項、購物清單、記帳與天氣查詢等日常事務。</p>
<p>本服務不對外公開招募使用者。</p>
<p><a href="/privacy">隱私權政策</a></p>
</body></html>"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>隱私權政策 － 培正家AI小幫手</title></head><body style="font-family:sans-serif;max-width:600px;margin:40px auto;line-height:1.7">
<h1>隱私權政策</h1>
<p>培正家AI小幫手（下稱「本服務」）是僅供培正家家人使用的私人 LINE 機器人，不對外公開招募使用者，也不會將任何資料提供、販售或分享給無關第三方。</p>
<h2>蒐集的資料</h2>
<p>本服務會處理使用者在 LINE 對話中主動提供的文字、照片與位置資訊，用於建立行事曆事件、待辦事項、購物清單、記帳記錄等功能，並經授權存取使用者的 Google 日曆與 Google Tasks 以完成上述功能。</p>
<h2>資料儲存與使用</h2>
<p>相關資料僅儲存於本服務的雲端資料庫與使用者本人的 Google 帳號（日曆／Tasks）中，僅用於提供上述功能，不做其他用途。</p>
<h2>聯絡方式</h2>
<p>如有任何問題，請透過 LINE 直接聯繫本服務管理者。</p>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
