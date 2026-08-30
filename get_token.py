"""One-time local helper: opens a browser, prints a refresh token.

Not run by GitHub Actions. Run this locally whenever the refresh token
needs to be (re)generated, then copy the printed values into .env and
into the repo's GitHub Actions secrets.

Usage:
    python get_token.py /path/to/credentials.json
"""

import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python get_token.py /path/to/credentials.json")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0)

    data = json.loads(creds.to_json())
    print("\n--- Copy these into your .env and GitHub secrets ---")
    print(f"GOOGLE_CLIENT_ID     = {data['client_id']}")
    print(f"GOOGLE_CLIENT_SECRET = {data['client_secret']}")
    print(f"GOOGLE_REFRESH_TOKEN = {data['refresh_token']}")


if __name__ == "__main__":
    main()
