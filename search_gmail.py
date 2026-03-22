"""Search personal Gmail for family context (doctors, sports, schools, etc.).

Run via GitHub Actions workflow or locally with .env configured.
Outputs structured findings to stdout for easy parsing.
"""
import os
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TIMEZONE = ZoneInfo("America/Los_Angeles")

# Searches to find family context — add new queries here as needed
SEARCHES = [
    "doctor appointment OR physician OR medical OR annual physical",
    "pediatrician OR pediatric OR kids doctor",
    "dentist OR dental appointment OR dental cleaning",
    "orthodontist OR ortho OR braces",
    "eye doctor OR optometrist OR ophthalmologist OR vision",
    "volleyball club OR volleyball practice OR volleyball registration",
    "LAMO soccer OR LAMO registration",
    "eclipse soccer OR eclipse registration",
    "swim team OR springbrook OR swim practice",
    "gymnastics OR luna gymnastics",
    "piano lesson OR music lesson OR tutor OR tutoring",
    "school registration OR school enrollment OR parent teacher",
    "Stanley middle school OR Lafayette elementary",
    "camp OR summer camp registration",
    "birthday party invitation",
]


def build_gmail():
    """Build Gmail API client from OAuth credentials."""
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("gmail", "v1", credentials=creds)


def search(gmail, query: str, max_results: int = 5) -> list[dict]:
    """Run a Gmail search and return message summaries."""
    results = gmail.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        return []

    summaries = []
    for msg in messages:
        msg_data = gmail.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {
            h["name"]: h["value"]
            for h in msg_data.get("payload", {}).get("headers", [])
        }
        summaries.append({
            "from": headers.get("From", "Unknown"),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "snippet": msg_data.get("snippet", "")[:200],
        })
    return summaries


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    gmail = build_gmail()

    all_results = {}
    for query in SEARCHES:
        results = search(gmail, query)
        if results:
            all_results[query] = results
            print(f"\n{'='*60}")
            print(f"SEARCH: {query} ({len(results)} results)")
            print(f"{'='*60}")
            for r in results:
                print(f"  From: {r['from']}")
                print(f"  Subject: {r['subject']}")
                print(f"  Date: {r['date']}")
                print(f"  Preview: {r['snippet']}")
                print()
        else:
            print(f"[no results] {query}")

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(all_results)} of {len(SEARCHES)} searches had results")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
