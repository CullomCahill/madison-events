import json
from datetime import date

import anthropic

MODEL = "claude-sonnet-5"
BATCH_SIZE = 5
BODY_TRUNCATE = 3000

SYSTEM_PROMPT = """You extract real, attendable events from emails for a personal Madison, WI calendar.

Rules:
- Only include events physically located in the Madison, WI area, OR events that are explicitly virtual/online AND community/social/cultural/hobby-relevant (meetups, talks, workshops, performances, classes, retreats). Exclude generic corporate, regulatory, or government virtual meetings (e.g. FDA advisory committee meetings, agency hearings) even if technically attendable online.
- The event must have an actual date and be something a person could attend (not a sale deadline, subscription renewal, or generic newsletter content).
- Skip anything that has already happened relative to today's date.
- Do not include marketing copy, promotional deadlines, or newsletter filler that isn't a real event.
- For a limited-run production (a play, exhibit, or show with a fixed multi-week engagement, e.g. "runs Aug 30 - Sept 20"), emit ONE event for the opening/kickoff date only, not one per showtime. Mention the run's full date range in one_line_description if known.
- This does NOT apply to ongoing recurring meetups (weekly meditation groups, standing classes, regular gatherings with no end date) — those should still get one event per occurrence as normal.
- Return ONLY a JSON array. No prose, no markdown code fences, no explanation.
- Each element must have exactly these fields:
  - title (string)
  - start_datetime (ISO 8601 string, include time if known)
  - end_datetime (ISO 8601 string, or null if unknown)
  - location (string, "Virtual" if online)
  - url (string, or null)
  - source_subject (string, the email subject this came from)
  - one_line_description (string)
  - confidence (number 0-1, how sure you are this is a real attendable event)
- If nothing in the batch qualifies, return an empty array: []
"""


def extract_events(emails: list[dict], today: date) -> list[dict]:
    client = anthropic.Anthropic()
    events: list[dict] = []

    for i in range(0, len(emails), BATCH_SIZE):
        batch = emails[i : i + BATCH_SIZE]
        events.extend(_extract_batch(client, batch, today))

    return events


def _extract_batch(client: anthropic.Anthropic, batch: list[dict], today: date) -> list[dict]:
    user_content = f"Today's date is {today.isoformat()}.\n\nEmails:\n\n"
    for email in batch:
        body = email["body"][:BODY_TRUNCATE]
        user_content += (
            f"---\nSubject: {email['subject']}\nFrom: {email['from']}\nBody:\n{body}\n"
        )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text_blocks = [block.text for block in response.content if block.type == "text"]
        text = "".join(text_blocks).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            print(f"extract.py: batch returned non-list JSON, skipping: {text[:200]}")
            return []
        return parsed
    except Exception as exc:
        print(f"extract.py: skipping batch due to error: {exc}")
        return []
