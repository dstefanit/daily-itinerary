"""
One-time OAuth setup for personal Gmail access.

Run this locally once:
    python auth_gmail.py

It will open a browser, you log in with dennis.stefanitsis@gmail.com,
and it saves a refresh token. Copy the token into your .env file
and add it as a GitHub secret (GMAIL_REFRESH_TOKEN).
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

# Gmail read-only scope
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    # You need to download OAuth client credentials from Google Cloud Console:
    # APIs & Services → Credentials → Create OAuth Client ID → Desktop App
    # Save as oauth_client.json in this directory
    flow = InstalledAppFlow.from_client_secrets_file(
        "oauth_client.json", SCOPES
    )
    creds = flow.run_local_server(port=0)

    # Extract the refresh token — this is what we store as a secret
    print("\n--- Save this refresh token as GMAIL_REFRESH_TOKEN ---")
    print(creds.refresh_token)
    print("------------------------------------------------------\n")

    # Also save full token info for reference
    token_data = {
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "token_uri": creds.token_uri,
    }
    print("Full token data (for .env):")
    print(json.dumps(token_data))


if __name__ == "__main__":
    main()
