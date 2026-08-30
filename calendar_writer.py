import re

from googleapiclient.discovery import build

CALENDAR_NAME = "Madison Events"
TITLE_SIMILARITY_THRESHOLD = 0.35
STOPWORDS = {"the", "a", "an", "and", "or", "to", "at", "in", "on", "of", "with", "for"}


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


def check_event_exists(service, cal_id: str, title: str, start_datetime: str) -> bool:
    day = start_datetime[:10]
    time_min = f"{day}T00:00:00Z"
    time_max = f"{day}T23:59:59Z"

    resp = (
        service.events()
        .list(calendarId=cal_id, timeMin=time_min, timeMax=time_max)
        .execute()
    )
    for event in resp.get("items", []):
        existing = event.get("summary", "")
        if _title_similarity(title, existing) >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def write_event(service, cal_id: str, event: dict) -> None:
    description_lines = [event.get("one_line_description", "")]
    if event.get("source_subject"):
        description_lines.append(f"Source: {event['source_subject']}")
    if event.get("url"):
        description_lines.append(f"URL: {event['url']}")
    if event.get("confidence") is not None:
        description_lines.append(f"Confidence: {event['confidence']}")

    body = {
        "summary": event["title"],
        "location": event.get("location") or "",
        "description": "\n".join(description_lines),
        "transparency": "transparent",
        "start": _time_field(event["start_datetime"]),
        "end": _time_field(event.get("end_datetime") or event["start_datetime"]),
    }

    service.events().insert(calendarId=cal_id, body=body).execute()


def _time_field(dt_str: str) -> dict:
    if "T" in dt_str:
        return {"dateTime": dt_str, "timeZone": "America/Chicago"}
    return {"date": dt_str}
