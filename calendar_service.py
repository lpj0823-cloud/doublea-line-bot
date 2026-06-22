import os
from datetime import datetime, timedelta

import pytz
from googleapiclient.discovery import build

from google_auth import get_credentials

CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
WIFE_EMAIL = "Ginnycyhuang@gmail.com"
TAIPEI_TZ = pytz.timezone("Asia/Taipei")


def list_events_for_date(date: datetime) -> list[dict]:
    """列出指定日期的所有行事曆事件（台北時間）。回傳 [{"title", "start_str"}, ...]"""
    service = build("calendar", "v3", credentials=get_credentials())

    # 當天 00:00 ~ 23:59:59
    day_start = TAIPEI_TZ.localize(datetime(date.year, date.month, date.day, 0, 0, 0))
    day_end = day_start + timedelta(days=1)

    result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = []
    for item in result.get("items", []):
        title = item.get("summary", "（無標題）")
        start = item["start"].get("dateTime") or item["start"].get("date", "")
        location = item.get("location") or ""
        if start:
            try:
                dt = datetime.fromisoformat(start)
                start_str = dt.strftime("%H:%M")
            except ValueError:
                start_str = start
        else:
            start_str = ""
        events.append({"title": title, "start_str": start_str, "location": location})

    return events


def list_events_for_range(start_date: datetime, end_date: datetime) -> list[dict]:
    """列出日期範圍內的所有行事曆事件（台北時間）。
    回傳 [{"title", "start_str", "date_str", "location"}, ...]，依時間排序。
    """
    service = build("calendar", "v3", credentials=get_credentials())

    range_start = TAIPEI_TZ.localize(datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0))
    range_end = TAIPEI_TZ.localize(datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59))

    result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=range_start.isoformat(),
            timeMax=range_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = []
    for item in result.get("items", []):
        title = item.get("summary", "（無標題）")
        start = item["start"].get("dateTime") or item["start"].get("date", "")
        location = item.get("location") or ""
        if start:
            try:
                dt = datetime.fromisoformat(start)
                if dt.tzinfo is None:
                    dt = TAIPEI_TZ.localize(dt)
                else:
                    dt = dt.astimezone(TAIPEI_TZ)
                date_str = dt.strftime("%-m/%-d")
                start_str = dt.strftime("%H:%M")
            except ValueError:
                date_str = start[:10]
                start_str = ""
        else:
            date_str = ""
            start_str = ""
        events.append({"title": title, "start_str": start_str, "date_str": date_str, "location": location})

    return events


def list_upcoming_events(days: int = 7) -> list[dict]:
    """列出今天起 N 天內的所有行事曆事件，包含 id 欄位。"""
    service = build("calendar", "v3", credentials=get_credentials())
    now = datetime.now(TAIPEI_TZ)
    range_start = TAIPEI_TZ.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
    range_end = range_start + timedelta(days=days)

    result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=range_start.isoformat(),
            timeMax=range_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = []
    for item in result.get("items", []):
        title = item.get("summary", "（無標題）")
        start = item["start"].get("dateTime") or item["start"].get("date", "")
        location = item.get("location") or ""
        try:
            dt = datetime.fromisoformat(start)
            if dt.tzinfo is None:
                dt = TAIPEI_TZ.localize(dt)
            else:
                dt = dt.astimezone(TAIPEI_TZ)
            date_str = dt.strftime("%-m/%-d")
            start_str = dt.strftime("%H:%M")
        except ValueError:
            date_str = start[:10]
            start_str = ""
        events.append({
            "id": item["id"],
            "title": title,
            "date_str": date_str,
            "start_str": start_str,
            "location": location,
        })
    return events


def delete_event_by_id(event_id: str) -> str:
    """以 Google Calendar event ID 直接刪除事件。回傳被刪除的標題。"""
    service = build("calendar", "v3", credentials=get_credentials())
    event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    title = event.get("summary", "（無標題）")
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id, sendUpdates="all").execute()
    return title


def find_and_delete_event(
    target_dt: datetime,
    has_time: bool = True,
    title_hint: str | None = None,
) -> str | None:
    """找到最符合條件的行事曆事件並刪除。回傳被刪除的事件標題，找不到時回傳 None。"""
    service = build("calendar", "v3", credentials=get_credentials())

    if target_dt.tzinfo is None:
        target_dt = TAIPEI_TZ.localize(target_dt)
    else:
        target_dt = target_dt.astimezone(TAIPEI_TZ)

    day_start = TAIPEI_TZ.localize(datetime(target_dt.year, target_dt.month, target_dt.day, 0, 0, 0))
    day_end = day_start + timedelta(days=1)

    result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    items = result.get("items", [])
    if not items:
        return None

    hint = (title_hint or "").strip()

    def time_diff_min(item) -> float:
        start = item["start"].get("dateTime") or item["start"].get("date", "")
        try:
            ev_dt = datetime.fromisoformat(start)
            if ev_dt.tzinfo is None:
                ev_dt = TAIPEI_TZ.localize(ev_dt)
            else:
                ev_dt = ev_dt.astimezone(TAIPEI_TZ)
            return abs((ev_dt - target_dt).total_seconds() / 60)
        except (ValueError, KeyError):
            return 9999.0

    def title_matches(item) -> bool:
        return bool(hint and hint in item.get("summary", ""))

    if has_time:
        # 先找時間在 90 分鐘內的
        candidates = [it for it in items if time_diff_min(it) <= 90]
        if not candidates and hint:
            # 時間找不到時，用標題 fallback
            candidates = [it for it in items if title_matches(it)]
        if not candidates:
            return None
        # 優先標題吻合，再依時間最近排序
        candidates.sort(key=lambda it: (not title_matches(it), time_diff_min(it)))
    else:
        # 沒有指定時間：純靠標題比對
        candidates = [it for it in items if title_matches(it)] if hint else items
        if not candidates:
            return None
        candidates.sort(key=time_diff_min)

    best = candidates[0]
    title = best.get("summary", "（無標題）")
    service.events().delete(
        calendarId=CALENDAR_ID,
        eventId=best["id"],
        sendUpdates="all",
    ).execute()
    return title


def find_and_update_event(
    target_dt: datetime,
    has_time: bool = True,
    title_hint: str | None = None,
    updates: dict | None = None,
) -> dict | None:
    """找到最符合條件的行事曆事件並更新時間或地點。
    updates 可包含 new_start（ISO8601）、new_location（str）。
    改時間時自動保留原本的活動時長。
    回傳 {"title", "new_start", "new_location", "link"} 或 None。
    """
    if not updates:
        return None

    service = build("calendar", "v3", credentials=get_credentials())

    if target_dt.tzinfo is None:
        target_dt = TAIPEI_TZ.localize(target_dt)
    else:
        target_dt = target_dt.astimezone(TAIPEI_TZ)

    day_start = TAIPEI_TZ.localize(datetime(target_dt.year, target_dt.month, target_dt.day, 0, 0, 0))
    day_end = day_start + timedelta(days=1)

    result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    items = result.get("items", [])
    if not items:
        return None

    hint = (title_hint or "").strip()

    def time_diff_min(item) -> float:
        start = item["start"].get("dateTime") or item["start"].get("date", "")
        try:
            ev_dt = datetime.fromisoformat(start)
            if ev_dt.tzinfo is None:
                ev_dt = TAIPEI_TZ.localize(ev_dt)
            else:
                ev_dt = ev_dt.astimezone(TAIPEI_TZ)
            return abs((ev_dt - target_dt).total_seconds() / 60)
        except (ValueError, KeyError):
            return 9999.0

    def title_matches(item) -> bool:
        return bool(hint and hint in item.get("summary", ""))

    if has_time:
        candidates = [it for it in items if time_diff_min(it) <= 90]
        if not candidates and hint:
            candidates = [it for it in items if title_matches(it)]
        if not candidates:
            return None
        candidates.sort(key=lambda it: (not title_matches(it), time_diff_min(it)))
    else:
        candidates = [it for it in items if title_matches(it)] if hint else items
        if not candidates:
            return None
        candidates.sort(key=time_diff_min)

    best = candidates[0]
    event_id = best["id"]
    title = best.get("summary", "（無標題）")

    event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    new_start = updates.get("new_start")
    new_location = updates.get("new_location")

    if new_start:
        # 計算原始時長，套用到新開始時間
        try:
            orig_start = datetime.fromisoformat(event["start"]["dateTime"])
            orig_end = datetime.fromisoformat(event["end"]["dateTime"])
            duration = orig_end - orig_start
            new_start_dt = datetime.fromisoformat(new_start)
            new_end_dt = new_start_dt + duration
            event["start"] = {"dateTime": new_start, "timeZone": "Asia/Taipei"}
            event["end"] = {"dateTime": new_end_dt.isoformat(), "timeZone": "Asia/Taipei"}
        except (KeyError, ValueError):
            event["start"] = {"dateTime": new_start, "timeZone": "Asia/Taipei"}

    if new_location is not None:
        event["location"] = new_location

    updated = (
        service.events()
        .update(calendarId=CALENDAR_ID, eventId=event_id, body=event, sendUpdates="all")
        .execute()
    )

    return {
        "title": title,
        "new_start": new_start,
        "new_location": new_location,
        "link": updated.get("htmlLink", ""),
    }


def create_calendar_event(event_data: dict) -> dict:
    """Create a Google Calendar event and invite Ginny. Returns {"id": ..., "link": ...}"""
    service = build("calendar", "v3", credentials=get_credentials())

    body: dict = {
        "summary": event_data["title"],
        "start": {"dateTime": event_data["start"], "timeZone": "Asia/Taipei"},
        "end": {"dateTime": event_data["end"], "timeZone": "Asia/Taipei"},
        "attendees": [{"email": WIFE_EMAIL}],
        "guestsCanModifyEvent": True,
        "reminders": {"useDefault": True},
    }

    if event_data.get("location"):
        body["location"] = event_data["location"]

    if event_data.get("description"):
        body["description"] = event_data["description"]

    result = (
        service.events()
        .insert(calendarId=CALENDAR_ID, body=body, sendUpdates="all")
        .execute()
    )

    return {"id": result["id"], "link": result.get("htmlLink", "")}


def update_calendar_event(event_id: str, updates: dict) -> str:
    """Update start/end of an existing event. Returns updated event HTML link."""
    service = build("calendar", "v3", credentials=get_credentials())

    event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    if updates.get("start"):
        event["start"] = {"dateTime": updates["start"], "timeZone": "Asia/Taipei"}
    if updates.get("end"):
        event["end"] = {"dateTime": updates["end"], "timeZone": "Asia/Taipei"}

    updated = (
        service.events()
        .update(calendarId=CALENDAR_ID, eventId=event_id, body=event, sendUpdates="all")
        .execute()
    )

    return updated.get("htmlLink", "")
