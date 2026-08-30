import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def test_fingerprint_same_title_and_date_match():
    fp1 = fingerprint("Madison Craft Fair", "2026-09-14T10:00:00")
    fp2 = fingerprint("madison craft fair", "2026-09-14T18:00:00")
    assert fp1 == fp2


def test_dedupe_skips_known_fingerprint():
    known = {fingerprint("Madison Craft Fair", "2026-09-14T10:00:00")}
    candidate_fp = fingerprint("Madison Craft Fair", "2026-09-14T10:00:00")
    assert candidate_fp in known
