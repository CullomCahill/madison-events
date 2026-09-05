"""
Capital Fitness / Yoga Sangha -> Google Calendar

Pulls the week ahead of yoga classes from the studio's own website and writes
them into the shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

WHY THIS GOES THROUGH THE WEBSITE, NOT MINDBODY
------------------------------------------------
This studio's Mindbody site (studioid 1956) sits behind Cloudflare bot
management (`clients.mindbodyonline.com/classic/ws` returns a 403 "Security
Check" page with a `__cf_bm` challenge cookie even with full browser-style
headers). That is a JS challenge, not a missing-header problem, and it is not
solvable with a plain HTTP client -- it would need a real browser.

capitalfitness.net/yoga-sangha carries its own weekly schedule text directly
in the page, server-rendered (confirmed: it's present in the raw HTML with no
JavaScript execution, and the page itself has no bot protection). It is a
flat Monday-through-Sunday grid with no date attached, no instructor field,
and no live cancellation data -- the page says as much: "Schedule is subject
to change. Please check Mindbody below for the most up to date schedule." So
this is a step down in freshness from a live booking API, but it is a live
scrape of the studio's own source of truth rather than a hardcoded transcript,
so it updates automatically whenever they edit the page -- no manual refresh
needed, unlike the Yoga Co-op script.

HOW THE PAGE IS STRUCTURED
---------------------------
It's a Wix site. Each day is an `<h6>` heading containing just the day name
("Monday", "Tuesday", ...), followed by a sibling block holding a `<ul>` of
`<li>` entries, each rendering (once you strip the styling spans) as:

    6:30am - 7:30am - Pilates Flow

So the parser walks the document in order via `find_all(['h6', 'li'])`,
tracks the current day as h6 headings with day names are encountered, and
attaches each subsequent li's parsed text to that day. This is where the
"(Group Fitness)" tag doubles as a real structured signal:  entries like
"CAPFIT HIIT (Group Fitness)", "RUN CLUB (Group Fitness)", "ZUMBA (Group
Fitness)" carry that literal suffix in the source, so they're excluded on
that alone rather than by guessing at names. Verified against the live page
2026-09-05: 38 li entries fall inside the seven day sections (6/8/6/6/5/7/0
Mon-Sun), all of them match the time-range regex, and there were zero strays
from other parts of the page (17 stray `<li>` elements exist elsewhere on the
page -- nav menu items and a separate promotional snippet -- but all of them
sit before the first "Monday" heading in document order, so the day-tracking
state machine never picks them up).

Reads credentials from a .env file in the working directory (or any parent):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
    YOGA_CALENDAR_ID

Requires:
    pip install requests beautifulsoup4 python-dotenv google-auth google-api-python-client
"""

import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------- config

PAGE_URL = "https://www.capitalfitness.net/yoga-sangha"

STUDIO = "Yoga Sangha"
ADDRESS = "15 N. Butler St., Madison, WI 53703"

TZ = ZoneInfo("America/Chicago")

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_INDEX = {name: i for i, name in enumerate(DAY_NAMES)}

# The page has no discipline tag at all: it is one undifferentiated list of
# "fitness classes" per day. "(Group Fitness)" is a literal suffix the studio
# puts on the non-yoga classes in that same list (CAPFIT HIIT, RUN CLUB,
# ZUMBA, BOOTCAMP, PUMP IT UP, TRX CIRCUIT), so that's a real structured
# signal, not a guess. On top of that, a denylist for the two other
# non-yoga-but-still-in-the-list categories, same as the old Mindbody version
# of this script: Pilates and any guided meditation session.
EXCLUDE_NAME_SUBSTRINGS = ("pilates", "guided meditation")
EXCLUDE_TAG_SUBSTRING = "(group fitness)"

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Must be unique per studio:
# if two scripts share a tag, each one's cleanup pass deletes the other's
# events.
SOURCE_TAG = "capital-fitness-yoga-sync"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml",
}

TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[apAP][mM])\s*-\s*(\d{1,2}:\d{2}\s*[apAP][mM])\s*-\s*(.+)"
)


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


def _parse_clock(text):
    """"6:30am" / "7:00pm" -> (hour, minute) in 24h."""
    text = text.strip().lower().replace(" ", "")
    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", text)
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return hour, minute


# --------------------------------------------------------------- fetch

def fetch_page():
    resp = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_grid(html):
    """Walk the page in document order, tracking the current day heading, and
    return a flat list of {day, start_hm, end_hm, name} dicts.

    Day headings are `<h6>` elements whose text is exactly a day name. Class
    rows are `<li>` elements that appear after one, each rendering (after
    stripping styling spans) as "H:MMam - H:MMpm - Class Name". `<li>`
    elements before the first day heading (nav menu, unrelated page content)
    are skipped because no current day is set yet.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []
    current_day = None

    for node in soup.find_all(["h6", "li"]):
        if node.name == "h6":
            text = node.get_text(strip=True)
            if text in WEEKDAY_INDEX:
                current_day = text
            continue

        if current_day is None:
            continue

        text = " ".join(node.get_text(" ", strip=True).split())
        m = TIME_RANGE_RE.match(text)
        if not m:
            continue

        out.append(
            {
                "day": current_day,
                "start_hm": _parse_clock(m.group(1)),
                "end_hm": _parse_clock(m.group(2)),
                "name": m.group(3).strip(" - "),
            }
        )

    return out


def is_yoga(entry):
    name = entry["name"].lower()
    if EXCLUDE_TAG_SUBSTRING in name:
        return False
    return not any(bad in name for bad in EXCLUDE_NAME_SUBSTRINGS)


def dated_classes(grid, start, end):
    """Expand the flat weekly grid into dated classes inside [start, end)."""
    monday = (end - dt.timedelta(days=7)).date()
    out = []
    for entry in grid:
        if not is_yoga(entry):
            continue
        day = monday + dt.timedelta(days=WEEKDAY_INDEX[entry["day"]])
        begins = dt.datetime.combine(day, dt.time(*entry["start_hm"]), tzinfo=TZ)
        ends = dt.datetime.combine(day, dt.time(*entry["end_hm"]), tzinfo=TZ)
        if not (start <= begins < end):
            continue
        out.append(
            {
                "name": entry["name"],
                "start": begins,
                "end": ends,
                "class_id": f"{day.isoformat()}-{entry['start_hm'][0]:02d}{entry['start_hm'][1]:02d}",
            }
        )
    return out


# ------------------------------------------------------- event building

def to_event(c):
    lines = [
        "Instructor and cancellation info not published on this page.",
        f"Book / confirm: {PAGE_URL}",
        "",
        "From the studio's own schedule page, not a live Mindbody feed. "
        "The page itself says the schedule is subject to change.",
    ]

    return {
        "summary": f"{STUDIO} - {c['name']}",
        "location": ADDRESS,
        "description": "\n".join(lines),
        "start": {"dateTime": c["start"].isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": c["end"].isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
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

    grid = parse_grid(fetch_page())
    if not grid:
        raise SystemExit(
            "Parsed zero class rows from the page. The page layout probably "
            "changed -- check PAGE_URL by hand before trusting this run."
        )

    classes = dated_classes(grid, start, end)
    classes.sort(key=lambda c: c["start"])
    print(f"Parsed {len(grid)} rows from the page, kept {len(classes)}")

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
