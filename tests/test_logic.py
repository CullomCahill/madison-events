import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from extract import _strip_json_fences
from madison_events import fingerprint

GOOD_JSON = json.dumps(
    [
        {
            "title": "Madison Craft Fair",
            "start_datetime": "2026-09-14T10:00:00",
            "end_datetime": "2026-09-14T16:00:00",
            "location": "Madison, WI",
            "url": "https://example.com",
            "source_subject": "Weekly newsletter",
            "one_line_description": "A craft fair downtown.",
            "confidence": 0.9,
            "recurrence": None,
        }
    ]
)


def test_json_parse_good():
    parsed = json.loads(GOOD_JSON)
    assert isinstance(parsed, list)
    assert parsed[0]["title"] == "Madison Craft Fair"


def test_json_parse_bad_is_handled():
    garbage = "not json at all"
    try:
        json.loads(garbage)
        parsed_ok = True
    except json.JSONDecodeError:
        parsed_ok = False
    assert parsed_ok is False


def test_strip_json_fences():
    fenced = "```json\n[1, 2, 3]\n```"
    assert _strip_json_fences(fenced) == "[1, 2, 3]"
    assert _strip_json_fences("[1, 2, 3]") == "[1, 2, 3]"


def test_fingerprint_ignores_title_wording():
    # This is the actual bug from the first real run: two emails describing
    # the same gathering produced different titles and dodged dedup.
    event_a = {
        "title": "SnowFlower Tuesday Sangha Gathering",
        "start_datetime": "2026-09-01T19:00:00",
        "location": "SnowFlower Sangha, Madison WI",
    }
    event_b = {
        "title": "Tuesday Sangha - SnowFlower Meditation Group",
        "start_datetime": "2026-09-01T19:00:00",
        "location": "SnowFlower Sangha, Madison, WI",
    }
    assert fingerprint(event_a) == fingerprint(event_b)


def test_fingerprint_different_time_does_not_match():
    event_a = {"title": "Foo", "start_datetime": "2026-09-01T19:00:00", "location": "Bar"}
    event_b = {"title": "Foo", "start_datetime": "2026-09-01T20:00:00", "location": "Bar"}
    assert fingerprint(event_a) != fingerprint(event_b)


def test_fingerprint_recurring_ignores_anchor_date():
    # A recurring series' "next occurrence" date drifts forward every run;
    # the fingerprint must key on weekday+time, not the specific date.
    event_this_week = {
        "title": "SnowFlower Sunday Sangha",
        "start_datetime": "2026-08-30T10:00:00",
        "location": "SnowFlower Sangha",
        "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=SU",
    }
    event_next_week = {
        "title": "SnowFlower Sunday Sangha",
        "start_datetime": "2026-09-06T10:00:00",
        "location": "SnowFlower Sangha",
        "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=SU",
    }
    assert fingerprint(event_this_week) == fingerprint(event_next_week)


def test_dedupe_skips_known_fingerprint():
    event = {
        "title": "Madison Craft Fair",
        "start_datetime": "2026-09-14T10:00:00",
        "location": "Madison, WI",
    }
    known = {fingerprint(event)}
    assert fingerprint(event) in known
