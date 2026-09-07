"""
One-off local utility: mint a fresh Google OAuth refresh token for the
Calendar scope used by the six *_yoga_sync.py scripts.

Not part of the sync pipeline itself. Run this by hand whenever
GOOGLE_REFRESH_TOKEN dies (e.g. after the OAuth app's Testing-mode 7-day
token expiry, or a manual revoke). Reads GOOGLE_CLIENT_ID and
GOOGLE_CLIENT_SECRET from .env, pops a browser consent window, and prints
the new refresh token -- paste that into .env and the GitHub Actions
secret of the same name.

Requires: pip install google-auth-oauthlib
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]

CLIENT_CONFIG = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def main():
    flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
    # access_type=offline + prompt=consent forces Google to hand back a
    # refresh token even if this client already has one on file.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    print()
    print("New refresh token:")
    print(creds.refresh_token)


if __name__ == "__main__":
    main()
