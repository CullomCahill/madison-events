import base64
from datetime import datetime

from googleapiclient.discovery import build

# Scan the whole inbox (including Promotions-categorized newsletters like
# Isthmus), plus cullyPy_ai-labeled mail that's set to skip the inbox.
QUERY_TEMPLATE = "(in:inbox OR label:cullyPy_ai) after:{after}"

# Fallback escape hatch: if recurring-event consolidation in extract.py
# proves unreliable for a given sender, add a substring of their address
# here to skip their mail entirely (e.g. "isthmus@isthmus.com").
SKIP_SENDERS: list[str] = []


def fetch_emails(creds, since_dt: datetime) -> list[dict]:
    service = build("gmail", "v1", credentials=creds)
    query = QUERY_TEMPLATE.format(after=int(since_dt.timestamp()))

    emails = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token)
            .execute()
        )
        for msg_ref in resp.get("messages", []):
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_ref["id"], format="full")
                .execute()
            )
            parsed = _parse_message(msg)
            if any(s.lower() in parsed["from"].lower() for s in SKIP_SENDERS):
                continue
            emails.append(parsed)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return emails


def _parse_message(msg: dict) -> dict:
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    return {
        "subject": headers.get("Subject", ""),
        "from": headers.get("From", ""),
        "body": _extract_body(msg["payload"]),
        "snippet": msg.get("snippet", ""),
    }


def _extract_body(payload: dict) -> str:
    plain = _find_part(payload, "text/plain")
    if plain:
        return plain

    html = _find_part(payload, "text/html")
    if html:
        return _strip_html(html)

    return ""


def _find_part(payload: dict, mime_type: str) -> str | None:
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        return _decode(data) if data else None

    for part in payload.get("parts", []):
        found = _find_part(part, mime_type)
        if found:
            return found

    return None


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ASCII")).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
