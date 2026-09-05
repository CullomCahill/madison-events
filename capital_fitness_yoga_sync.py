"""
Capital Fitness / Yoga Sangha -> Google Calendar

Pulls the week ahead of yoga classes from Capital Fitness's Mindbody schedule
and writes them into the shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

WHY THIS IS TIER 2 AND NOT TIER 3
---------------------------------
capitalfitness.net/yoga-sangha does show a fixed weekly grid in the page, and
the brief flagged it as a possible hardcode. It isn't necessary. The page also
embeds Mindbody site 1956, and that site still has classic branded web turned
on, which means a plain server-rendered HTML week is available:

    GET /classic/ws?studioid=1956&stype=-7&sView=week      (opens the session)
    GET /classic/mainclass?studioid=1956&stype=-7&view=week&date=M/D/YYYY&tg=22

The first request is required. Hitting mainclass cold returns a Mindbody
sign-in page; hitting ws first sets the session cookies and then mainclass
serves the real schedule. Both are plain GETs with a cookie jar, no login.

That gets live instructors and, more importantly, cancellations, which the
static grid on the website does not have.

`tg=22` is Mindbody's own service category, named "Yoga & Pilates Classes"
(the other one is `tg=23`, "Fitness & Cycling"). That is a real structured
signal, so it does the heavy lifting rather than class-name matching.

Verified against live data 2026-09-05: the week of 2026-09-07 returned 45
rows unfiltered, 38 rows under tg=22.

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

STUDIO_ID = "1956"
BASE = "https://clients.mindbodyonline.com/classic"
SESSION_URL = f"{BASE}/ws"
SCHEDULE_URL = f"{BASE}/mainclass"
BOOKING_PAGE = "https://www.capitalfitness.net/yoga-sangha"

STUDIO = "Yoga Sangha"
# Mindbody site 1956 has exactly one location, "Capital Fitness- North Butler",
# so the title omits it.
EXPECTED_LOCATION = "Capital Fitness- North Butler"
ADDRESS = "15 N. Butler St., Madison, WI 53703"

TZ = ZoneInfo("America/Chicago")

# Mindbody service category. 22 = "Yoga & Pilates Classes".
SERVICE_CATEGORY = "22"

# The structured category above bundles Pilates in with yoga, and sweeps in a
# couple of seated meditation offerings. There is no finer structured field to
# split them, so this is a denylist applied ON TOP of the structured filter,
# rather than the allowlist the brief asked for. The reasoning: an allowlist
# here would need to name every yoga class the studio runs (24 distinct names
# in the verified week alone) and would silently drop anything new they add,
# which is the failure mode you least want in a weekly job. Case-insensitive
# substring match on the class name. Edit freely.
#
# In the verified week this dropped: Pilates Flow, Pilates Sculpt,
# Guided Meditation. Left in, deliberately: Yoga Strong, Mobility Flow,
# Foam Roll & Flow, Meditation & Motion, Deep Restore.
EXCLUDE_NAME_SUBSTRINGS = ("pilates", "guided meditation")

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


# --------------------------------------------------------------- fetch

def fetch_week_html(monday):
    """Open a Mindbody session, then pull one Monday-to-Sunday week.

    `view=week` starts the grid on whatever date you pass, so passing Monday
    gives exactly the Monday-to-Sunday block we want. No slicing needed.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # Establishes the cookies. The body is a "you need javascript" shell that
    # auto-submits to mainclass; we skip that and request mainclass directly.
    session.get(
        SESSION_URL,
        params={"studioid": STUDIO_ID, "stype": "-7", "sView": "week"},
        timeout=30,
    ).raise_for_status()

    resp = session.get(
        SCHEDULE_URL,
        params={
            "studioid": STUDIO_ID,
            "stype": "-7",
            "view": "week",
            "date": f"{monday.month}/{monday.day}/{monday.year}",
            "tg": SERVICE_CATEGORY,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def _column_names(soup):
    """Build the column order for the class cells from the table's own header
    row rather than assuming it.

    Mindbody renders each row as
        .col-1  ->  [start time, sign-up button]
        .col-2  ->  [the remaining columns, in header order]
    and which columns exist varies by site: sites with one location sometimes
    drop the Location column entirely. Reading the header keeps the field
    mapping honest instead of guessing by position.
    """
    header = soup.select_one("#classSchedule-header")
    if not header:
        raise RuntimeError("schedule header not found; the page shape changed")
    ids = [d.get("id", "") for d in header.select(".floatingHeader")]
    # Drop the two that live in col-1.
    return [i for i in ids if i not in ("startTimeHeader", "signUpNowHeader")]


TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.I)
# Durations arrive as "1 hour", "45 minutes", or "1 hour & 15 minutes". Match
# the two parts separately: a single combined pattern quietly returns 60 for
# the third form because of the ampersand between the halves.
HOUR_RE = re.compile(r"(\d+)\s*hour", re.I)
MIN_RE = re.compile(r"(\d+)\s*minute", re.I)


def parse_week(html):
    """Walk the schedule table in document order, carrying the current day
    header, and return a list of plain dicts."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#classSchedule-mainTable")
    if not table:
        return []

    columns = _column_names(soup)
    out = []
    current_date = None

    for node in table.find_all("div", recursive=False):
        classes = node.get("class") or []

        if "header" in classes:
            # e.g. "Mon September 7, 2026"
            text = " ".join(node.get_text(" ", strip=True).split())
            m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", text)
            current_date = (
                dt.datetime.strptime(m.group(1), "%B %d, %Y").date() if m else None
            )
            continue

        if "row" not in classes or current_date is None:
            continue

        first = node.select_one(".col-1 .col.col-first")
        if not first:
            continue
        raw_time = first.get_text(" ", strip=True).replace("\xa0", " ")

        cells = [c.get_text(" ", strip=True).replace("\xa0", " ")
                 for c in node.select(".col-2 .col")]
        record = dict(zip(columns, cells))

        m = TIME_RE.search(raw_time)
        if not m:
            continue
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        begins = dt.datetime.combine(
            current_date, dt.time(hour, minute), tzinfo=TZ
        )

        duration_text = record.get("durationHeader", "")
        minutes = 0
        mh = HOUR_RE.search(duration_text)
        mm = MIN_RE.search(duration_text)
        if mh:
            minutes += int(mh.group(1)) * 60
        if mm:
            minutes += int(mm.group(1))
        if minutes == 0:
            minutes = 60  # last resort, so a parse miss is still a sane block

        out.append(
            {
                "name": record.get("classNameHeader", "").strip(),
                "instructor": record.get("trainerNameHeader", "").strip(),
                "location": record.get("locationNameHeader", "").strip(),
                "room": record.get("resourceNameHeader", "").strip(),
                "start": begins,
                "end": begins + dt.timedelta(minutes=minutes),
                # Mindbody strikes through the time and the class name of a
                # cancelled class and replaces the instructor cell with
                # "Cancelled ...". Check both; the <s> tag is the reliable one.
                "cancelled": bool(node.find("s"))
                or record.get("trainerNameHeader", "").lower().startswith("cancel"),
                # From the Sign Up Now handler: ...classId=2302&classDate=...
                "class_id": _class_id(node),
            }
        )

    return out


def _class_id(node):
    """Mindbody's own class id, pulled out of the sign-up button's onclick.
    Falls back to None for cancelled rows, which have no button."""
    button = node.select_one(".col-1 input[onclick]")
    if not button:
        return None
    m = re.search(r"classId=(\d+)", button.get("onclick", ""))
    return m.group(1) if m else None


def is_yoga(name):
    low = (name or "").lower()
    return not any(bad in low for bad in EXCLUDE_NAME_SUBSTRINGS)


def keep(c, start, end):
    if c["cancelled"]:
        return False
    # Single physical location. Anything else would be a new site, a second
    # studio, or a livestream entry, and is worth not writing silently.
    if c["location"] and c["location"] != EXPECTED_LOCATION:
        return False
    if not c["name"]:
        return False
    if not is_yoga(c["name"]):
        return False
    return start <= c["start"] < end


# ------------------------------------------------------- event building

def to_event(c):
    lines = []
    if c["instructor"]:
        lines.append(f"Instructor: {c['instructor']}")
    if c["room"]:
        lines.append(f"Room: {c['room']}")
    # The classic schedule carries no intensity field and no inline class
    # description; descriptions live behind a modal keyed to a separate id.
    lines.append(f"Book: {BOOKING_PAGE}")

    private = {"source": SOURCE_TAG}
    if c["class_id"]:
        private["classId"] = c["class_id"]

    return {
        "summary": f"{STUDIO} - {c['name']}",
        "location": ADDRESS,
        "description": "\n".join(lines),
        "start": {"dateTime": c["start"].isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": c["end"].isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": private},
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
    monday = end - dt.timedelta(days=7)
    print(f"Window: {start:%a %b %d %I:%M %p} through {end:%a %b %d}")

    raw = parse_week(fetch_week_html(monday))
    classes = [c for c in raw if keep(c, start, end)]
    classes.sort(key=lambda c: c["start"])
    print(f"Fetched {len(raw)} classes, kept {len(classes)}")

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
