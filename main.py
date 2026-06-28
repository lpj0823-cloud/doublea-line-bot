import base64
import hashlib
import hmac
import json
import os
import re
import random
import urllib.parse

import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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
    get_due_reminders,
    load_chat_id,
    load_last_event,
    load_pending_edit,
    mark_reminder_sent,
    save_chat_id,
    save_last_event,
    save_pending_edit,
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
from proverbs_service import get_todays_proverbs, get_proverbs_header
from rate_limiter import check_rate_limit
from birthday_service import (
    add_birthday,
    delete_birthday_by_index,
    get_birthdays,
    get_todays_birthdays,
    parse_birthday_date,
)
from restaurant_service import search_nearby_restaurants, format_restaurant_results

load_dotenv()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"].strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"].strip()

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
REMINDER_MINUTES = 120
BOT_NAME = "培正家AI小幫手"
BOT_MENTION = f"@{BOT_NAME}"

scheduler = AsyncIOScheduler(timezone=TAIPEI_TZ)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.add_job(morning_briefing_job, CronTrigger(hour=7, minute=0, timezone=TAIPEI_TZ))
    scheduler.add_job(birthday_reminder_job, CronTrigger(hour=7, minute=0, timezone=TAIPEI_TZ))
    scheduler.add_job(daily_reminder_job, CronTrigger(hour=17, minute=0, timezone=TAIPEI_TZ))
    scheduler.add_job(proverbs_job, CronTrigger(hour=7, minute=5, timezone=TAIPEI_TZ))
    scheduler.start()
    print("[DoubleA] 排程器已啟動：07:00 早安行程＋生日提醒、07:05 每日箴言、17:00 待辦提醒")
    yield
    scheduler.shutdown()
    print("[DoubleA] 排程器已停止")


app = FastAPI(title="DoubleA LINE Bot", lifespan=lifespan)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


# ── LINE push ─────────────────────────────────────────────────────────────────

def _push_line(chat_id: str, text: str) -> None:
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=chat_id,
                    messages=[TextMessage(text=text)],
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
                    messages=[TextMessage(text=text)],
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
    chat_id = load_chat_id()
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


def proverbs_job() -> None:
    """每日 07:05 排程：推播今日箴言（中文＋英文各一則）。"""
    chat_id = load_chat_id()
    if not chat_id:
        print("[DoubleA] 箴言排程：找不到 chat_id，略過")
        return
    now = datetime.now(TAIPEI_TZ)
    header = get_proverbs_header(now)
    try:
        zh_text, en_text = get_todays_proverbs(now)
    except Exception as e:
        print(f"[DoubleA] 箴言排程：取得經文失敗 {e}")
        return
    try:
        _push_line(chat_id, f"🕊️ 今日箴言\n\n{header}")
        _push_line(chat_id, zh_text)
        _push_line(chat_id, en_text)
        print(f"[DoubleA] 箴言發送成功")
    except Exception as e:
        print(f"[DoubleA] 箴言發送失敗：{e}")


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
        params["details"] = ev["description"]
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
        f"⏰ 將於開始前 2 小時提醒\n\n"
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
        location_line = f"\n   📍 {loc}" if (loc and loc != "null") else ""
        share_link = _generate_share_link(ev)
        lines.append(
            f"{i}.【{ev['title']}】\n"
            f"   🗓 {date_str}{location_line}\n"
            f"   🔗 {r['link']}\n"
            f"   📤 {share_link}"
        )
    lines.append("\n✅ Ginny 已收到邀請\n⏰ 將於各活動開始前 2 小時提醒")
    return "\n\n".join(lines)


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

def handle_command(text: str, chat_id: str, reply_token: str | None = None) -> bool:
    _used: list[bool] = [False]

    def _respond(msg: str) -> None:
        _send_line_msg(chat_id, TextMessage(text=msg), reply_token, _used)

    if text.strip() in ("待辦清單", "待辦", "todo", "TODO"):
        try:
            tasks = get_pending_tasks()
            _respond(_format_todo_list(tasks))
        except Exception as e:
            _respond(f"⚠️ 無法取得待辦清單：{e}")
        return True

    if text.startswith("完成 ") or text.startswith("done "):
        keyword = text.split(" ", 1)[1].strip()
        try:
            title = complete_task_by_keyword(keyword)
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
            _respond(f"⚠️ 無法取得行程：{e}")
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
            _respond(f"⚠️ 無法取得行程：{e}")
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

    if _should_notify(text):
        _push_line(chat_id, "⏳ 收到！處理中，請稍候...")

    result = parse_message(text, now)
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
                reply = "⚠️ 行事曆寫入失敗，請稍後再試。"
            _respond(reply)
        else:
            succeeded, failed = [], []
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
            if succeeded:
                reply = _format_multi_calendar_confirmation(succeeded) + f"\n\n{_cheer_calendar()}"
                if failed:
                    reply += f"\n\n⚠️ 以下事件建立失敗：{'、'.join(failed)}"
            else:
                reply = "⚠️ 行事曆寫入失敗，請稍後再試。"
            _respond(reply)

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
            reply = "⚠️ 待辦事項記錄失敗，請稍後再試。"
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
            reply = "⚠️ 查詢行事曆失敗，請稍後再試。"
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
    print(f"[DoubleA] process_image 開始：message_id={message_id} chat_id={chat_id}")
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
                print(f"[DoubleA] image reply 失敗，改用 push：{_e}")
        try:
            _push_line(chat_id, msg)
        except Exception as _e:
            print(f"[DoubleA] image push 最終失敗：{_e}")

    try:
        _push_line(chat_id, "⏳ 圖片收到，正在分析中...")
    except Exception as e:
        print(f"[DoubleA] 分析中通知失敗：{e}")

    try:
        image_bytes, mime_type = _download_line_content(message_id)
    except Exception as e:
        _respond("⚠️ 圖片下載失敗，請稍後再試。")
        return

    try:
        result = parse_image_for_event(image_bytes, mime_type, now)
    except Exception as e:
        _respond("⚠️ 圖片分析失敗，請稍後再試。")
        return

    if result.get("type") != "calendar":
        return

    events = result.get("events") or []
    if not events:
        return

    if len(events) == 1:
        ev = events[0]
        _fix_event_times(ev)
        try:
            created = create_calendar_event(ev)
            save_last_event(created["id"], ev)
            schedule_event_reminder(chat_id, ev)
            reply = _format_calendar_confirmation(ev, created["link"])
            reply += f"\n\n📸 已從圖片自動偵測並建立！"
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
            reply += "\n\n📸 已從圖片自動偵測並建立！"
            if failed:
                reply += f"\n\n⚠️ 以下建立失敗：{'、'.join(failed)}"
            _respond(reply)


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
                if BOT_MENTION.lower() not in text.lower():
                    continue
                text = re.sub(re.escape(BOT_MENTION), "", text, flags=re.IGNORECASE).strip()
                if not text:
                    continue
            background_tasks.add_task(process_message, text, chat_id, reply_token)
        elif msg.get("type") == "image":
            message_id = msg.get("id", "")
            if message_id:
                background_tasks.add_task(process_image, message_id, chat_id, reply_token)

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


@app.get("/health")
def health():
    return {"status": "ok", "bot": "DoubleA", "env": "cloud" if os.environ.get("K_SERVICE") else "local"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
