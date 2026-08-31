"""Add a single event from a flyer screenshot/photo to the Madison Events calendar.

Usage:
    python add_event.py path/to/flyer.jpg
"""

import base64
import json
import mimetypes
import sys
from datetime import date

import anthropic
from dotenv import load_dotenv

import calendar_writer
from extract import _strip_json_fences
from madison_events import build_credentials, fingerprint

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You extract a single event from a photo of a flyer, poster, or screenshot
(e.g. from Instagram) for a personal calendar.

Rules:
- Extract the one event shown. If the image shows no identifiable event, return null.
- If the year is missing, assume the nearest future occurrence of that month/day relative to today's date.
- Return ONLY a JSON object (or the literal null). No prose, no markdown code fences.
- Fields:
  - title (string)
  - start_datetime (ISO 8601 string, include time if known)
  - end_datetime (ISO 8601 string, or null if unknown)
  - location (string, "Virtual" if online)
  - url (string, or null)
  - source_subject (string, e.g. "Instagram flyer screenshot")
  - one_line_description (string)
  - confidence (number 0-1, how sure you are this is a real attendable event with a clear date)
  - recurrence (iCalendar RRULE string if this is an ongoing recurring gathering, else null)
"""


def extract_from_image(image_path: str, today: date) -> dict | None:
    media_type, _ = mimetypes.guess_type(image_path)
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise ValueError(f"unsupported image type for {image_path}: {media_type}")

    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("ascii")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                    },
                    {"type": "text", "text": f"Today's date is {today.isoformat()}."},
                ],
            }
        ],
    )
    text_blocks = [block.text for block in response.content if block.type == "text"]
    text = _strip_json_fences("".join(text_blocks))
    return json.loads(text)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python add_event.py path/to/flyer.jpg")
        sys.exit(1)

    load_dotenv()
    today = date.today()
    event = extract_from_image(sys.argv[1], today)

    if not event or not isinstance(event, dict) or not event.get("title"):
        print("add_event.py: no event found in image")
        return

    print("Extracted event:")
    print(json.dumps(event, indent=2))
    confirm = input("\nAdd this to the Madison Events calendar? [y/N] ").strip().lower()
    if confirm != "y":
        print("add_event.py: not added")
        return

    creds = build_credentials()
    service = calendar_writer.get_calendar_service(creds)
    cal_id = calendar_writer.get_or_create_radar_calendar(service)

    if calendar_writer.check_event_exists(service, cal_id, event):
        print("add_event.py: a similar event already exists on that day, skipping")
        return

    calendar_writer.write_event(service, cal_id, event)
    print(f"add_event.py: added '{event['title']}' @ {event['start_datetime']}")

    fp = fingerprint(event)
    print(f"add_event.py: note - fingerprint {fp!r} was not recorded in state.json "
          "(that file is only updated by the daily scan)")


if __name__ == "__main__":
    main()
