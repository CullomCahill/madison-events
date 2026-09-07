"""
Capital Fitness / Yoga Sangha -> Google Calendar

Generates the week ahead from a hardcoded weekly grid and writes it into the
shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

HARDCODED, ON PURPOSE
---------------------
This studio's Mindbody site (studioid 1956) sits behind Cloudflare bot
management (`clients.mindbodyonline.com/classic/ws` returns a 403 "Security
Check" page with a `__cf_bm` challenge cookie even with full browser-style
headers) -- a JS challenge, not fixable from a plain HTTP client. A first
pass instead scraped the schedule text off capitalfitness.net/yoga-sangha
directly, but that page turned out to carry the wrong week's grid mixed in
with the current one, so the safer route is the same one used for the Yoga
Co-op: transcribe the grid by hand below and refresh it manually when the
studio changes its schedule.

WEEKLY_GRID is exactly the schedule the studio publishes: transcribed
2026-09-05. Filtering matches how every other script in this repo treats
this studio's data: `(Group Fitness)` is a literal tag the studio puts on
its non-yoga classes (CAPFIT HIIT, RUN CLUB, ZUMBA, BOOTCAMP, PUMP IT UP,
TRX CIRCUIT), so those are dropped on that signal, plus a denylist for
Pilates. There is no cancellation or instructor data in this source, so the
event description says so plainly.

*** MAINTENANCE: this needs a manual refresh whenever the studio changes its
schedule. There is no way to detect that automatically from this source, so
it can go stale silently. ***

Reads credentials from a .env file in the working directory (or any parent):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
    YOGA_CALENDAR_ID

Requires:
    pip install python-dotenv google-auth google-api-python-client
"""

import datetime as dt
import os
import random
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ---------------------------------------------------------------- config

STUDIO = "Yoga Sangha"
ADDRESS = "15 N. Butler St., Madison, WI 53703"
BOOKING_PAGE = "https://www.capitalfitness.net/yoga-sangha"

TZ = ZoneInfo("America/Chicago")

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# Transcribed verbatim from the studio's published schedule, 2026-09-05.
#     (weekday, start "HH:MM", end "HH:MM", name)
WEEKLY_GRID = [
    (MON, "06:30", "07:30", "Slow Flow"),
    (MON, "07:45", "08:45", "Morning Vinyasa"),
    (MON, "12:00", "12:45", "Express Slow Flow"),
    (MON, "16:15", "17:15", "Ashtanga Inspired"),
    (MON, "17:30", "18:30", "Vinyasa Flow"),
    (MON, "18:45", "19:45", "Slow Flow & Restorative"),

    (TUE, "06:30", "07:30", "Pilates Flow"),
    (TUE, "08:00", "09:00", "Ashtanga Inspired"),
    (TUE, "12:00", "13:00", "Vinyasa Flow"),
    (TUE, "16:15", "17:15", "Yoga Strong"),
    (TUE, "17:30", "18:45", "Heated Slow & Mighty Flow"),
    (TUE, "19:00", "20:00", "Heated Aroma Moon Flow + Restorative"),

    (WED, "06:15", "07:00", "Express Sunrise Flow"),
    (WED, "07:15", "08:15", "Vinyasa Flow"),
    (WED, "12:00", "12:45", "CAPFIT HIIT (Group Fitness)"),
    (WED, "12:00", "13:00", "Vinyasa Flow"),
    (WED, "16:15", "17:15", "Align & Flow"),
    (WED, "17:30", "18:30", "Power Flow"),
    (WED, "17:45", "18:30", "RUN CLUB (Group Fitness)"),
    (WED, "18:45", "19:45", "Meditation & Motion"),

    (THU, "06:30", "07:30", "Vinyasa Flow"),
    (THU, "07:45", "08:45", "Pilates Flow"),
    (THU, "12:00", "13:00", "Slow Flow"),
    (THU, "16:15", "17:15", "Foam Roll & Flow"),
    (THU, "17:30", "18:30", "Mobility Flow"),
    (THU, "18:45", "19:45", "Heated Core Flow"),

    (FRI, "06:30", "07:30", "Pilates Sculpt"),
    (FRI, "12:00", "13:00", "Heated Mobility Flow"),
    (FRI, "16:15", "17:15", "Yin-Yang Reiki Flow"),
    (FRI, "16:45", "17:30", "RUN CLUB (Group Fitness)"),
    (FRI, "17:30", "18:30", "Vinyasa Flow"),
    (FRI, "18:45", "19:45", "Deep Restore"),

    (SAT, "07:45", "08:45", "Pilates Flow"),
    (SAT, "09:00", "10:00", "Vinyasa Flow"),
    (SAT, "09:00", "09:45", "BOOTCAMP (Group Fitness)"),
    (SAT, "10:15", "11:30", "Power Flow"),
    (SAT, "11:00", "11:45", "ZUMBA (Group Fitness)"),

    (SUN, "08:30", "09:30", "Align & Flow"),
    (SUN, "09:00", "09:50", "PUMP IT UP (Group Fitness)"),
    (SUN, "10:00", "10:45", "TRX CIRCUIT (Group Fitness)"),
    (SUN, "10:00", "11:00", "FLOW"),
    (SUN, "16:00", "17:00", "Yoga Strong"),
    (SUN, "17:30", "18:30", "Align & Flow"),
    (SUN, "18:45", "19:30", "Express Deep Restore"),
]

# Same treatment as every other script in this repo: "(Group Fitness)" is a
# literal tag the studio puts on its non-yoga classes, and the rest is a
# denylist for the other non-yoga category mixed into the same list.
EXCLUDE_NAME_SUBSTRINGS = ("pilates", "guided meditation")
EXCLUDE_TAG_SUBSTRING = "(group fitness)"

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Must be unique per studio:
# if two scripts share a tag, each one's cleanup pass deletes the other's
# events.
SOURCE_TAG = "capital-fitness-yoga-sync"


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


def _at(day, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=TZ)


def is_yoga(name):
    low = name.lower()
    if EXCLUDE_TAG_SUBSTRING in low:
        return False
    return not any(bad in low for bad in EXCLUDE_NAME_SUBSTRINGS)


# ------------------------------------------------------------- generate

def generate(start, end):
    """Expand the static weekly grid into dated classes inside [start, end),
    repeating it for every week the window spans."""
    first_monday = (start - dt.timedelta(days=start.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    out = []

    monday = first_monday
    while monday < end:
        for weekday, begins_at, ends_at, name in WEEKLY_GRID:
            if not is_yoga(name):
                continue

            day = (monday + dt.timedelta(days=weekday)).date()
            begins = _at(day, begins_at)
            ends = _at(day, ends_at)
            if not (start <= begins < end):
                continue

            out.append(
                {
                    "name": name,
                    "start": begins,
                    "end": ends,
                    # Dated, so it stays unique across repeated weeks.
                    "class_id": f"{day.isoformat()}-{begins_at.replace(':', '')}",
                }
            )
        monday += dt.timedelta(days=7)

    return out


# ------------------------------------------------------- event building

def to_event(c):
    lines = [
        "Instructor and cancellation info not published in this source.",
        f"Book / confirm: {BOOKING_PAGE}",
        "",
        "From the studio's published weekly schedule, transcribed by hand, "
        f"not a live feed. Confirm at {BOOKING_PAGE} before you go.",
    ]

    return {
        "summary": f"{STUDIO} - {c['name']}",
        "location": ADDRESS,
        "description": "\n".join(lines),
        "start": {"dateTime": c["start"].isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": c["end"].isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
        "source": {"url": BOOKING_PAGE, "title": "Yoga Sangha schedule"},
        "extendedProperties": {
            "private": {
                "source": SOURCE_TAG,
                "classId": c["class_id"],
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

    classes = generate(start, end)
    classes.sort(key=lambda c: c["start"])
    print(f"Generated {len(classes)} classes for the window")

    if not classes:
        print("Nothing matched. Not touching the calendar.")
        return

    svc = calendar_service()
    print(f"Removed {clear_window(svc, start, end)} previously synced events")

    for c in classes:
        ev = to_event(c)
        execute_with_backoff(svc.events().insert(calendarId=CALENDAR_ID, body=ev))
        print(f"  {c['start']:%a %m/%d %I:%M %p}  {ev['summary']}")

    print(f"Wrote {len(classes)} events")


if __name__ == "__main__":
    main()
