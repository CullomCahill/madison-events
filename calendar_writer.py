import re
from datetime import date, datetime, timedelta, time as dtime

from googleapiclient.discovery import build

CALENDAR_NAME = "Madison Events"
TITLE_SIMILARITY_THRESHOLD = 0.35
STOPWORDS = {"the", "a", "an", "and", "or", "to", "at", "in", "on", "of", "with", "for"}

DEFAULT_DURATION_HOURS = 2
REMINDER_HOUR = 20  # 8:00 pm local, the evening before the event


def get_calendar_service(creds):
    return build("calendar", "v3", credentials=creds)


def get_or_create_radar_calendar(service) -> str:
    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        for cal in resp.get("items", []):
            if cal.get("summary") == CALENDAR_NAME:
                return cal["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    created = service.calendars().insert(
        body={"summary": CALENDAR_NAME, "timeZone": "America/Chicago"}
    ).execute()
    print(f"calendar_writer.py: created calendar '{CALENDAR_NAME}' ({created['id']})")
    return created["id"]


def _significant_words(title: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    return {t for t in tokens if t not in STOPWORDS}


def _title_similarity(a: str, b: str) -> float:
    wa, wb = _significant_words(a), _significant_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_all_day(dt_str: str) -> bool:
    return "T" not in dt_str


def naive_datetime(dt_str: str) -> datetime:
    """Parse an ISO string and drop any timezone info.

    We always pass a separate `timeZone: America/Chicago` field alongside
    `dateTime` to Google, so treat every timestamp as naive local time.
    """
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt.replace(tzinfo=None)


def _all_day_end_exclusive(last_day_str: str) -> str:
    # Google Calendar's all-day `end.date` is EXCLUSIVE (the day AFTER the
    # event actually ends) while `start.date` is inclusive. A trip running
    # the 4th through the 6th needs end.date = the 7th. This applies even
    # to single-day all-day events (end.date = start.date + 1 day).
    return (date.fromisoformat(last_day_str) + timedelta(days=1)).isoformat()


def _reminder_minutes_before(start_str: str) -> int:
    if is_all_day(start_str):
        start_dt = datetime.combine(date.fromisoformat(start_str), dtime(0, 0))
    else:
        start_dt = naive_datetime(start_str)
    reminder_dt = datetime.combine(start_dt.date() - timedelta(days=1), dtime(REMINDER_HOUR, 0))
    return max(int((start_dt - reminder_dt).total_seconds() // 60), 0)


def build_event_body(event: dict) -> dict:
    start_str = event["start_datetime"]
    all_day = is_all_day(start_str)

    description_lines = [event.get("one_line_description", "")]
    if event.get("source_subject"):
        description_lines.append(f"Source: {event['source_subject']}")
    if event.get("url"):
        description_lines.append(f"URL: {event['url']}")
    if event.get("confidence") is not None:
        description_lines.append(f"Confidence: {event['confidence']}")

    if all_day:
        start_field = {"date": start_str}
        last_day = event.get("end_datetime") or start_str
        end_field = {"date": _all_day_end_exclusive(last_day)}
    else:
        end_str = event.get("end_datetime")
        if not end_str or end_str == start_str:
            assumed_end = naive_datetime(start_str) + timedelta(hours=DEFAULT_DURATION_HOURS)
            end_str = assumed_end.isoformat()
            description_lines.append(f"(End time assumed: {DEFAULT_DURATION_HOURS}h after start)")
        start_field = {"dateTime": start_str, "timeZone": "America/Chicago"}
        end_field = {"dateTime": end_str, "timeZone": "America/Chicago"}

    body = {
        "summary": event["title"],
        "location": event.get("location") or "",
        "description": "\n".join(line for line in description_lines if line),
        "transparency": "transparent",
        "start": start_field,
        "end": end_field,
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": _reminder_minutes_before(start_str)}],
        },
    }
    if event.get("recurrence"):
        body["recurrence"] = [event["recurrence"]]
    return body


def check_event_exists(service, cal_id: str, event: dict) -> bool:
    start_str = event["start_datetime"]
    ref_date = date.fromisoformat(start_str[:10])

    if event.get("recurrence"):
        # Recurring series drift forward each run ("next occurrence"), so
        # search a wider window rather than the exact anchor day.
        time_min = f"{(ref_date - timedelta(days=7)).isoformat()}T00:00:00Z"
        time_max = f"{(ref_date + timedelta(days=7)).isoformat()}T23:59:59Z"
    else:
        time_min = f"{ref_date.isoformat()}T00:00:00Z"
        time_max = f"{ref_date.isoformat()}T23:59:59Z"

    resp = (
        service.events()
        .list(calendarId=cal_id, timeMin=time_min, timeMax=time_max, singleEvents=False)
        .execute()
    )
    for existing in resp.get("items", []):
        existing_title = existing.get("summary", "")
        if _title_similarity(event["title"], existing_title) >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def write_event(service, cal_id: str, event: dict) -> None:
    body = build_event_body(event)
    service.events().insert(calendarId=cal_id, body=body).execute()
