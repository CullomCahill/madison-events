"""
Dragonfly Hot Yoga -> Google Calendar

Pulls the week ahead of yoga classes from Dragonfly's Arketa booking widget
and writes them into a dedicated "Yoga" Google Calendar.

Run this Monday morning. It writes Monday 00:00 through Sunday 23:59 local.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

Reads credentials from a .env file in the working directory (or any parent):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
    YOGA_CALENDAR_ID

Requires:
    pip install requests python-dotenv google-auth google-api-python-client
"""

import datetime as dt
import os
import random
import time
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ---------------------------------------------------------------- config

WIDGET_NAME = "dragonflyhotyoga"
API_URL = "https://app.arketa.co/api/widget/data"
REFERER = f"https://app.arketa.co/iframe/{WIDGET_NAME}/calendar"

TZ = ZoneInfo("America/Chicago")

# Which studios you care about. These match location.name in the payload.
# Full set available: East, Downtown, Middleton, Fitchburg, Verona
LOCATIONS = {"East", "Downtown"}

# Dragonfly tags every class with workout_types. Yoga classes carry "Yoga";
# Power Up and Cardio Barre carry Cardio/Strength instead. Filtering on the
# tag rather than on class names means new class names work automatically.
WORKOUT_TYPE = "Yoga"

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Change this and the
# cleanup pass stops recognizing previously written events.
SOURCE_TAG = "dragonfly-yoga-sync"

HEADERS = {
    "referer": REFERER,
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
    "content-type": "application/json",
}


# ------------------------------------------------------------- time math

def week_window(today=None):
    """Return (start, end) datetimes for Monday 00:00 through the Monday
    00:00 two weeks out, local time.

    If run on a Monday you get that same day forward. If run any other day
    you still get the Monday of the current week, so a manual mid-week
    re-run repairs the current week rather than jumping ahead.
    """
    today = today or dt.datetime.now(TZ)
    monday = (today - dt.timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + dt.timedelta(days=14)


def arketa_week_anchors(start, end):
    """Arketa serves Sunday-to-Saturday weeks keyed by a Unix timestamp at
    Sunday 00:00 local. Our Monday-to-Sunday window always straddles two of
    them, so return both anchors."""
    # Sunday on or before our start
    first_sunday = start - dt.timedelta(days=(start.weekday() + 1) % 7)
    anchors = []
    cursor = first_sunday
    while cursor < end:
        anchors.append(int(cursor.timestamp()))
        cursor += dt.timedelta(days=7)
    return anchors


# --------------------------------------------------------------- fetch

def fetch_classes(anchors):
    """Fetch each Arketa week and return a deduped list of class dicts."""
    by_id = {}
    for anchor in anchors:
        resp = requests.get(
            API_URL,
            params={
                "widgetName": WIDGET_NAME,
                "type": "classes",
                "start_time": anchor,
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        for c in resp.json()["data"]["classes"]:
            by_id[c["id"]] = c
    return list(by_id.values())


def keep(c, start, end):
    if c.get("canceled") or c.get("deleted"):
        return False
    if c.get("location_type") != "inperson":
        return False
    if (c.get("location") or {}).get("name") not in LOCATIONS:
        return False
    if WORKOUT_TYPE not in (c.get("workout_types") or []):
        return False
    begins = dt.datetime.fromtimestamp(c["start_time"], TZ)
    return start <= begins < end


# ------------------------------------------------------- event building

def to_event(c):
    loc = c["location"]
    name = c.get("class_name") or c.get("name") or "Class"
    instructor = c.get("hostName") or c.get("host_name") or ""

    begins = dt.datetime.fromtimestamp(c["start_time"], TZ)
    ends = dt.datetime.fromtimestamp(c["end_time"], TZ)

    spots_left = None
    if c.get("max_capacity") is not None and c.get("total_booked") is not None:
        spots_left = max(c["max_capacity"] - c["total_booked"], 0)

    lines = []
    if instructor:
        lines.append(f"Instructor: {instructor}")
    if c.get("intensity"):
        lines.append(f"Intensity: {c['intensity']}")
    if spots_left is not None:
        lines.append(f"Spots open at sync time: {spots_left} of {c['max_capacity']}")
    lines.append(f"Book: https://app.arketa.co/{WIDGET_NAME}/calendar")
    if c.get("description"):
        lines.append("")
        lines.append(c["description"])

    return {
        "summary": f"Dragonfly {loc['name']} - {name}",
        "location": loc.get("address", ""),
        "description": "\n".join(lines),
        "start": {"dateTime": begins.isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": ends.isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
        "source": {
            "url": f"https://app.arketa.co/{WIDGET_NAME}/calendar",
            "title": "Dragonfly Hot Yoga booking",
        },
        "extendedProperties": {
            "private": {
                "source": SOURCE_TAG,
                "classId": c["id"],
            }
        },
    }


# ------------------------------------------------------------- calendar

def calendar_service():
    """Build the Calendar client from refresh token credentials in .env."""
    missing = [
        k
        for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        if not os.environ.get(k)
    ]
    if missing:
        raise SystemExit(f"Missing from .env: {', '.join(missing)}")

    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def execute_with_backoff(request, max_tries=6):
    """Run a Calendar API request, retrying transient failures with
    exponential backoff plus jitter.

    The six sync scripts in this repo run as parallel GitHub Actions matrix
    jobs, all writing to the same calendar at once, which can trip Google's
    short-burst rate limiting well under any daily quota. Retrying with
    backoff rides that out instead of failing the job.
    """
    for attempt in range(max_tries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            transient = status in (429, 500, 503) or (
                status == 403 and "ratelimitexceeded" in str(e).lower()
            )
            if not transient or attempt == max_tries - 1:
                raise
            time.sleep(2**attempt + random.uniform(0, 1))


def clear_window(svc, start, end):
    """Delete previously synced events in the window. Only touches events
    carrying our own stamp, so anything you added by hand is safe."""
    removed = 0
    page = None
    while True:
        resp = execute_with_backoff(
            svc.events().list(
                calendarId=CALENDAR_ID,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                privateExtendedProperty=f"source={SOURCE_TAG}",
                singleEvents=True,
                maxResults=250,
                pageToken=page,
            )
        )
        for ev in resp.get("items", []):
            execute_with_backoff(
                svc.events().delete(calendarId=CALENDAR_ID, eventId=ev["id"])
            )
            removed += 1
        page = resp.get("nextPageToken")
        if not page:
            break
    return removed


def main():
    start, end = week_window()
    print(f"Window: {start:%a %b %d} through {end - dt.timedelta(days=1):%a %b %d}")

    raw = fetch_classes(arketa_week_anchors(start, end))
    classes = [c for c in raw if keep(c, start, end)]
    classes.sort(key=lambda c: c["start_time"])
    print(f"Fetched {len(raw)} classes, kept {len(classes)}")

    if not classes:
        print("Nothing matched. Not touching the calendar.")
        return

    svc = calendar_service()
    print(f"Removed {clear_window(svc, start, end)} previously synced events")

    for c in classes:
        ev = to_event(c)
        execute_with_backoff(svc.events().insert(calendarId=CALENDAR_ID, body=ev))
        begins = dt.datetime.fromtimestamp(c["start_time"], TZ)
        print(f"  {begins:%a %m/%d %I:%M %p}  {ev['summary']}")

    print(f"Wrote {len(classes)} events")


if __name__ == "__main__":
    main()
