import os
from datetime import datetime

import pytz
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from calendar_service import create_calendar_event
from event_parser import parse_event

load_dotenv()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

app = FastAPI(title="DoubleA LINE Bot")
parser = WebhookParser(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

TAIPEI_TZ = pytz.timezone("Asia/Taipei")


def _format_confirmation(event_data: dict) -> str:
    start_dt = datetime.fromisoformat(event_data["start"])
    date_str = start_dt.strftime("%-m月%-d日 %H:%M")
    location_line = f"\n📍 {event_data['location']}" if event_data.get("location") else ""
    return (
        f"📅 已加入行事曆！\n\n"
        f"【{event_data['title']}】\n"
        f"🗓 {date_str}{location_line}\n\n"
        f"✅ Angel 已收到邀請"
    )


async def process_message(event: MessageEvent) -> None:
    text = event.message.text.strip()
    now = datetime.now(TAIPEI_TZ)

    calendar_event = parse_event(text, now)
    if not calendar_event:
        return

    try:
        create_calendar_event(calendar_event)
        reply_text = _format_confirmation(calendar_event)
    except Exception as e:
        print(f"[DoubleA] Calendar error: {e}")
        reply_text = "⚠️ 行事曆寫入失敗，請稍後再試。"

    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(
            event.message, TextMessageContent
        ):
            await process_message(event)

    return JSONResponse(content={"status": "ok"})


@app.get("/health")
def health():
    return {"status": "ok", "bot": "DoubleA"}
