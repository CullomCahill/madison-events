import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials

import calendar_writer
import extract
import gmail_reader

# Email subjects can contain emoji/unicode that Windows' default console
# codepage (cp1252) can't print; widen stdout so logging never crashes on it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE_PATH = Path(__file__).parent / "state.json"
LOOKBACK_DAYS = 30
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", 0.6))
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


def _normalize_location(location: str | None) -> str:
    tokens = re.findall(r"[a-z0-9]+", (location or "").lower())
    return " ".join(tokens)


def fingerprint(event: dict) -> str:
    """Identity for dedup: date/time/location, NOT the model's title wording
    (which varies run to run for the same real event).
    """
    start = event["start_datetime"]
    location = _normalize_location(event.get("location"))

    if event.get("recurrence"):
        # Anchor on weekday+time, not the specific date — the "next
        # occurrence" date the model picks advances every run, so a
        # date-based fingerprint would treat the same series as new each time.
        dt = calendar_writer.naive_datetime(start) if "T" in start else datetime.combine(
            calendar_writer.date.fromisoformat(start), datetime.min.time()
        )
        return f"recurring {dt.strftime('%a').lower()} {dt.strftime('%H:%M')} {location}"

    if "T" not in start:
        return f"{start} allday {location}"

    return f"{start[:16]} {location}"


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
    candidates, chunk_count = extract.extract_events(emails, today)
    print(f"madison_events.py: extracted {len(candidates)} candidate events from {chunk_count} chunks")

    merged_events = extract.merge_duplicate_events(candidates, today)
    merged_away = len(candidates) - len(merged_events)
    print(f"madison_events.py: merge step consolidated {merged_away} entries -> {len(merged_events)} events")

    cal_service = None
    cal_id = None
    if not args.dry_run:
        cal_service = calendar_writer.get_calendar_service(creds)
        cal_id = calendar_writer.get_or_create_radar_calendar(cal_service)

    written = 0
    skipped_confidence = 0
    skipped_dup = 0
    fingerprints = set(state["fingerprints"])

    for event in merged_events:
        if not event.get("title") or not event.get("start_datetime"):
            skipped_dup += 1
            continue

        confidence = event.get("confidence")
        if confidence is not None and confidence < MIN_CONFIDENCE:
            skipped_confidence += 1
            print(
                f"madison_events.py: skipped (confidence {confidence} < {MIN_CONFIDENCE}): {event['title']}"
            )
            continue

        fp = fingerprint(event)
        if fp in fingerprints:
            skipped_dup += 1
            continue

        if args.dry_run:
            preview = calendar_writer.build_event_body(event)
            reminder = preview["reminders"]["overrides"][0]["minutes"]
            print(
                f"[dry-run] would write: {event['title']} | start={preview['start']} "
                f"end={preview['end']} recurrence={preview.get('recurrence')} "
                f"reminder_minutes_before={reminder}"
            )
            continue

        if calendar_writer.check_event_exists(cal_service, cal_id, event):
            fingerprints.add(fp)
            skipped_dup += 1
            continue

        calendar_writer.write_event(cal_service, cal_id, event)
        fingerprints.add(fp)
        written += 1

    if not args.dry_run:
        state["fingerprints"] = sorted(fingerprints)
        state["last_run"] = run_started_at.isoformat()
        save_state(state)

    print(
        "madison_events.py: summary - "
        f"{len(emails)} emails scanned, {chunk_count} chunks processed, "
        f"{len(candidates)} candidates extracted, {merged_away} merged as duplicates, "
        f"{skipped_confidence} skipped by confidence, {skipped_dup} skipped as duplicate, "
        f"{written} written"
    )


if __name__ == "__main__":
    main()
