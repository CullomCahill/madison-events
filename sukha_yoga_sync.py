"""
Sukha Somatics -> Google Calendar

Pulls the week ahead of yoga classes from Sukha's Momence booking widget and
writes them into the shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

Source: Momence read-only host-schedule API (Tier 1, platform API).
    https://readonly-api.momence.com/host-plugins/host/35255/host-schedule/sessions
No auth, no cookies, no referer gate. Verified 2026-09-05.

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

HOST_ID = 35255
API_URL = f"https://readonly-api.momence.com/host-plugins/host/{HOST_ID}/host-schedule/sessions"
BOOKING_PAGE = "https://sukhasomatics.com/signup/"

STUDIO = "Sukha Somatics"
ADDRESS = "312 N Third St #2, Madison, WI"

TZ = ZoneInfo("America/Chicago")

# These are the sessionTypes the studio's own widget requests. Momence will
# happily return an empty list if you ask for a type they don't use.
SESSION_TYPES = [
    "course-class",
    "fitness",
    "retreat",
    "special-event",
    "special-event-new",
]

# Momence has NO structured discipline tag. Every Sukha session comes back
# with type == "fitness", which is Momence's booking category, not a subject.
# So this is name matching, and per the brief it is an allowlist.
#
# Anything whose name contains "yoga" is kept automatically. ALSO_YOGA is for
# classes you decide count even though the name doesn't say yoga.
#
# Over a sample month (2026-09-07 to 2026-10-07) the full set of names was:
#     Flow Yoga                             -> kept (contains "yoga")
#     Gentle Yoga                           -> kept
#     Gentle Restorative Yoga w/ Sound Bath -> kept
#     Gentle Restorative Yoga               -> kept
#     Queer & Trans Yoga (PWYC)             -> kept
#     Tension & Fascial Release Yoga        -> kept
#     All Levels TRE                        -> DROPPED (trauma release, not yoga)
#     Mindful Movement for Joint Health     -> DROPPED (judgment call)
# Add either of those two to ALSO_YOGA if you want them on the calendar.
ALSO_YOGA: set[str] = set()

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Must be unique per studio:
# if two scripts share a tag, each one's cleanup pass deletes the other's
# events.
SOURCE_TAG = "sukha-yoga-sync"

HEADERS = {
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}


# ------------------------------------------------------------- time math

def week_window(now=None):
    """Return (start, end) covering the current week plus the following
    one, with the start clamped to now.

    end is always the Monday 00:00 local two weeks out. start is whichever
    is later of this week's Monday 00:00 and the current moment, so a
    Wednesday or Saturday re-run only ever writes what is still ahead.
    """
    now = now or dt.datetime.now(TZ)
    monday = (now - dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(monday, now), monday + dt.timedelta(days=14)


# --------------------------------------------------------------- fetch

def fetch_sessions(start, end):
    """Momence takes an explicit UTC fromDate/toDate, so one request covers
    the whole window. No week-alignment games needed."""
    params = [("sessionTypes[]", t) for t in SESSION_TYPES]
    params += [
        ("fromDate", start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
        ("toDate", end.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
        ("pageSize", "200"),
        ("page", "0"),
        ("timeZone", "America/Chicago"),
    ]
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    sessions = body["payload"]

    # Page if the studio ever exceeds one page in a week.
    total = (body.get("pagination") or {}).get("totalCount", len(sessions))
    page = 1
    while len(sessions) < total:
        paged = [p for p in params if p[0] != "page"] + [("page", str(page))]
        r = requests.get(API_URL, params=paged, headers=HEADERS, timeout=30)
        r.raise_for_status()
        chunk = r.json()["payload"]
        if not chunk:
            break
        sessions.extend(chunk)
        page += 1

    return sessions


def is_yoga(name):
    n = (name or "").strip()
    return "yoga" in n.lower() or n in ALSO_YOGA


def parse_utc(value):
    """Momence returns startsAt/endsAt as UTC ISO with a trailing Z."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


def keep(s, start, end):
    if s.get("isCancelled"):
        return False
    # inPerson is Momence's own boolean. Online-only sessions come back False.
    if not s.get("inPerson"):
        return False
    if not is_yoga(s.get("sessionName")):
        return False
    begins = parse_utc(s["startsAt"])
    return start <= begins < end


# ------------------------------------------------------- event building

def to_event(s):
    name = s.get("sessionName") or "Class"
    instructor = s.get("teacher") or ""

    begins = parse_utc(s["startsAt"])
    ends = parse_utc(s["endsAt"])

    lines = []
    if instructor:
        lines.append(f"Instructor: {instructor}")
    # Momence has no intensity field for this host. The `level` field is
    # NOT a level: on every Sukha record it holds the class description.
    # Checked against several records before relying on it.
    if s.get("remainingSpots") is not None and s.get("capacity"):
        lines.append(
            f"Spots open at sync time: {s['remainingSpots']} of {s['capacity']}"
        )
    booking_link = s.get("link") or BOOKING_PAGE
    lines.append(f"Book: {booking_link}")

    description = (s.get("level") or "").strip()
    if description:
        lines.append("")
        lines.append(description)

    return {
        "summary": f"{STUDIO} - {name}",
        "location": ADDRESS,
        "description": "\n".join(lines),
        "start": {"dateTime": begins.isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": ends.isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
        "source": {"url": booking_link, "title": "Sukha Somatics booking"},
        "extendedProperties": {
            "private": {
                "source": SOURCE_TAG,
                "classId": str(s["id"]),
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
    carrying our own stamp, so anything you added by hand is safe.

    Note this uses the clamped start, so classes that already happened this
    week are left alone rather than being deleted and not rewritten.
    """
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
    print(f"Window: {start:%a %b %d %I:%M %p} through {end:%a %b %d}")

    raw = fetch_sessions(start, end)
    classes = [s for s in raw if keep(s, start, end)]
    classes.sort(key=lambda s: s["startsAt"])
    print(f"Fetched {len(raw)} sessions, kept {len(classes)}")

    if not classes:
        print("Nothing matched. Not touching the calendar.")
        return

    svc = calendar_service()
    print(f"Removed {clear_window(svc, start, end)} previously synced events")

    for s in classes:
        ev = to_event(s)
        execute_with_backoff(svc.events().insert(calendarId=CALENDAR_ID, body=ev))
        print(f"  {parse_utc(s['startsAt']):%a %m/%d %I:%M %p}  {ev['summary']}")

    print(f"Wrote {len(classes)} events")


if __name__ == "__main__":
    main()
