"""
auth.py — Google OAuth 2.0 helper.

On first run, opens a browser so you can authorise the app.
The resulting token is cached in token.json so subsequent runs
are silent (it auto-refreshes when the access token expires).

Usage:
    from auth import get_google_services, get_gmail_service
    calendar, tasks = get_google_services()
    gmail           = get_gmail_service()
"""

import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# All scopes the app requests.  If you add or remove a scope,
# delete token.json so the user re-authorises with the updated permissions.
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Paths relative to this file, so the script works from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(_HERE, "credentials.json")
TOKEN_FILE       = os.path.join(_HERE, "token.json")


def get_credentials() -> Credentials:
    """
    Load cached credentials or run the browser OAuth flow.
    Returns a valid, refreshed Credentials object.
    """
    creds = None

    # If a cached token exists, load it.
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # If there are no valid credentials, get new ones.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token expired but we have a refresh token — silently refresh.
            creds.refresh(Request())
        else:
            # First run (or token deleted): open the browser consent screen.
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            # port=0 lets the OS pick a free port for the local redirect server.
            creds = flow.run_local_server(port=0)

        # Save the (new or refreshed) token for next time.
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


def get_google_services():
    """
    Build and return authenticated service clients for Calendar and Tasks.

    Returns:
        (calendar_service, tasks_service)
    """
    creds = get_credentials()
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    tasks    = build("tasks",    "v1", credentials=creds, cache_discovery=False)
    return calendar, tasks


def get_gmail_service():
    """
    Build and return an authenticated Gmail service client.
    Separate from get_google_services() so Gmail init never blocks calendar/tasks.
    """
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
