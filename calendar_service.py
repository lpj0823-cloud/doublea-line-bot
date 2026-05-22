import base64
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

CALENDAR_ID = "atticus.wu@gmail.com"
WIFE_EMAIL = "angelsu0923@gmail.com"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_credentials() -> Credentials:
    """Build OAuth credentials from GOOGLE_TOKEN_JSON env var (base64-encoded JSON)."""
    raw = os.environ.get("GOOGLE_TOKEN_JSON", "")
    if not raw:
        raise EnvironmentError("GOOGLE_TOKEN_JSON environment variable is not set")

    token_data = json.loads(base64.b64decode(raw).decode("utf-8"))

    creds = Credentials(
        token=None,
        refresh_token=token_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def create_calendar_event(event_data: dict) -> str:
    """Create a Google Calendar event and invite Angel. Returns the event HTML link."""
    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

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

    return result.get("htmlLink", "")
