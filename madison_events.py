import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials

import calendar_writer
import extract
import gmail_reader

STATE_PATH = Path(__file__).parent / "state.json"
LOOKBACK_DAYS = 30
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def build_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def load_state() -> dict:
    if not STATE_PATH.exists():
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        return {"last_run": since.isoformat(), "fingerprints": []}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def fingerprint(title: str, start_datetime: str) -> str:
    date_part = start_datetime[:10]
    return f"{title.strip().lower()} {date_part}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()

    state = load_state()
    since_dt = datetime.fromisoformat(state["last_run"])
    run_started_at = datetime.now(timezone.utc)

    creds = build_credentials()

    emails = gmail_reader.fetch_emails(creds, since_dt)
    print(f"madison_events.py: scanned {len(emails)} emails since {since_dt.isoformat()}")

    if not emails:
        if not args.dry_run:
            state["last_run"] = run_started_at.isoformat()
            save_state(state)
        print("madison_events.py: no emails to process, exiting")
        return

    today = run_started_at.date()
    events = extract.extract_events(emails, today)
    print(f"madison_events.py: extracted {len(events)} candidate events")

    cal_service = None
    cal_id = None
    if not args.dry_run:
        cal_service = calendar_writer.get_calendar_service(creds)
        cal_id = calendar_writer.get_or_create_radar_calendar(cal_service)

    written = 0
    skipped = 0
    fingerprints = set(state["fingerprints"])

    for event in events:
        if not event.get("title") or not event.get("start_datetime"):
            skipped += 1
            continue

        fp = fingerprint(event["title"], event["start_datetime"])
        if fp in fingerprints:
            skipped += 1
            continue

        if args.dry_run:
            print(f"[dry-run] would write: {event['title']} @ {event['start_datetime']}")
            continue

        if calendar_writer.check_event_exists(cal_service, cal_id, event["title"], event["start_datetime"]):
            fingerprints.add(fp)
            skipped += 1
            continue

        calendar_writer.write_event(cal_service, cal_id, event)
        fingerprints.add(fp)
        written += 1

    if not args.dry_run:
        state["fingerprints"] = sorted(fingerprints)
        state["last_run"] = run_started_at.isoformat()
        save_state(state)

    print(
        f"madison_events.py: summary - {len(emails)} emails scanned, "
        f"{len(events)} events found, {written} written, {skipped} skipped"
    )


if __name__ == "__main__":
    main()
