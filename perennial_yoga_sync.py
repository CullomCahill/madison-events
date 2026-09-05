"""
Perennial Yoga (Madison) -> Google Calendar

Pulls the week ahead of yoga classes from Perennial's Mindbody "go" schedule
widget and writes them into the shared "Yoga" Google Calendar.

Run this Monday morning. It writes from max(this Monday 00:00, right now)
through the following Monday 00:00, local America/Chicago, so a mid-week or
weekend re-run never writes classes that already happened.

Idempotent: every event it creates is stamped with a private extended
property. On each run it deletes anything carrying that stamp inside the
target window before writing fresh, so re-running never duplicates.

HOW THIS ONE WORKS, AND WHY IT IS THE FRAGILE ONE
-------------------------------------------------
Perennial Madison is on Mindbody's newer platform (studio id 5754596). It has
no classic clients.mindbodyonline.com schedule page: that URL returns a
Mindbody business sign-in. The only public surface is the widget iframe

    https://go.mindbodyonline.com/book/widgets/schedules/view/82543892fb7/schedule

which is a Next.js app. The schedule is NOT in the server-rendered HTML, and
there is no REST endpoint behind it. The widget fetches classes by calling a
Next.js **server action**: an HTTP POST back to the same URL carrying a
Next-Action header and a multipart body.

The two values that call needs are both present in the page's RSC flight
payload on every load, so this script scrapes them fresh each run:

    "fetchClasses":"$h21"                                     -> flight row 21
    21:{"id":"<40 hex action id>","bound":"$@22"}             -> action id
    22:["$@39"]                                               -> bound-args row
    39:"<~208 char encrypted blob>"                           -> the blob

Then it POSTs:

    Next-Action: <action id>
    multipart body, field "1" = the JSON-quoted blob
                    field "0" = ["$@1", {"fromDate": ..., "toDate": ...}]

and parses the RSC flight response, where row "1" is the array of classes.

That means: this script breaks if Mindbody changes the widget's internals,
and the failure will look like "could not find the fetchClasses action".
It does not break on a Mindbody redeploy alone, because nothing is hardcoded
except the widget id. Verified against live data 2026-09-05: 32 classes
returned for 2026-09-07 through 2026-09-14.

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
import json
import os
import re
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ---------------------------------------------------------------- config

WIDGET_ID = "82543892fb7"
WIDGET_URL = (
    f"https://go.mindbodyonline.com/book/widgets/schedules/view/{WIDGET_ID}/schedule"
)

STUDIO = "Perennial"
# The widget is scoped to a single location, so every record comes back with
# location.name == "Perennial Yoga - Madison". Perennial also runs Fitchburg,
# on a completely separate Mindbody site, so the title still names the
# location to keep the two apart on the calendar.
LOCATION_LABEL = "Madison"
EXPECTED_LOCATION = "Perennial Yoga - Madison"
ADDRESS = "3241 Garver Green, Ste 250, Madison, WI 53704"

TZ = ZoneInfo("America/Chicago")

# There is no clean structured yoga tag here. Every class carries a
# sessionTypeId, but the session types that map to yoga flows and the one
# that maps to the sound bath sit in the SAME serviceGroupId, so the group is
# useless as a filter. What IS structured and useful is `type`, which is
# "Class" for drop-ins and "Enrollment" for multi-week series.
KEEP_TYPES = {"Class"}

# ...and on top of that, name matching, which per the brief is an allowlist.
# A class is kept if its name contains "YOGA" or starts with one of these.
# Over the verified week the full set of names was:
#     FLOW | REJUVENATE                              -> kept (FLOW)
#     FLOW | RADIATE                                 -> kept (FLOW)
#     FLOW | GENTLE                                  -> kept (FLOW)
#     GROUND | YIN                                   -> kept (GROUND)
#     MADISON | $5 COMMUNITY YOGA | FLOW REJUVENATE  -> kept (contains YOGA)
#     SOUND | VIBRATION                              -> DROPPED (sound bath)
#     YOGA NIDRAHHH FALL YOGA SERIES                 -> DROPPED (Enrollment)
NAME_PREFIX_ALLOWLIST = ("FLOW", "GROUND")

CALENDAR_ID = os.environ["YOGA_CALENDAR_ID"]

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Stamp used to find and clean up our own events. Must be unique per studio:
# if two scripts share a tag, each one's cleanup pass deletes the other's
# events.
SOURCE_TAG = "perennial-yoga-sync"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
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


# ----------------------------------------------- Next.js server action glue

class WidgetChanged(RuntimeError):
    """Raised when the widget page no longer looks the way we expect."""


def _flight_row(flat, row_id):
    """Return the raw text of one RSC flight row, or None.

    Rows are separated by a literal backslash-n inside the page's inline
    script strings, and the first row on a chunk has no separator in front of
    it, so accept either.
    """
    for pattern in (r"\\n" + row_id + r":", r"(?<![0-9a-f])" + row_id + r":"):
        m = re.search(pattern, flat)
        if m:
            return flat[m.end():]
    return None


def scrape_action(session):
    """Fetch the widget page and pull out the fetchClasses action id and its
    encrypted bound-args blob."""
    resp = session.get(
        WIDGET_URL,
        headers={"user-agent": USER_AGENT, "accept": "text/html"},
        timeout=30,
    )
    resp.raise_for_status()
    # The flight payload lives inside JS string literals, so quotes arrive
    # escaped. Unescape once and work on the flat text.
    flat = resp.text.replace('\\"', '"')

    m = re.search(r'"fetchClasses":"\$h([0-9a-f]+)"', flat)
    if not m:
        raise WidgetChanged("could not find the fetchClasses action reference")
    action_row = m.group(1)

    tail = _flight_row(flat, action_row)
    if tail is None:
        raise WidgetChanged(f"flight row {action_row} not found")
    m = re.match(r'\{"id":"([0-9a-f]{40})","bound":"\$@([0-9a-f]+)"\}', tail)
    if not m:
        raise WidgetChanged(f"flight row {action_row} is not an action reference")
    action_id, bound_row = m.group(1), m.group(2)

    tail = _flight_row(flat, bound_row)
    if tail is None:
        raise WidgetChanged(f"bound-args row {bound_row} not found")
    m = re.match(r'\["\$@([0-9a-f]+)"\]', tail)
    if not m:
        raise WidgetChanged(f"bound-args row {bound_row} has an unexpected shape")
    blob_row = m.group(1)

    tail = _flight_row(flat, blob_row)
    if tail is None:
        raise WidgetChanged(f"blob row {blob_row} not found")
    m = re.match(r'"([^"]+)"', tail)
    if not m:
        raise WidgetChanged(f"blob row {blob_row} is not a string")

    return action_id, m.group(1)


def parse_flight(raw):
    """Parse an RSC flight response into {row_id: value}.

    Rows are either
        <id>:<json>\n
    or
        <id>:T<hex byte length>,<that many bytes of text>
    and the text rows contain real newlines, so this walks bytes rather than
    splitting on lines.
    """
    rows = {}
    i, n = 0, len(raw)
    while i < n:
        colon = raw.find(b":", i)
        if colon == -1:
            break
        row_id = raw[i:colon].decode("utf-8", "replace")
        if not re.fullmatch(r"[0-9a-f]+", row_id):
            # Lost sync; skip to the next newline and try again.
            nl = raw.find(b"\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        j = colon + 1
        if raw[j:j + 1] == b"T":
            comma = raw.find(b",", j)
            length = int(raw[j + 1:comma], 16)
            body = raw[comma + 1:comma + 1 + length]
            rows[row_id] = body.decode("utf-8", "replace")
            i = comma + 1 + length
            if raw[i:i + 1] == b"\n":
                i += 1
        else:
            end = raw.find(b"\n", j)
            if end == -1:
                end = n
            chunk = raw[j:end].decode("utf-8", "replace")
            try:
                rows[row_id] = json.loads(chunk)
            except json.JSONDecodeError:
                rows[row_id] = chunk
            i = end + 1
    return rows


def resolve(value, rows, depth=0):
    """Expand RSC references ($2, $@39) against the parsed rows.

    A leading "$$" is RSC's escape for a literal "$" and "$D" prefixes a date
    string; everything else starting with "$" is a row reference.
    """
    if depth > 24:
        return value
    if isinstance(value, str):
        if value.startswith("$$"):
            return value[1:]
        if value.startswith("$D"):
            return value[2:]
        if len(value) > 1 and value[0] == "$":
            key = value[1:]
            if key.startswith("@"):
                key = key[1:]
            if key in rows:
                return resolve(rows[key], rows, depth + 1)
        return value
    if isinstance(value, list):
        return [resolve(v, rows, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, rows, depth + 1) for k, v in value.items()}
    return value


# --------------------------------------------------------------- fetch

def fetch_classes(start, end):
    """One POST covers the whole window: the action takes an explicit UTC
    fromDate/toDate, so there is no source-week alignment to work around."""
    session = requests.Session()
    action_id, blob = scrape_action(session)

    payload = {
        "fromDate": start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "toDate": end.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z"),
    }
    # Field order matters to Next.js: the bound blob ("1") is referenced by
    # the args array ("0") as "$@1", so send them in that order.
    files = [
        ("1", (None, json.dumps(blob))),
        ("0", (None, json.dumps(["$@1", payload]))),
    ]

    resp = session.post(
        WIDGET_URL,
        headers={
            "Next-Action": action_id,
            "user-agent": USER_AGENT,
            "referer": WIDGET_URL,
            "accept": "text/x-component",
        },
        files=files,
        timeout=30,
    )
    resp.raise_for_status()

    rows = parse_flight(resp.content)
    if "1" not in rows:
        raise WidgetChanged("server action response had no result row")
    classes = resolve(rows["1"], rows)
    if not isinstance(classes, list):
        raise WidgetChanged("server action result was not a list of classes")
    return classes


def is_yoga(name):
    n = (name or "").upper()
    if "YOGA" in n:
        return True
    return n.startswith(NAME_PREFIX_ALLOWLIST)


def parse_utc(value):
    """startDateTime looks like 2026-09-07T14:00:00.0000000Z: seven decimal
    places, which fromisoformat will not take. Trim to microseconds."""
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return dt.datetime.fromisoformat(value).astimezone(TZ)


def keep(c, start, end):
    if c.get("cancelled"):
        return False
    if c.get("type") not in KEEP_TYPES:
        return False
    # The widget is scoped to one physical location, so anything that is not
    # that location is either an online offering or a data change worth
    # noticing. NOTE: isBookableOnline is about online BOOKING, not an online
    # class. Do not use it here.
    loc = (c.get("location") or {}).get("name")
    if loc != EXPECTED_LOCATION:
        return False
    if not is_yoga(c.get("name")):
        return False
    begins = parse_utc(c["startDateTime"])
    return start <= begins < end


# ------------------------------------------------------- event building

def to_event(c):
    name = c.get("name") or "Class"

    staff = c.get("staff") or []
    instructor = ", ".join(
        s.get("displayLabel", "") for s in staff if s.get("displayLabel")
    )

    begins = parse_utc(c["startDateTime"])
    ends = parse_utc(c["endDateTime"])

    spots_left = None
    if c.get("capacity") is not None and c.get("numberRegistered") is not None:
        spots_left = max(c["capacity"] - c["numberRegistered"], 0)

    lines = []
    if instructor:
        lines.append(f"Instructor: {instructor}")
    if c.get("roomName"):
        lines.append(f"Room: {c['roomName']}")
    if spots_left is not None:
        lines.append(f"Spots open at sync time: {spots_left} of {c['capacity']}")
    lines.append(f"Book: {WIDGET_URL}")

    description = re.sub(r"<[^>]+>", " ", c.get("description") or "")
    description = html.unescape(description)
    description = re.sub(r"[ \t\xa0]+", " ", description).strip()
    if description:
        lines.append("")
        lines.append(description)

    return {
        "summary": f"{STUDIO} {LOCATION_LABEL} - {name}",
        "location": ADDRESS,
        "description": "\n".join(lines),
        "start": {"dateTime": begins.isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": ends.isoformat(), "timeZone": "America/Chicago"},
        "transparency": "transparent",  # shows as Free, not Busy
        "reminders": {"useDefault": False, "overrides": []},
        "source": {"url": WIDGET_URL, "title": "Perennial Madison booking"},
        "extendedProperties": {
            "private": {
                "source": SOURCE_TAG,
                "classId": str(c["id"]),
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

    raw = fetch_classes(start, end)
    classes = [c for c in raw if keep(c, start, end)]
    classes.sort(key=lambda c: c["startDateTime"])
    print(f"Fetched {len(raw)} classes, kept {len(classes)}")

    if not classes:
        print("Nothing matched. Not touching the calendar.")
        return

    svc = calendar_service()
    print(f"Removed {clear_window(svc, start, end)} previously synced events")

    for c in classes:
        ev = to_event(c)
        svc.events().insert(calendarId=CALENDAR_ID, body=ev).execute()
        print(f"  {parse_utc(c['startDateTime']):%a %m/%d %I:%M %p}  {ev['summary']}")

    print(f"Wrote {len(classes)} events")


if __name__ == "__main__":
    main()
