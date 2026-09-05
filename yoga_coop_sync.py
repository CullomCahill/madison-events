"""
Yoga Co-op of Madison -> Google Calendar

Generates the week ahead from a hardcoded weekly grid and writes it into the
shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

TIER 3, ON PURPOSE
------------------
The Co-op publishes one PDF per term and nothing else. There is no booking
platform behind the site, no JSON, no HTML schedule to parse: the schedule
IS the PDF. Writing a PDF parser for a single-page image-like layout that
changes three times a year would be more fragile than typing it out, so the
grid below is transcribed from

    https://cdn.yogacoop.com/wp-content/uploads/2026/08/2026-SEPT-DEC-schedule.pdf

read on 2026-09-05.

*** MAINTENANCE: this needs a manual refresh roughly every four months. ***
The Co-op publishes Jan-Apr, May-Aug, and Sept-Dec terms. When the new PDF
goes up, retype DROP_IN / SERIES and move VALID_THROUGH. The script prints a
loud warning once the run date passes VALID_THROUGH so it can't quietly go
stale on you.

There is nothing to filter here: every class the Co-op runs is yoga, none of
them are cancelled in the source (a cancellation just won't be visible to
this script at all), and the Studio/Zoom classes are hybrid rather than
online-only, so they stay in as in-person.

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
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------- config

STUDIO = "Yoga Co-op"
ADDRESS = "812 E Dayton St, Madison, WI 53703"
BOOKING_PAGE = "https://yogacoop.com/book-a-class"

TZ = ZoneInfo("America/Chicago")

# Last day the transcription below is known good. From the PDF header:
# "SEPTEMBER-DECEMBER 2026 SCHEDULE".
VALID_THROUGH = dt.date(2026, 12, 31)
SOURCE_PDF = (
    "https://cdn.yogacoop.com/wp-content/uploads/2026/08/2026-SEPT-DEC-schedule.pdf"
)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# Weekly drop-in classes, transcribed verbatim from the DROP-IN CLASSES block.
# The PDF's middle column is a level, not a class name, so it is used as the
# class name here; that is what the Co-op calls its classes.
#     (weekday, start "HH:MM", end "HH:MM", name, teacher, hybrid?)
DROP_IN = [
    (SUN, "09:00", "10:30", "Level Two",  "Katie",  False),
    (SUN, "10:45", "12:00", "Level One",  "Katie",  False),
    (MON, "17:30", "19:00", "All Levels", "Karan",  True),
    (TUE, "10:00", "11:30", "All Levels", "Katie",  False),
    (TUE, "17:30", "19:00", "Level Two",  "Faith",  True),
    (WED, "17:00", "18:30", "All Levels", "Katie",  False),
    (WED, "19:00", "20:30", "Level One",  "Sophie", False),
    (THU, "11:00", "12:15", "Level One",  "Sophie", False),
    (SAT, "09:00", "10:30", "Level One",  "Lisa",   True),
]

# Registration series, from the SERIES block. Each runs only inside its own
# date ranges, inclusive.
#     (weekday, start, end, name, teacher, hybrid?, [(from, to), ...])
SERIES = [
    (MON, "09:00", "10:15", "Ageless Intro", "Faith", False,
     [(dt.date(2026, 8, 31), dt.date(2026, 9, 28)),
      (dt.date(2026, 10, 5), dt.date(2026, 11, 9))]),
    (WED, "09:00", "10:15", "Ageless Cont", "Faith", False,
     [(dt.date(2026, 9, 2), dt.date(2026, 9, 23)),
      (dt.date(2026, 10, 7), dt.date(2026, 11, 11))]),
    (THU, "17:30", "18:30", "Beginners", "Paul", False,
     [(dt.date(2026, 9, 3), dt.date(2026, 9, 24))]),
]

# Series are multi-week registration courses, not drop-ins, so you may not
# want them cluttering a browse-and-pick calendar. Flip to False to skip them.
# When True they get a "(series)" suffix so they read differently at a glance.
INCLUDE_SERIES = True

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Must be unique per studio:
# if two scripts share a tag, each one's cleanup pass deletes the other's
# events.
SOURCE_TAG = "yoga-coop-sync"


# ------------------------------------------------------------- time math

def week_window(now=None):
    """Return (start, end) for the current Monday-to-Monday week, with the
    start clamped to now.

    end is always the following Monday 00:00 local. start is whichever is
    later of this week's Monday 00:00 and the current moment, so a Wednesday
    or Saturday re-run only ever writes what is still ahead.
    """
    now = now or dt.datetime.now(TZ)
    monday = (now - dt.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(monday, now), monday + dt.timedelta(days=7)


def _at(day, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=TZ)


# ------------------------------------------------------------- generate

def generate(start, end):
    """Expand the static grid into dated classes inside [start, end)."""
    monday = end - dt.timedelta(days=7)
    out = []

    entries = [(e, None) for e in DROP_IN]
    if INCLUDE_SERIES:
        entries += [(e[:6], e[6]) for e in SERIES]

    for (weekday, begins_at, ends_at, name, teacher, hybrid), ranges in entries:
        day = (monday + dt.timedelta(days=weekday)).date()

        if ranges is not None and not any(a <= day <= b for a, b in ranges):
            continue

        begins = _at(day, begins_at)
        ends = _at(day, ends_at)
        if not (start <= begins < end):
            continue

        out.append(
            {
                "name": name + (" (series)" if ranges is not None else ""),
                "teacher": teacher,
                "hybrid": hybrid,
                "start": begins,
                "end": ends,
                # Stable within a week, which is all the cleanup pass needs.
                "class_id": f"{day.isoformat()}-{begins_at.replace(':', '')}",
            }
        )

    return out


# ------------------------------------------------------- event building

def to_event(c):
    lines = [f"Instructor: {c['teacher']}"]
    lines.append("Format: in studio + Zoom" if c["hybrid"] else "Format: in studio")
    lines.append(f"Book: {BOOKING_PAGE}")
    lines.append("")
    lines.append(
        "From the Co-op's printed term schedule, not a live feed. "
        f"Confirm at {BOOKING_PAGE} before you go."
    )

    return {
        "summary": f"{STUDIO} - {c['name']}",
        "location": ADDRESS,
        "description": "\n".join(lines),
        "start": {"dateTime": c["start"].isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": c["end"].isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
        "source": {"url": BOOKING_PAGE, "title": "Yoga Co-op booking"},
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


def clear_window(svc, start, end):
    """Delete previously synced events in the window. Only touches events
    carrying our own stamp, so anything you added by hand is safe.

    Note this uses the clamped start, so classes that already happened this
    week are left alone rather than being deleted and not rewritten.
    """
    removed = 0
    page = None
    while True:
        resp = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            privateExtendedProperty=f"source={SOURCE_TAG}",
            singleEvents=True,
            maxResults=250,
            pageToken=page,
        ).execute()
        for ev in resp.get("items", []):
            svc.events().delete(calendarId=CALENDAR_ID, eventId=ev["id"]).execute()
            removed += 1
        page = resp.get("nextPageToken")
        if not page:
            break
    return removed


def main():
    start, end = week_window()
    print(f"Window: {start:%a %b %d %I:%M %p} through {end:%a %b %d}")

    if end.date() > VALID_THROUGH:
        print()
        print("  !! The hardcoded Yoga Co-op grid expired on "
              f"{VALID_THROUGH:%b %d, %Y}.")
        print(f"  !! Get the new term PDF and update this file: {SOURCE_PDF}")
        print("  !! Writing the old grid anyway; times are probably wrong.")
        print()

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
        svc.events().insert(calendarId=CALENDAR_ID, body=ev).execute()
        print(f"  {c['start']:%a %m/%d %I:%M %p}  {ev['summary']}")

    print(f"Wrote {len(classes)} events")


if __name__ == "__main__":
    main()
