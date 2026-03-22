"""Weekly Gmail scanner — finds family context and auto-updates family_context.md.

Searches personal Gmail for doctors, dentists, sports teams, schools, etc.
Uses Claude to extract structured info and merge into existing context file.
Runs weekly via GitHub Actions or manually.
"""
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("America/Los_Angeles")
CONTEXT_FILE = Path(__file__).parent / "family_context.md"

# Gmail search queries — add new ones here as needed
SEARCHES = [
    # Medical
    "doctor appointment OR physician OR medical OR annual physical",
    "pediatrician OR pediatric OR kids doctor",
    "dentist OR dental appointment OR dental cleaning",
    "orthodontist OR ortho OR braces",
    "eye doctor OR optometrist OR ophthalmologist OR vision",
    "pharmacy OR prescription OR refill",
    # Kids activities
    "volleyball club OR volleyball practice OR volleyball registration",
    "LAMO soccer OR LAMO registration",
    "eclipse soccer OR eclipse registration",
    "swim team OR springbrook OR swim practice",
    "gymnastics OR luna gymnastics",
    "piano lesson OR music lesson OR tutor OR tutoring",
    "camp OR summer camp registration",
    "birthday party invitation",
    "from:jackrabbittech.com",  # Town Hall Theatre
    # Schools
    "school registration OR school enrollment OR parent teacher",
    "Stanley middle school OR Lafayette elementary",
    "from:parentsquare.com",
    # Bills & financial
    "from:billpay.pge.com OR from:em.pge.com",
    "from:account.xfinity.com",
    "from:ecrmemail.verizonwireless.com",
    "from:no-reply@invoicecloud.net",  # Placer County property tax
    "from:no.reply.alerts@chase.com",
    "from:payments.pureinsurance.com",
    "from:info6.citi.com OR from:info15.citi.com",  # Costco Visa
    "from:notifications.usbank.com",
    "from:mail.fidelity.com subject:action",
    "from:notify.cloudflare.com subject:invoice",
    "from:NJCourtNotice.mbx@njcourts.gov",
    # Travel
    "from:united.com OR from:united airlines",
    "from:delta.com OR from:southwest.com OR from:aa.com",
    "flight confirmation OR booking confirmation OR e-ticket",
    "hotel reservation OR hotel confirmation",
    "from:airbnb.com OR from:vrbo.com",
    "rental car confirmation OR from:enterprise.com OR from:hertz.com",
    "boarding pass OR airline itinerary",
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


def scan_gmail(gmail) -> dict[str, list[dict]]:
    """Run all searches and collect results."""
    all_results = {}
    for query in SEARCHES:
        results = search(gmail, query)
        if results:
            all_results[query] = results
            logger.info(f"Found {len(results)} results for: {query}")
        else:
            logger.info(f"No results for: {query}")
    return all_results


def update_context(gmail_results: dict[str, list[dict]]) -> bool:
    """Use Claude to merge Gmail findings into family_context.md.

    Reads the existing context file, sends it + Gmail results to Claude,
    and writes back the updated version. Only adds NEW information —
    never removes existing entries.

    Returns:
        True if the file was updated, False if no changes needed.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot update context")
        return False

    existing_context = CONTEXT_FILE.read_text() if CONTEXT_FILE.exists() else ""

    # Format Gmail results for the prompt
    gmail_text = ""
    for query, results in gmail_results.items():
        gmail_text += f"\n### Search: {query}\n"
        for r in results:
            gmail_text += (
                f"- From: {r['from']}\n"
                f"  Subject: {r['subject']}\n"
                f"  Date: {r['date']}\n"
                f"  Preview: {r['snippet']}\n"
            )

    if not gmail_text.strip():
        logger.info("No Gmail results to process")
        return False

    prompt = f"""You are updating a family context file used by a daily itinerary AI.
The file helps the AI interpret calendar events accurately.

Here is the CURRENT family_context.md:
```
{existing_context}
```

Here are RECENT Gmail search results that may contain new family context:
{gmail_text}

Your job:
1. Extract any NEW, useful family context from the Gmail results:
   - Doctor/dentist names, addresses, phone numbers
   - Sports teams, coaches, practice schedules, locations
   - Schools, teachers
   - Recurring activities or classes
   - Upcoming travel: flights, hotels, Airbnb, rental cars, trips
   - Any other family logistics
2. MERGE new findings into the existing file structure, preserving ALL existing content
3. Do NOT remove or modify any existing entries unless correcting clearly outdated info
4. Do NOT add promotional/spam content or transient info
5. Keep the same markdown format and section structure
6. If a section doesn't exist yet, create it in the appropriate place
7. Travel goes in a "## Upcoming Travel" section with destination, dates, \
confirmation numbers, and any logistics
8. REMOVE travel entries whose dates have already passed
9. If nothing new was found, return the file EXACTLY as-is

Return ONLY the complete updated family_context.md content, no explanation."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        updated_content = message.content[0].text.strip()

        # Remove markdown code fences if Claude wrapped it
        if updated_content.startswith("```"):
            lines = updated_content.split("\n")
            # Remove first line (```markdown or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            updated_content = "\n".join(lines)

        if updated_content == existing_context.strip():
            logger.info("No new context found — file unchanged")
            return False

        CONTEXT_FILE.write_text(updated_content + "\n")
        logger.info("family_context.md updated with new findings")
        return True

    except Exception as e:
        logger.error(f"Failed to update context: {e}")
        return False


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logger.info("Scanning personal Gmail for family context...")
    gmail = build_gmail()
    results = scan_gmail(gmail)

    logger.info(f"Found results in {len(results)} of {len(SEARCHES)} searches")

    if results:
        updated = update_context(results)
        if updated:
            logger.info("Context file updated — changes will take effect "
                        "on next daily itinerary send")
        else:
            logger.info("No new context to add")
    else:
        logger.info("No Gmail results found across any search")


if __name__ == "__main__":
    main()
