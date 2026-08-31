import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import anthropic

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_json_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


MODEL = "claude-sonnet-5"
BATCH_SIZE = 5
CHUNK_SIZE = 10000
CHUNK_OVERLAP = 500
MAX_WORKERS = 8

SYSTEM_PROMPT = """You extract real, attendable events from emails for a personal Madison, WI calendar.

Rules:
- Only include events physically located in the Madison, WI area, OR events that are explicitly virtual/online AND community/social/cultural/hobby-relevant (meetups, talks, workshops, performances, classes, retreats). Exclude generic corporate, regulatory, or government virtual meetings (e.g. FDA advisory committee meetings, agency hearings) even if technically attendable online.
- The event must have an actual date and be something a person could attend (not a sale deadline, subscription renewal, or generic newsletter content).
- Skip anything that has already happened relative to today's date.
- Do not include marketing copy, promotional deadlines, or newsletter filler that isn't a real event.
- For a limited-run production (a play, exhibit, or show with a fixed multi-week engagement, e.g. "runs Aug 30 - Sept 20"), emit ONE event for the opening/kickoff date only, not one per showtime. If the run's opening date has already passed but the run is still ongoing (today falls within the run's date range), use TODAY's date as start_datetime instead of the original opening date — do not emit an event dated in the past. Mention the run's full date range in one_line_description if known.
- This does NOT apply to ongoing recurring meetups (weekly meditation groups, standing classes, regular gatherings with no end date) — see the recurrence rule below.
- If an event is an ongoing recurring gathering with no end date (a weekly meditation group, a standing class, a regular meetup), set start_datetime to the next upcoming occurrence and set recurrence to an iCalendar RRULE string describing the pattern (e.g. "RRULE:FREQ=WEEKLY;BYDAY=TU"). For everything else, set recurrence to null.
- For an event with no specific times spanning multiple days (a festival, a multi-day exhibit), treat it as an all-day event: set start_datetime and end_datetime to bare dates (YYYY-MM-DD, no time component, no "T"), where end_datetime is the LAST day the event actually occurs (inclusive). Do not invent times like 00:00 or 23:59 for these.
- Return ONLY a JSON array. No prose, no markdown code fences, no explanation.
- Each element must have exactly these fields:
  - title (string)
  - start_datetime (ISO 8601 string; a bare date YYYY-MM-DD for all-day events, otherwise include time)
  - end_datetime (ISO 8601 string, or null if unknown; a bare date for all-day events)
  - location (string, "Virtual" if online)
  - url (string, or null)
  - source_subject (string, the email subject this came from)
  - one_line_description (string)
  - confidence (number 0-1, how sure you are this is a real attendable event)
  - recurrence (iCalendar RRULE string, or null)
- If nothing in the batch qualifies, return an empty array: []
"""

MERGE_SYSTEM_PROMPT = """You are given a JSON array of candidate calendar events extracted from several
different emails. Some entries may describe the SAME real-world event or gathering, worded
differently because they came from different source emails.

Merge duplicates:
- If two or more entries clearly describe the same real event (same date, same or very close
  time, same or clearly related location), merge them into ONE entry. Keep the clearer/more
  complete title and description, the most specific location and url, and the highest confidence.
- If several entries describe the same ongoing recurring gathering (e.g. a weekly meditation
  group announced across multiple emails, each for a different upcoming week), merge them into
  ONE entry representing the series: set start_datetime to the soonest upcoming occurrence and
  set recurrence to an iCalendar RRULE string (e.g. "RRULE:FREQ=WEEKLY;BYDAY=TU"). Do not keep
  separate one-off entries for that series.
- Do not merge events that are genuinely different (different dates, different gatherings).

Return ONLY a JSON array of the resulting events, using the same field schema as the input
(title, start_datetime, end_datetime, location, url, source_subject, one_line_description,
confidence, recurrence). No prose, no markdown fences.
"""


def _chunk_body(body: str) -> list[str]:
    if len(body) <= CHUNK_SIZE:
        return [body]

    chunks = []
    start = 0
    while start < len(body):
        end = start + CHUNK_SIZE
        chunks.append(body[start:end])
        if end >= len(body):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def _expand_to_chunks(emails: list[dict]) -> list[dict]:
    chunked = []
    for email in emails:
        body = email.get("body", "")
        chunks = _chunk_body(body)
        print(
            f"extract.py: '{email.get('subject', '')[:60]}' body_len={len(body)} chunks={len(chunks)}"
        )
        for chunk in chunks:
            item = dict(email)
            item["body"] = chunk
            chunked.append(item)
    return chunked


def extract_events(emails: list[dict], today: date) -> tuple[list[dict], int]:
    client = anthropic.Anthropic()
    chunks = _expand_to_chunks(emails)

    batches = [chunks[i : i + BATCH_SIZE] for i in range(0, len(chunks), BATCH_SIZE)]

    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for result in pool.map(lambda batch: _extract_batch(client, batch, today), batches):
            events.extend(result)

    return events, len(chunks)


def _extract_batch(client: anthropic.Anthropic, batch: list[dict], today: date) -> list[dict]:
    user_content = f"Today's date is {today.isoformat()}.\n\nEmails:\n\n"
    for item in batch:
        user_content += (
            f"---\nSubject: {item['subject']}\nFrom: {item['from']}\nBody:\n{item['body']}\n"
        )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text_blocks = [block.text for block in response.content if block.type == "text"]
        text = _strip_json_fences("".join(text_blocks))
        if not text:
            print(
                f"extract.py: batch returned no text (stop_reason={response.stop_reason}, "
                f"output_tokens={response.usage.output_tokens}), skipping"
            )
            return []
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            print(f"extract.py: batch returned non-list JSON, skipping: {text[:200]}")
            return []
        return parsed
    except Exception as exc:
        print(f"extract.py: skipping batch due to error: {exc}")
        return []


def merge_duplicate_events(events: list[dict], today: date) -> list[dict]:
    if len(events) < 2:
        return events

    client = anthropic.Anthropic()
    payload = json.dumps(events)

    try:
        # Large event lists produce large JSON output; stream with a generous
        # budget so the response isn't cut off mid-JSON (and to avoid the
        # non-streaming HTTP timeout that a big max_tokens can otherwise hit).
        with client.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=MERGE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Today's date is {today.isoformat()}.\n\nEvents:\n{payload}",
                }
            ],
        ) as stream:
            response = stream.get_final_message()
        text_blocks = [block.text for block in response.content if block.type == "text"]
        text = _strip_json_fences("".join(text_blocks))
        if not text:
            print(
                f"extract.py: merge step returned no text (stop_reason={response.stop_reason}, "
                f"output_tokens={response.usage.output_tokens}), keeping originals unmerged"
            )
            return events
        merged = json.loads(text)
        if not isinstance(merged, list):
            print(f"extract.py: merge step returned non-list JSON, keeping originals: {text[:200]}")
            return events
        return merged
    except Exception as exc:
        print(f"extract.py: merge step failed ({exc}), keeping originals unmerged")
        return events
