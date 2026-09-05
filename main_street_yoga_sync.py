"""
Main Street Yoga Center -> Google Calendar

Pulls the week ahead of yoga classes from WellnessLiving and writes them into
the shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

WHICH ENDPOINT THIS USES, AND WHY NOT THE OTHER ONE
---------------------------------------------------
mainstreetyogacenter.org embeds a WellnessLiving widget. That widget's own
data call is

    GET /en-frame/Wl/Schedule/Schedule.json?...&s_data=<opaque>&csrf=<token>

and it is a dead end for a plain HTTP client. csrf is scrapeable, but s_data
is an encrypted blob the widget's JavaScript builds client side, with a fresh
value per week and no stable prefix between weeks, so there is no way to ask
it for an arbitrary date without running their JS. That would have meant a
headless browser.

It doesn't, because WellnessLiving also runs its consumer "Explore" site on a
plain public REST API with no auth, no csrf and no cookies:

    GET https://wl-explorer-be-prod.www.wellnessliving.com/api/course/schedule
        ?wLocationId=282709&wBookNowTabId=-4119511&dateMin=...&dateMax=...

That is what this script uses. wLocationId 282709 is Main Street's location id
(confirmed via Wl/Location/List.json, which also gave the street address).

*** ONE REAL BUG IN THEIR API, HANDLED BELOW ***
/api/course/schedule projects EVERY class weekly, even ones that recur
annually. Two of Main Street's holiday classes, "Giving Thanks Flow"
(Thanksgiving) and "Recovery Day Flow" (the Friday after), therefore come back
on every single Thursday and Friday of the year. The studio's own widget does
not show them.

The fix is `real_occurrence` below: each course also ships a `periods` list
describing its actual recurrence, and the phantoms are the ones whose only
matching period is open ended (localDateEnd "0000-00-00") with
repeatPeriod != 7, i.e. not a weekly rule. One-off dated classes like
"108 Sun Salutations" have a period bounded to a single date and survive.

Verified 2026-09-05: for the week of 2026-09-14 the raw API returned 35
sessions and this filter kept 33, matching the studio's own widget exactly,
day by day (6/5/5/4/3/5/5). Known cost of the filter: on the actual
Thanksgiving week it will also skip the real "Giving Thanks Flow". That is one
missed class a year versus roughly a hundred phantoms, so it is the right
trade, but add "giving thanks" handling by hand if you care in November.

Reads credentials from a .env file in the working directory (or any parent):
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
    YOGA_CALENDAR_ID

Requires:
    pip install requests python-dotenv google-auth google-api-python-client
"""

import datetime as dt
import html
import os
import re
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------- config

API_URL = "https://wl-explorer-be-prod.www.wellnessliving.com/api/course/schedule"
LOCATION_ID = "282709"
BOOK_NOW_TAB_ID = "-4119511"
BOOKING_PAGE = (
    "https://www.wellnessliving.com/explore/locations/yoga/us-madison/"
    "mainstreetyogacenter-lhiowz/"
)

STUDIO = "Main Street Yoga"
# One location, so the title omits it.
ADDRESS = "1882 E Main Street, Madison, WI 53704"

TZ = ZoneInfo("America/Chicago")

# WellnessLiving exposes quickSearchTags on each course, which would have been
# the structured discipline signal, but Main Street leaves it empty on every
# single course. There is no other subject field: s_class_type / k_class_type
# are null too. So this is name matching, and per the brief it is an allowlist.
# Case-insensitive substring match on the course title.
#
# Across 2026-09-07 to 2026-11-30 this keeps:
#   108 Sun Salutations, Ashtanga Flow, Beginner's Yoga ( Drop In ),
#   Candlelight Yin, Community Class: Flow, Community Class: Strong Fun & Flow,
#   Continuing Beginner's Yoga ( Drop In ), Evening Restorative, Flow Yoga,
#   Free Community Class: Flow Yoga, Labor Day Flow, Morning Flow,
#   Prana Vinyasa Flow, and the four rotating Thursday Night Specials.
# and drops:
#   Authentic Relating Games, Beginners Circling Intro, Meditation,
#   Monthly Circling Lab, Community Class: Meditation (Mantra & Pranayama).
YOGA_NAME_ALLOWLIST = (
    "yoga",
    "flow",
    "yin",
    "restorative",
    "vinyasa",
    "hatha",
    "ashtanga",
    "nidra",
    "sun salutations",
    "somatic",
    "thursday night special",  # their rotating weekly yoga slot
)

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Must be unique per studio:
# if two scripts share a tag, each one's cleanup pass deletes the other's
# events.
SOURCE_TAG = "main-street-yoga-sync"

HEADERS = {
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

OPEN_ENDED = ("", "0000-00-00", None)
WEEKLY = 7  # repeatPeriod value that means "every week"


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


def parse_local(value):
    """localDateTimeStart looks like "2026-09-16 17:15:00", already in the
    studio's own timezone."""
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


# --------------------------------------------------------------- fetch

def real_occurrence(course, session):
    """True if this projected session matches one of the course's real
    recurrence rules. See the module docstring: without this check the API
    hands back annual holiday classes as if they ran every week.

    WellnessLiving's `weekday` is ISO: Monday 1 through Sunday 7.
    """
    stamp = session.get("localDateTimeStart") or ""
    if " " not in stamp:
        return False
    date_part, time_part = stamp.split(" ", 1)
    weekday = parse_local(stamp).isoweekday()

    for period in course.get("periods") or []:
        if period.get("localTimeStart") != time_part:
            continue
        if str(period.get("weekday")) != str(weekday):
            continue
        if (period.get("localDateStart") or "") > date_part:
            continue
        ends = period.get("localDateEnd")
        open_ended = ends in OPEN_ENDED
        if not open_ended and ends < date_part:
            continue
        # A weekly rule is always fine. A non-weekly rule is only trustworthy
        # when it is bounded to a real date range; open ended non-weekly rules
        # are the ones the API mis-projects.
        if period.get("repeatPeriod") == WEEKLY or not open_ended:
            return True
    return False


def fetch_sessions(start, end):
    """One request covers the whole window: the API takes explicit UTC
    dateMin/dateMax, so there is no source-week alignment to work around.

    Returns flat (course, session) pairs.
    """
    params = {
        "wLocationId": LOCATION_ID,
        "wBookNowTabId": BOOK_NOW_TAB_ID,
        "dateMin": start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "dateMax": end.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z"),
    }
    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    pairs = []
    for course in body.get("courses") or []:
        for session in course.get("sessions") or []:
            pairs.append((course, session))
    return pairs


def is_yoga(title):
    low = (title or "").lower()
    return any(word in low for word in YOGA_NAME_ALLOWLIST)


def keep(pair, start, end):
    course, session = pair
    if session.get("isCancelled"):
        return False
    # isVirtual sits on both the course and the individual session. Check both;
    # a normally in-person class can be moved online for one week.
    if session.get("isVirtual") or course.get("isVirtual"):
        return False
    if not real_occurrence(course, session):
        return False
    if not is_yoga(course.get("title")):
        return False
    begins = parse_local(session["localDateTimeStart"])
    return start <= begins < end


# ------------------------------------------------------- event building

def to_event(pair):
    course, session = pair
    name = (course.get("title") or "Class").strip()

    staff = session.get("staffMembers") or []
    instructor = ", ".join(
        (m.get("fullName") or f"{m.get('firstName', '')} {m.get('lastName', '')}").strip()
        for m in staff
    ).strip(", ")

    begins = parse_local(session["localDateTimeStart"])
    minutes = session.get("durationMinutes") or 60
    ends = begins + dt.timedelta(minutes=minutes)

    spots_left = None
    total = session.get("bookingSlotsTotal")
    used = session.get("bookingSlotsUsed")
    if total is not None and used is not None:
        spots_left = max(int(total) - int(used), 0)

    lines = []
    if instructor:
        lines.append(f"Instructor: {instructor}")
    if spots_left is not None:
        lines.append(f"Spots open at sync time: {spots_left} of {total}")
    lines.append(f"Book: {BOOKING_PAGE}")

    # description is a whole HTML document, doctype and all.
    description = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ",
                         course.get("description") or "")
    description = re.sub(r"<[^>]+>", " ", description)
    description = html.unescape(description)
    description = re.sub(r"\s+", " ", description).strip()
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
        "extendedProperties": {
            "private": {
                "source": SOURCE_TAG,
                # `id` is the session instance; kClassPeriod is the recurring
                # slot and repeats week to week, so it is not unique here.
                "classId": str(session.get("id")),
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

    raw = fetch_sessions(start, end)
    classes = [p for p in raw if keep(p, start, end)]
    classes.sort(key=lambda p: p[1]["localDateTimeStart"])
    print(f"Fetched {len(raw)} sessions, kept {len(classes)}")

    if not classes:
        print("Nothing matched. Not touching the calendar.")
        return

    svc = calendar_service()
    print(f"Removed {clear_window(svc, start, end)} previously synced events")

    for pair in classes:
        ev = to_event(pair)
        svc.events().insert(calendarId=CALENDAR_ID, body=ev).execute()
        begins = parse_local(pair[1]["localDateTimeStart"])
        print(f"  {begins:%a %m/%d %I:%M %p}  {ev['summary']}")

    print(f"Wrote {len(classes)} events")


if __name__ == "__main__":
    main()
