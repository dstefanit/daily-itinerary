"""
Stefanitsis Family Itinerary — pulls Google Calendar events and weather,
uses AI to summarize the day with practical nudges, sends a formatted
morning email with week-ahead preview and upcoming birthdays.
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests
import icalendar
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from jinja2 import Environment, FileSystemLoader
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, HtmlContent

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --- Config ---
TIMEZONE = ZoneInfo("America/Los_Angeles")
LOCATION = "Lafayette, CA"
LAT, LON = 37.8858, -122.1233  # Lafayette, CA

SENDER = "dennis@glacierpointinsurance.agency"

# Calendar ID → label for organizing events
CALENDARS = {
    "dennis.stefanitsis@gmail.com": "Dennis",
    "amylynnfischer@gmail.com": "Amy",
    "family05389224298174643941@group.calendar.google.com": "Family",
}

# ICS/webcal feeds — fetched directly, no Google Calendar subscription needed
ICS_FEEDS = {
    "https://lamorindasc.byga.net/cal/dnphjqyNfD.ics": "Anna Soccer",
    "https://eastbayeclipse.byga.net/cal/aoXhNbLkFA.ics": "Sophia Soccer",
    (
        "https://lmyasports.leagueapps.com/ajax/loadSchedule"
        "?origin=site&scope=user&publishedOnly=0"
        "&itemType=games_events&userScope=me_kids"
        "&startsAfterDate=01/01/2026&startsBeforeDate=12/31/2026"
        "&programId=&iCalExport=true&userId=13321030"
    ): "Volleyball",
    (
        "https://www.gomotionapp.com/rest/ics/system/3/General.ics"
        "?key=TvJ8yGo6%2FRO5FqvDtM2nEQ%3D%3D&enabled=true"
        "&startDate=1769932800000&endDate=1803888000000"
        "&roster_group_id=130962"
    ): "Sophia Swim",
    (
        "https://www.gomotionapp.com/rest/ics/system/5/Events.ics"
        "?key=TvJ8yGo6%2FRO5FqvDtM2nEQ%3D%3D&enabled=false"
        "&tz=America%2FLos_Angeles"
    ): "Sophia Swim",
    (
        "https://www.lafsd.org/apps/events/ical/"
        "?id=0&cb=1774233892517"
    ): "School",
}

# School calendar events to INCLUDE — no school days, early dismissals,
# first/last day. Everything else (board meetings, town halls) is skipped.
_SCHOOL_KEYWORDS = [
    "no school", "early dismissal", "first day of school",
    "last day of school", "break", "holiday",
    "district office closed", "conference",
]

# Family context for the AI summary — loads from family_context.md if available
def _load_family_context() -> str:
    """Load family context from markdown file, fall back to default."""
    context_path = Path(__file__).parent / "family_context.md"
    if context_path.exists():
        return context_path.read_text()
    return (
        "The Stefanitsis family: Dennis (dad), Amy (mom), "
        "Anna (12 year old daughter), Sophia (10 year old daughter). "
        "They live in Lafayette, CA."
    )


FAMILY_CONTEXT = _load_family_context()


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from Claude's response.

    Claude sometimes wraps JSON in ```json ... ``` blocks.
    This extracts the content inside the fences.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    return text

# Birthdays and anniversaries — (month, day, label, year_born_or_married)
# year is used to calculate age/years married
SPECIAL_DATES = [
    (8, 11, "Dennis's Birthday", 1981),
    (6, 29, "Amy's Birthday", 1977),
    (8, 21, "Anna's Birthday", 2013),
    (9, 11, "Sophia's Birthday", 2015),
    (11, 11, "Wedding Anniversary", 2012),
]

# How many days ahead to show upcoming birthdays/anniversaries
SPECIAL_DATE_LOOKAHEAD_DAYS = 90


def _build_calendar_service():
    """Build and return a Google Calendar API service client."""
    sa_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not sa_key:
        logger.warning("GOOGLE_SERVICE_ACCOUNT_KEY not set — skipping calendar")
        return None

    key_data = json.loads(sa_key)
    creds = service_account.Credentials.from_service_account_info(
        key_data, scopes=["https://www.googleapis.com/auth/calendar.readonly"]
    )
    return build("calendar", "v3", credentials=creds)


def _detect_person(calendar: str, summary: str) -> str:
    """Detect which family member an event belongs to (fallback).

    Kid-specific keywords in the summary ALWAYS take priority over
    parent calendar names, because parents put kids' events on their
    own calendars all the time.

    Returns 'anna', 'sophia', 'dennis', 'amy', or '' if no match.
    """
    cal = calendar.lower()
    summ = summary.lower()

    # 1) Kid-dedicated calendars are definitive
    if "anna" in cal and "sophia" not in cal:
        return "anna"
    if "sophia" in cal and "anna" not in cal:
        return "sophia"

    # 2) Kid keywords in summary — ALWAYS checked before parent calendars
    # Anna: LAMO soccer, Coach Luis, Stanley, 6th grade volleyball, Myrtle
    anna_keywords = [
        "lamo", "luis", "lamorinda soccer", "stanley",
        "6th grade", "myrtle",
    ]
    if any(k in summ for k in anna_keywords):
        return "anna"

    # Sophia: swim, Springbrook, Eclipse soccer, Luna gymnastics,
    # 4th grade volleyball, Manhattan team
    sophia_keywords = [
        "swim", "springbrook", "eclipse", "luna", "gymnastics",
        "play sophia", "sophia", "4th grade", "manhattan",
    ]
    if any(k in summ for k in sophia_keywords):
        return "sophia"

    # Check for kid names in summary
    if "anna" in summ:
        return "anna"

    # 3) Parent calendar names — only if no kid keyword matched
    if "dennis" in cal:
        return "dennis"
    if "amy" in cal:
        return "amy"

    # 4) Parent names in summary
    if "dennis" in summ:
        return "dennis"
    if "amy" in summ:
        return "amy"

    return ""


def _fetch_events(service, time_min: datetime, time_max: datetime) -> list[dict]:
    """Fetch events from all calendars within a time range.

    Args:
        service: Google Calendar API service client
        time_min: Start of range (inclusive)
        time_max: End of range (exclusive)

    Returns:
        List of event dicts with keys: start, end, summary, location,
        all_day, calendar
    """
    all_events = []
    for cal_id, cal_label in CALENDARS.items():
        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            # Family names — only their birthdays are tracked separately
            family_names = {"dennis", "amy", "anna", "sophia"}

            for event in result.get("items", []):
                # Skip birthday events (Google Contacts auto-generated)
                if event.get("eventType") == "birthday":
                    continue
                summary = event.get("summary", "(No title)")
                summary_lower = summary.lower()
                # Filter all birthday-like events (birthday, bday, b-day)
                # unless it's a family member's birthday party we're hosting
                if any(w in summary_lower for w in ("birthday", "bday", "b-day")):
                    # Keep only if it's clearly a party event we're attending
                    if "party" not in summary_lower:
                        continue

                start = event["start"]
                end = event["end"]

                all_day = "date" in start
                if all_day:
                    start_dt = datetime.strptime(start["date"], "%Y-%m-%d")
                    end_dt = datetime.strptime(end["date"], "%Y-%m-%d")
                    # Clamp start to query range for multi-day events
                    # so "Spring Break 3/9-3/22" shows as today, not 3/9
                    if start_dt < time_min.replace(tzinfo=None):
                        start_dt = time_min.replace(
                            hour=0, minute=0, second=0,
                            microsecond=0, tzinfo=None,
                        )
                else:
                    start_dt = datetime.fromisoformat(start["dateTime"])
                    end_dt = datetime.fromisoformat(end["dateTime"])

                all_events.append({
                    "start": start_dt,
                    "end": end_dt,
                    "summary": summary,
                    "description": event.get("description", ""),
                    "location": _clean_location(event.get("location", "")),
                    "all_day": all_day,
                    "calendar": cal_label,
                    "person": _detect_person(cal_label, summary),
                })
        except Exception as e:
            logger.error(f"Failed to fetch calendar {cal_label}: {e}")

    # Merge in ICS feed events
    all_events.extend(_fetch_ics_events(time_min, time_max))

    all_events.sort(key=lambda e: (not e["all_day"], e["start"]))
    return _dedup_events(all_events)


import re


def _clean_ics_summary(summary: str) -> str:
    """Clean verbose ICS event summaries into human-readable form.

    Examples:
        "Practice: 2013G Navy (LaMO ARENA)" → "Practice"
        "Practice: U11 Girls(2034)-26SP (Lafayette...)" → "Practice"
        "2013G Navy at HYSL" → "Game vs HYSL"
        "CP25-2015GS vs CFC 2015 G White" → "Game vs CFC"
    """
    s = summary.strip()

    # "Practice: TEAM (LOCATION)" → "Soccer Practice"
    if s.lower().startswith("practice:"):
        return "Soccer Practice"

    # "4th grade Volleyball Practice (4th Grade)" → "Volleyball Practice"
    # "6th grade Volleyball Game (6th Grade)" → "Volleyball Game"
    vball_match = re.match(
        r"\d+(?:st|nd|rd|th)\s+grade\s+(Volleyball\s+\w+)", s, re.IGNORECASE
    )
    if vball_match:
        return vball_match.group(1).strip()

    # "TEAM at OPPONENT" → "Game @ OPPONENT"
    at_match = re.match(
        r"(?:[\w\s\-]+?\d{4}\w*\s+)at\s+(.+)", s, re.IGNORECASE
    )
    if at_match:
        opponent = at_match.group(1).strip()
        # Shorten opponent — take first word/acronym
        opponent_short = opponent.split()[0] if opponent else opponent
        return f"Soccer @ {opponent_short}"

    # "CODE vs OPPONENT" → "Game vs OPPONENT"
    vs_match = re.match(r".+?\s+vs\.?\s+(.+)", s, re.IGNORECASE)
    if vs_match:
        opponent = vs_match.group(1).strip()
        # Drop team year codes from opponent
        opponent = re.sub(r"\b\d{4}\s*[GBgb]?\s*", "", opponent).strip()
        return f"Soccer vs {opponent}"

    # "[Placeholder]" events
    if "[placeholder]" in s.lower():
        # "Tournament [Placeholder] (TEAM)" → "Tournament"
        return re.sub(r"\s*\[.*?\]\s*\(.*?\)", "", s).strip()

    return s


def _clean_location(location: str) -> str:
    """Shorten location to just the venue name, dropping street address.

    Examples:
        "LaMO ARENA, 452 Center ST, #A, Moraga" → "LaMO Arena"
        "Wilder Field #2, 101 Wilder Rd, Orinda, CA 94563" → "Wilder Field #2"
        "Lafayette Community Center Futsal Rink, 500 St. Mary's..." → "Lafayette CC Futsal"
    """
    if not location:
        return ""
    # Take everything before the first street number pattern
    # e.g., "Venue Name, 123 Street..." → "Venue Name"
    venue = re.split(r",\s*\d+\s", location)[0].strip().rstrip(",")

    # Shorten common long names
    venue = venue.replace("Lafayette Community Center", "Lafayette CC")
    venue = venue.replace("Wilder Sports Complex - ", "Wilder ")
    venue = venue.replace("Sports Complex", "")

    return venue


def _fetch_ics_events(
    time_min: datetime, time_max: datetime
) -> list[dict]:
    """Fetch events from ICS/webcal feeds (BYGA soccer, etc.).

    Args:
        time_min: Start of range (inclusive)
        time_max: End of range (exclusive)

    Returns:
        List of event dicts matching the same format as Google Calendar events.
    """
    events = []
    for url, label in ICS_FEEDS.items():
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            cal = icalendar.Calendar.from_ical(resp.content)

            for component in cal.walk():
                if component.name != "VEVENT":
                    continue

                summary = str(component.get("SUMMARY", "(No title)"))
                # Skip cancelled events
                if summary.upper().startswith("CANCELLED"):
                    continue

                # School calendar: only keep relevant events
                if label == "School":
                    summ_lower = summary.lower()
                    if not any(k in summ_lower for k in _SCHOOL_KEYWORDS):
                        continue

                dtstart = component.get("DTSTART")
                dtend = component.get("DTEND")
                if not dtstart:
                    continue

                start_dt = dtstart.dt
                end_dt = dtend.dt if dtend else start_dt

                # Handle date vs datetime
                all_day = isinstance(start_dt, date) and not isinstance(
                    start_dt, datetime
                )
                if all_day:
                    # Compare as dates
                    if start_dt >= time_max.date() or end_dt <= time_min.date():
                        continue
                    # Clamp start to query range for multi-day events
                    # so "Spring Break 3/9-3/22" shows as today, not 3/9
                    effective_start = max(start_dt, time_min.date())
                    start_dt = datetime.combine(
                        effective_start, datetime.min.time()
                    )
                    end_dt = datetime.combine(end_dt, datetime.min.time())
                else:
                    # Ensure timezone-aware for comparison
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=TIMEZONE)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=TIMEZONE)
                    if start_dt >= time_max or end_dt <= time_min:
                        continue

                location = str(component.get("LOCATION", ""))
                description = str(component.get("DESCRIPTION", ""))

                # Detect person from ORIGINAL summary before cleaning
                person = _detect_person(label, summary)

                # Clean up verbose ICS summaries and locations
                summary = _clean_ics_summary(summary)
                location = _clean_location(location)

                events.append({
                    "start": start_dt,
                    "end": end_dt,
                    "summary": summary,
                    "description": description,
                    "location": location,
                    "all_day": all_day,
                    "calendar": label,
                    "person": person,
                })

            logger.info(f"ICS feed '{label}': fetched, "
                        f"{len(events)} events in range")
        except Exception as e:
            logger.warning(f"ICS feed '{label}' failed: {e}")

    return events


def _dedup_events(events: list[dict]) -> list[dict]:
    """Merge duplicate events that appear on multiple calendars.

    Two events are considered duplicates if they have the same summary
    and start time. Merged events show all calendar sources.

    Args:
        events: Sorted list of event dicts

    Returns:
        Deduplicated list with calendar field as comma-joined string
    """
    seen: dict[str, dict] = {}  # key → event dict
    deduped = []

    for e in events:
        # Build a key from summary + start time
        start_str = e["start"].isoformat() if hasattr(e["start"], "isoformat") else str(e["start"])
        key = f"{e['summary'].strip().lower()}|{start_str}"

        if key in seen:
            # Append this calendar to the existing entry
            existing = seen[key]
            if e["calendar"] not in existing["calendar"]:
                existing["calendar"] += f", {e['calendar']}"
            # Keep the more specific location if one is missing
            if not existing["location"] and e.get("location"):
                existing["location"] = e["location"]
        else:
            # Copy so we don't mutate the original
            entry = dict(e)
            seen[key] = entry
            deduped.append(entry)

    return deduped


def get_today_events(service) -> list[dict]:
    """Fetch today's events (midnight-to-midnight Pacific)."""
    now = datetime.now(TIMEZONE)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    return _fetch_events(service, start_of_day, end_of_day)


def get_week_ahead_events(service) -> list[dict]:
    """Fetch events for the rest of the week (tomorrow through Sunday)."""
    now = datetime.now(TIMEZONE)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    # Days until end of Sunday (weekday 6 = Sunday)
    days_until_sunday = 6 - now.weekday()
    if days_until_sunday <= 0:
        days_until_sunday = 7
    end_of_week = tomorrow + timedelta(days=days_until_sunday)

    return _fetch_events(service, tomorrow, end_of_week)


def get_upcoming_special_dates() -> list[dict]:
    """Check for birthdays and anniversaries in the next 30 days.

    Returns:
        List of dicts with keys: label, date, days_away, milestone
    """
    today = date.today()
    upcoming = []

    for month, day, label, origin_year in SPECIAL_DATES:
        # This year's occurrence
        try:
            this_year_date = date(today.year, month, day)
        except ValueError:
            continue

        # If it already passed this year, check next year
        if this_year_date < today:
            this_year_date = date(today.year + 1, month, day)

        days_away = (this_year_date - today).days

        if days_away <= SPECIAL_DATE_LOOKAHEAD_DAYS:
            # Calculate milestone (age or years married)
            milestone = None
            if origin_year:
                milestone = this_year_date.year - origin_year

            upcoming.append({
                "label": label,
                "date": this_year_date,
                "days_away": days_away,
                "milestone": milestone,
            })

    # Sort by soonest first
    upcoming.sort(key=lambda x: x["days_away"])
    return upcoming


def get_upcoming_travel(service) -> list[dict]:
    """Find upcoming travel from calendar events and family_context.md.

    Searches calendars for travel-related events (flights, hotels, trips)
    in the next 60 days, plus any travel listed in the context file.
    Uses Claude to dedupe and structure everything.

    Args:
        service: Google Calendar API service client

    Returns:
        List of dicts with keys: trip, dates, details
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []

    # 1. Search calendars for travel-related events (next 60 days)
    travel_events = []
    if service:
        now = datetime.now(TIMEZONE)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=60)

        travel_keywords = [
            "flight", "fly", "airport", "airline", "united",
            "hotel", "airbnb", "vrbo", "resort", "cabin",
            "trip", "vacation", "travel", "disneyland", "disney",
        ]

        all_events = _fetch_events(service, start, end)
        for e in all_events:
            summary_lower = e["summary"].lower()
            loc_lower = (e.get("location") or "").lower()
            combined = summary_lower + " " + loc_lower
            if any(kw in combined for kw in travel_keywords):
                dt = e["start"]
                if hasattr(dt, "astimezone"):
                    dt = dt.astimezone(TIMEZONE)
                travel_events.append(
                    f"- {dt.strftime('%b %-d')}: {e['summary']}"
                    f"{' at ' + e['location'] if e.get('location') else ''}"
                )

    # 2. Check family_context.md for travel section
    context_travel = ""
    context = FAMILY_CONTEXT
    if "## Upcoming Travel" in context:
        travel_start = context.index("## Upcoming Travel")
        next_section = context.find("\n## ", travel_start + 1)
        section = context[travel_start:next_section] if next_section > 0 else context[travel_start:]
        if section.strip() != "## Upcoming Travel":
            context_travel = section

    # Combine sources
    all_travel_text = ""
    if travel_events:
        all_travel_text += "Calendar events:\n" + "\n".join(travel_events)
    if context_travel:
        all_travel_text += "\n\n" + context_travel

    if not all_travel_text.strip():
        return []

    try:
        client = anthropic.Anthropic(api_key=api_key)
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            messages=[{"role": "user", "content": f"""Extract upcoming travel from these sources. \
Today is {today}. Only include future trips, not past ones. \
Group related events into single trips (e.g., a flight + hotel = one trip). \
IMPORTANT: Only include trips that involve flights or leaving the San Francisco Bay Area \
(e.g., a Disneyland trip, a flight to Denver). Exclude local Bay Area activities \
like a nearby hotel stay or local rental car.

{all_travel_text}

Return a JSON array of objects with these keys:
- "trip": short destination/name (e.g., "Tahoe Family Trip")
- "dates": date range as string (e.g., "Apr 10-13")
- "details": one-line logistics (e.g., "United SFO-DEN, Airbnb in Breckenridge")

If no upcoming trips, return [].
Return ONLY the JSON array, nothing else."""}],
        )
        response_text = _strip_code_fences(message.content[0].text)
        trips = json.loads(response_text)
        logger.info(f"Found {len(trips)} upcoming trip(s)")
        return trips
    except Exception as e:
        logger.error(f"Travel extraction failed: {e}")
        return []


def _get_weather_grab(weather: dict) -> str:
    """Generate a quick 'grab before you leave' note based on weather.

    Returns a short string like 'Grab a raincoat' or empty string if
    nothing special is needed.
    """
    if not weather:
        return ""

    tips = []
    temp = weather.get("temp", 70)
    high = weather.get("high", 70)
    low = weather.get("low", 60)
    rain = weather.get("rain_chance", 0)

    # Rain gear
    if rain >= 50:
        tips.append(f"{rain}% chance of rain — grab a raincoat and umbrella")
    elif rain >= 30:
        tips.append(f"{rain}% rain chance — bring an umbrella just in case")

    # Cold gear — based on current temp and low
    if low < 45 or temp < 50:
        tips.append(f"Low of {low}°F — warm jacket recommended")
    elif low < 55 or temp < 60:
        tips.append(f"Cooling to {low}°F — fleece or light jacket")

    # Sun protection
    if high >= 85:
        tips.append(f"High of {high}°F — don't forget sunscreen")

    if not tips:
        return ""
    return ". ".join(tips) + "."


def get_weather() -> dict:
    """Fetch current weather and daily forecast for Lafayette, CA.

    Returns:
        Dict with keys: temp, high, low, description, icon,
        rain_chance, uv_index
    """
    api_key = os.environ.get("OPENWEATHERMAP_API_KEY")
    if not api_key:
        logger.warning("OPENWEATHERMAP_API_KEY not set — skipping weather")
        return {}

    try:
        # Current weather
        current_url = "https://api.openweathermap.org/data/2.5/weather"
        current_params = {
            "lat": LAT, "lon": LON,
            "appid": api_key, "units": "imperial",
        }
        current_resp = requests.get(current_url, params=current_params, timeout=10)
        current_resp.raise_for_status()
        current_data = current_resp.json()

        # Daily forecast (for high/low)
        forecast_url = "https://api.openweathermap.org/data/2.5/forecast"
        forecast_params = {
            "lat": LAT, "lon": LON,
            "appid": api_key, "units": "imperial",
            "cnt": 8,  # Next 24 hours in 3-hour blocks
        }
        forecast_resp = requests.get(forecast_url, params=forecast_params, timeout=10)
        forecast_resp.raise_for_status()
        forecast_data = forecast_resp.json()

        # Calculate high/low from forecast blocks
        temps = [b["main"]["temp"] for b in forecast_data["list"]]
        temps.append(current_data["main"]["temp"])
        rain_chance = max(
            (round(b.get("pop", 0) * 100) for b in forecast_data["list"]),
            default=0,
        )

        return {
            "temp": round(current_data["main"]["temp"]),
            "high": round(max(temps)),
            "low": round(min(temps)),
            "description": current_data["weather"][0]["description"].title(),
            "icon": current_data["weather"][0]["icon"],
            "rain_chance": rain_chance,
            "uv_index": 0,  # Not available on 2.5 free tier
        }
    except Exception as e:
        logger.error(f"Weather API failed: {e}")
        return {}


def _get_or_create_label(gmail, label_name: str) -> str | None:
    """Get or create a Gmail label. Returns the label ID, or None on error."""
    try:
        results = gmail.users().labels().list(userId="me").execute()
        for label in results.get("labels", []):
            if label["name"] == label_name:
                return label["id"]
        # Label doesn't exist — create it
        body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = gmail.users().labels().create(userId="me", body=body).execute()
        logger.info(f"Created Gmail label '{label_name}' (id={created['id']})")
        return created["id"]
    except Exception as e:
        logger.warning(f"Could not get/create Gmail label '{label_name}': {e}")
        return None


def get_gmail_action_items() -> dict[str, list[str]]:
    """Scan personal Gmail for recent emails and extract tiered action items.

    Uses OAuth refresh token to access dennis.stefanitsis@gmail.com,
    pulls recent messages + financial lookback, and uses Claude to
    identify and tier actionable items. Emails labeled "Done" in
    Gmail are excluded.

    Returns:
        Dict with keys 'due_now', 'this_week', 'on_radar' —
        each a list of action item strings. Returns empty dict on error.
    """
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    gmail_client_id = os.environ.get("GMAIL_CLIENT_ID")
    gmail_client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if not all([refresh_token, gmail_client_id, gmail_client_secret]):
        logger.warning("Gmail OAuth not configured — skipping action items")
        return []
    if not anthropic_key:
        logger.warning("ANTHROPIC_API_KEY not set — skipping action items")
        return []

    try:
        # Build Gmail credentials from refresh token
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=gmail_client_id,
            client_secret=gmail_client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        gmail = build("gmail", "v1", credentials=creds)

        # Ensure "Done" label exists (for filtering completed items)
        done_label_id = _get_or_create_label(gmail, "Done")

        # Exclude emails labeled "Done" — Dennis marks these when handled
        done_filter = f" -label:Done" if done_label_id else ""

        # Search 1: Recent emails (last 3 days, all categories)
        now = datetime.now(TIMEZONE)
        after_date = (now - timedelta(days=3)).strftime("%Y/%m/%d")
        query = f"after:{after_date}{done_filter}"

        results = gmail.users().messages().list(
            userId="me", q=query, maxResults=30
        ).execute()
        messages = results.get("messages", [])

        # Search 2: Financial/deadline emails (last 14 days) —
        # bills often arrive early and sit in Promotions/Updates
        bills_after = (now - timedelta(days=14)).strftime("%Y/%m/%d")
        bills_query = (
            f"after:{bills_after}{done_filter} "
            f"(bill OR payment OR due OR invoice OR autopay OR past due "
            f"OR statement OR tax OR penalty OR registration OR deadline "
            f"OR renewal OR expires OR expiring)"
        )
        bills_results = gmail.users().messages().list(
            userId="me", q=bills_query, maxResults=15
        ).execute()
        bills_messages = bills_results.get("messages", [])

        # Dedupe by message ID
        seen_ids = {m["id"] for m in messages}
        for m in bills_messages:
            if m["id"] not in seen_ids:
                messages.append(m)
                seen_ids.add(m["id"])

        if not messages:
            logger.info("No recent Gmail messages found")
            return []

        # Fetch snippet + headers for each message
        email_summaries = []
        for msg in messages[:40]:
            msg_data = gmail.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()

            headers = {
                h["name"]: h["value"]
                for h in msg_data.get("payload", {}).get("headers", [])
            }
            snippet = msg_data.get("snippet", "")
            email_summaries.append(
                f"From: {headers.get('From', 'Unknown')}\n"
                f"Subject: {headers.get('Subject', '(no subject)')}\n"
                f"Preview: {snippet[:200]}"
            )

        emails_text = "\n---\n".join(email_summaries)
        logger.info(f"Fetched {len(email_summaries)} Gmail messages")

        # Use Claude to extract action items
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        prompt = f"""Review these personal emails from Dennis Stefanitsis's Gmail \
(dennis.stefanitsis@gmail.com). Today is {today}. This is his PERSONAL email, \
not his business email (Glacier Point Insurance is separate — skip any GP business items).

{emails_text}

Extract action items and sort them into tiers based on urgency. Rules:
- Only include things that require Dennis to DO something (reply, sign, pay, \
schedule, etc.)
- HIGHEST PRIORITY: upcoming bills, payments, auto-withdrawals, and due dates — \
anything that could result in a late fee or penalty if missed (property taxes, \
estimated taxes, utility bills, insurance premiums, subscriptions, tuition, etc.)
- Flag emails from: IRS, FTB, Contra Costa County, Placer County, NJ Courts, \
Chase mortgage, PURE Insurance, Fidelity "Action Needed", or any tax/CPA firm
- Flag kids activity registration deadlines (LMYA, Springbrook, Eclipse, Luna, etc.)
- Flag school forms/fees that need payment or signatures by a deadline
- Skip: newsletters, promotions, payment confirmations for already-paid bills, \
notifications that don't need action
- Skip: PTA meeting invites — Dennis does not attend PTA meetings
- Skip: Glacier Point / insurance business emails — handled separately
- Skip: auto-pay confirmations where no action is needed (e.g., "thanks for your payment")
- Each action item should be one concise sentence with due date/amount when available

Return a JSON object with three arrays:
- "due_now": items due within 3 days or overdue (max 5)
- "this_week": items due within 7 days (max 5)
- "on_radar": items 7-14 days out, just for awareness (max 3)

If no items in a tier, use an empty array. If nothing at all, return \
{{"due_now": [], "this_week": [], "on_radar": []}}
Return ONLY the JSON object, nothing else.

Example: {{"due_now": ["Chase credit card — $3,532 due tomorrow 04/14"], \
"this_week": ["Eclipse tryout registration closes Friday — sign up Sophia"], \
"on_radar": ["PG&E autopay ~$523 scheduled 04/03"]}}"""

        client = anthropic.Anthropic(api_key=anthropic_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = _strip_code_fences(message.content[0].text)

        # Parse the JSON object with tiered items
        result = json.loads(response_text)
        # Normalize — ensure all keys exist
        tiered = {
            "due_now": result.get("due_now", [])[:5],
            "this_week": result.get("this_week", [])[:5],
            "on_radar": result.get("on_radar", [])[:3],
        }
        total = sum(len(v) for v in tiered.values())
        logger.info(
            f"Action items: {len(tiered['due_now'])} due now, "
            f"{len(tiered['this_week'])} this week, "
            f"{len(tiered['on_radar'])} on radar ({total} total)"
        )
        return tiered

    except Exception as e:
        logger.error(f"Gmail action items failed: {e}")
        return {}


def get_carpool_updates() -> str:
    """Scan Gmail for replies to itinerary emails containing carpool info.

    Looks for recent replies (last 24h) to "Family Itinerary" emails
    from Amy or Dennis. Returns raw text for the AI enrichment prompt.

    Returns:
        Carpool context string, or "" if none found.
    """
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    gmail_client_id = os.environ.get("GMAIL_CLIENT_ID")
    gmail_client_secret = os.environ.get("GMAIL_CLIENT_SECRET")

    if not all([refresh_token, gmail_client_id, gmail_client_secret]):
        return ""

    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=gmail_client_id,
            client_secret=gmail_client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        gmail = build("gmail", "v1", credentials=creds)

        # Search for replies to itinerary emails in the last 24 hours
        now = datetime.now(TIMEZONE)
        after_date = (now - timedelta(hours=24)).strftime("%Y/%m/%d")
        query = (
            f"after:{after_date} subject:\"Family Itinerary\" "
            f"(from:amylynnfischer@gmail.com OR "
            f"from:dennis.stefanitsis@gmail.com) "
            f"-from:{SENDER}"
        )

        results = gmail.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()
        messages = results.get("messages", [])

        if not messages:
            return ""

        # Fetch body text of each reply
        reply_texts = []
        for msg in messages:
            msg_data = gmail.users().messages().get(
                userId="me", id=msg["id"], format="full",
            ).execute()

            # Extract plain text body
            body = _extract_email_body(msg_data)
            if body:
                headers = {
                    h["name"]: h["value"]
                    for h in msg_data.get("payload", {}).get("headers", [])
                }
                sender = headers.get("From", "Unknown")
                reply_texts.append(f"From: {sender}\n{body[:500]}")

        if not reply_texts:
            return ""

        carpool_text = "\n---\n".join(reply_texts)
        logger.info(
            f"Found {len(reply_texts)} itinerary reply(s) with "
            f"potential carpool info"
        )
        return carpool_text

    except Exception as e:
        logger.warning(f"Carpool reply scan failed: {e}")
        return ""


def _extract_email_body(msg_data: dict) -> str:
    """Extract plain text body from a Gmail message payload.

    Handles both simple messages and multipart MIME structures.
    """
    import base64

    payload = msg_data.get("payload", {})

    # Simple message with body directly
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(
            payload["body"]["data"]
        ).decode("utf-8", errors="replace")

    # Multipart — look for text/plain
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode(
                    "utf-8", errors="replace"
                )
        # Nested multipart
        for sub in part.get("parts", []):
            if sub.get("mimeType") == "text/plain":
                data = sub.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode(
                        "utf-8", errors="replace"
                    )

    return ""


def enrich_events(events: list[dict], carpool_context: str = "") -> None:
    """Add practical context notes to sparse calendar events.

    Uses Claude to match each event against family_context.md and add
    a short 'note' field with actionable info (address, what to bring,
    pickup times, etc.). Modifies events in-place.

    Args:
        events: List of event dicts — each gets a 'note' key added.
    """
    if not events:
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return

    # Build a numbered list of events for the prompt
    event_lines = []
    for i, e in enumerate(events):
        if e["all_day"]:
            time_str = "All Day"
        else:
            start = e["start"]
            if hasattr(start, "astimezone"):
                start = start.astimezone(TIMEZONE)
            time_str = start.strftime("%-I:%M %p")
        loc = f" at {e['location']}" if e.get("location") else ""
        desc = e.get("description", "").strip()
        desc_snippet = f" [{desc[:120]}]" if desc else ""
        event_lines.append(f"{i}: {time_str} — {e['summary']}{loc}{desc_snippet}")

    events_text = "\n".join(event_lines)

    # Build carpool section if replies were found
    carpool_section = ""
    if carpool_context:
        carpool_section = (
            "\n\nCARPOOL REPLIES — family members replied to a previous "
            "itinerary email with logistics updates. Match these to the "
            "correct events above:\n"
            f"{carpool_context}\n"
        )

    prompt = f"""You are enriching calendar events for a family daily itinerary email.

Here is the family context file with known details about people, activities, \
locations, doctors, sports teams, etc.:

{FAMILY_CONTEXT}

Here are today's calendar events (index: time — title [description if any]):
{events_text}
{carpool_section}
For EACH event, provide three things:

1. **location**: ONLY if the event has NO location (no "at ..." shown above). \
Use the family context to fill it in with a short venue name + city, \
e.g., "Springbrook Pool, Lafayette" or "Luna Gymnastics, Moraga". \
CRITICAL: If the event already has a location from the calendar, return null — \
the calendar location is authoritative and must NOT be overridden or \
contradicted by family context. Practice locations vary by week.

2. **note**: A short practical tip (max 15 words) — what to bring, contact info, \
pickup details, carpool info. NEVER mention a location in the note if the event \
already has one from the calendar. Do NOT contradict the calendar location.
CARPOOL PRIORITY: If the event description contains carpool info (who is driving, \
pickup time/location), ALWAYS surface it in the note — this is the most important \
detail for parents. Format: "Carpool: [Parent] driving" or "Carpool: [Parent] \
picks up at [time]". Examples:
- "Carpool: Sarah's mom driving. Bring suit, goggles."
- "Carpool: Amy drops off, Johnson family picks up."
- "Bring suit, goggles, cap."
- "(925) 944-5151. Dr. Milcovich."
- "Coach Wayne. Super Stars class."
- "Pickup at 1:20 PM"
Set to null if no useful tip beyond the location.

3. **person**: Which family member this event is for. Return one of: \
"anna", "sophia", "dennis", "amy", or "" if shared/family.
Key mappings from context:
- Luna/gymnastics → "sophia"
- Swim/Springbrook → "sophia"
- LAMO/Luis → "anna" (Luis is Anna's LAMO soccer coach)
- Eclipse → "sophia"
- 4th grade volleyball → "sophia"
- 6th grade volleyball → "anna"

Return ONLY a JSON object mapping event index (as string) to an object \
with "location" (string or null), "note" (string or null), and "person" (string).
Example: {{"0": {{"location": "Springbrook Pool, Lafayette", "note": "Bring suit, goggles, cap.", "person": "sophia"}}, \
"1": {{"location": null, "note": null, "person": "dennis"}}}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = _strip_code_fences(message.content[0].text)
        results = json.loads(response_text)

        for idx_str, value in results.items():
            idx = int(idx_str)
            if 0 > idx or idx >= len(events):
                continue

            # Support both old format (string) and new format (object)
            if isinstance(value, str):
                # Old format: just a note string
                if value:
                    events[idx]["note"] = value
            elif isinstance(value, dict):
                # AI-provided location fills gaps from calendar
                ai_loc = value.get("location")
                if ai_loc and not events[idx].get("location"):
                    events[idx]["location"] = ai_loc

                note = value.get("note")
                if note:
                    events[idx]["note"] = note

                # AI person assignment always overrides the fallback
                person = value.get("person")
                if person is not None:
                    events[idx]["person"] = person

        enriched = sum(1 for e in events if e.get("note"))
        located = sum(1 for e in events if e.get("location"))
        assigned = sum(1 for e in events if e.get("person"))
        logger.info(
            f"Enriched {enriched}/{len(events)} notes, "
            f"{located} with locations, {assigned} with person tags"
        )

    except Exception as e:
        logger.error(f"Event enrichment failed: {e}")


def _format_events_for_prompt(events: list[dict]) -> str:
    """Format today's events as text lines for the AI prompt."""
    lines = []
    for e in events:
        if e["all_day"]:
            time_str = "All Day"
        else:
            # Convert to Pacific before formatting for the prompt
            start = e["start"]
            if hasattr(start, "astimezone"):
                start = start.astimezone(TIMEZONE)
            time_str = start.strftime("%-I:%M %p")
        loc = f" at {e['location']}" if e.get("location") else ""
        lines.append(
            f"- [{e['calendar']}] {time_str}: {e['summary']}{loc}"
        )
    return "\n".join(lines) if lines else "No events today."


def generate_summary(events: list[dict], weather: dict) -> str:
    """Use Claude to generate a concise family day summary.

    Focused on today only — who has what, weather nudges. Week-ahead
    and birthdays are handled separately in the template.

    Args:
        events: Today's calendar events
        weather: Weather data dict

    Returns:
        AI-generated summary string (plain text, 2-4 sentences)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — skipping AI summary")
        return ""

    today = datetime.now(TIMEZONE)
    events_text = _format_events_for_prompt(events)

    weather_text = ""
    if weather:
        weather_text = (
            f"Weather: {weather['description']}, currently {weather['temp']}°F, "
            f"high {weather['high']}°, low {weather['low']}°. "
            f"Rain chance: {weather['rain_chance']}%. "
            f"UV index: {weather['uv_index']}."
        )

    prompt = f"""{FAMILY_CONTEXT}

Today is {today.strftime('%A, %B %-d, %Y')}.
{weather_text}

Today's calendar events (tagged by source calendar — Dennis, Amy, or Family):
{events_text}

Write exactly 2-4 sentences summarizing this family's day. Rules:
- CRITICAL: Use the EXACT times from the events above. Do not change, round, \
or guess event times. If an event says 10:00 AM, say 10:00 AM — not 5:00 PM.
- If a child's name (Anna or Sophia) appears in the event title, that event \
is specifically for that child. Say "Sophia has..." or "Anna has..." — do not \
say "the kids" unless both names appear or it's clearly a family event.
- Dennis's calendar = Dennis's events. Amy's calendar = Amy's events. \
Family calendar = shared or kids events.
- Mention the weather only if it's noteworthy (very hot, cold, rainy). Don't \
comment on normal pleasant weather.
- If rain chance >30%: mention grabbing an umbrella or rain jacket.
- If high temp >85°F: mention sunscreen.
- If low temp <50°F: mention layering up or grabbing a jacket.
- If no events: say it's an open day and suggest something brief and fun.
- Tone: warm, casual, like a note left on the kitchen counter. No greetings, \
no sign-offs, no emojis, no bullet points. Just flowing sentences."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = message.content[0].text.strip()
        logger.info("AI summary generated")
        return summary
    except Exception as e:
        logger.error(f"AI summary failed: {e}")
        return ""


def format_event_time(event: dict) -> str:
    """Format an event's time for display."""
    if event["all_day"]:
        return "All Day"
    start = event["start"]
    if hasattr(start, "astimezone"):
        start = start.astimezone(TIMEZONE)
    return start.strftime("%-I:%M %p")


def format_week_event_day(event: dict) -> str:
    """Format an event's day and date for the week-ahead section."""
    dt = event["start"]
    if hasattr(dt, "astimezone"):
        dt = dt.astimezone(TIMEZONE)
    return dt.strftime("%a %-m/%-d")


HOME_ADDRESS = "1019 Walnut Drive, Lafayette, CA"


def _suggest_dinner_time(events: list[dict]) -> dict | None:
    """Suggest a dinner time based on when the last family event ends.

    Uses AI to estimate drive time from the last event's location
    back to home (1019 Walnut Dr, Lafayette). Rounds up to nearest
    15 minutes. Defaults to 6:00 PM if no evening activities.

    Returns:
        Dict with 'time' (str like "6:30 PM"), 'reason' (str),
        or None if no events at all.
    """
    if not events:
        return {"time": "6:00 PM", "reason": "Open evening — no activities"}

    # Find the latest non-all-day event end time
    latest_end = None
    latest_event = None
    for e in events:
        if e.get("all_day"):
            continue
        end = e["end"]
        if hasattr(end, "astimezone"):
            end = end.astimezone(TIMEZONE)
        if latest_end is None or end > latest_end:
            latest_end = end
            latest_event = e

    if latest_end is None:
        return {"time": "6:00 PM", "reason": "No timed events today"}

    # Estimate drive time from last event location to home
    drive_minutes = _estimate_drive_time(latest_event)

    dinner_dt = latest_end + timedelta(minutes=drive_minutes)

    # Round up to nearest 15 minutes
    minute = dinner_dt.minute
    remainder = minute % 15
    if remainder > 0:
        dinner_dt += timedelta(minutes=15 - remainder)
    dinner_dt = dinner_dt.replace(second=0, microsecond=0)

    # Clamp: no earlier than 5:30 PM, no later than 8:30 PM
    early = dinner_dt.replace(hour=17, minute=30)
    late = dinner_dt.replace(hour=20, minute=30)
    if dinner_dt < early:
        dinner_dt = early
        reason = "Everyone home early"
    elif dinner_dt > late:
        dinner_dt = late
        end_str = latest_end.strftime("%-I:%M %p")
        reason = f"Late night — last event ends {end_str}"
    else:
        who = latest_event.get("person", "").capitalize() or "Last event"
        end_str = latest_end.strftime("%-I:%M %p")
        loc = latest_event.get("location", "")
        if drive_minutes > 15:
            reason = f"{who} done at {end_str} + ~{drive_minutes} min drive"
        else:
            reason = f"{who} done at {end_str}"

    return {
        "time": dinner_dt.strftime("%-I:%M %p"),
        "reason": reason,
    }


# Known drive times (minutes) from common locations to home in Lafayette
_DRIVE_TIMES = {
    "lamo arena": 10,
    "moraga": 10,
    "luna gymnastics": 10,
    "wilder": 8,
    "stanley": 5,
    "orinda": 10,
    "lafayette": 5,
    "springbrook": 5,
    "camino pablo": 5,
    "lafayette elementary": 5,
    "lafayette cc": 5,
    "walnut creek": 12,
    "milcovich": 12,
    "concord": 20,
    "pleasant hill": 15,
}


def _estimate_drive_time(event: dict) -> int:
    """Estimate drive time in minutes from an event location to home.

    Uses a lookup table of known local venues. Falls back to 15 min
    for unknown locations.
    """
    location = (event.get("location") or "").lower()
    summary = (event.get("summary") or "").lower()
    search = f"{location} {summary}"

    for keyword, minutes in _DRIVE_TIMES.items():
        if keyword in search:
            return minutes

    # Default: 15 minutes for unknown locations in the Lamorinda area
    return 15


def render_email(
    events: list[dict],
    week_ahead: list[dict],
    weather: dict,
    summary: str,
    special_dates: list[dict],
    action_items: dict[str, list[str]],
    travel: list[dict],
    evening_mode: bool = False,
    midday_mode: bool = False,
) -> str:
    """Render the HTML email using Jinja2 template.

    Args:
        events: Today's calendar events
        week_ahead: Events for the rest of the week
        weather: Weather data dict
        summary: AI-generated day summary
        special_dates: Upcoming birthdays/anniversaries
        action_items: Tiered action items dict with keys
            'due_now', 'this_week', 'on_radar'
        travel: Upcoming trips
        evening_mode: True for tomorrow preview
        midday_mode: True for afternoon update

    Returns:
        Rendered HTML string
    """
    today = datetime.now(TIMEZONE)
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("email.html")

    # Extract tiers (backwards-compatible with empty dict)
    due_now = action_items.get("due_now", []) if action_items else []
    this_week = action_items.get("this_week", []) if action_items else []
    on_radar = action_items.get("on_radar", []) if action_items else []

    # Mode-specific headers
    if evening_mode:
        tomorrow = today + timedelta(days=1)
        date_header = f"Tomorrow — {tomorrow.strftime('%A, %B %-d')}"
        title = "Evening Preview"
        schedule_label = "Tomorrow's Schedule"
        briefing_label = "Prep for Tomorrow"
    elif midday_mode:
        date_header = today.strftime("%A, %B %-d")
        title = "Midday Update"
        schedule_label = "This Afternoon"
        briefing_label = "Afternoon Check-In"
    else:
        date_header = today.strftime("%A, %B %-d")
        title = "Stefanitsis Family Itinerary"
        schedule_label = "Today's Schedule"
        briefing_label = "Today's Briefing"

    return template.render(
        date_header=date_header,
        title=title,
        schedule_label=schedule_label,
        briefing_label=briefing_label,
        year=today.strftime("%Y"),
        events=events,
        week_ahead=week_ahead,
        weather=weather,
        weather_grab=_get_weather_grab(weather),
        summary=summary,
        special_dates=special_dates,
        action_items_due_now=due_now,
        action_items_this_week=this_week,
        action_items_on_radar=on_radar,
        has_action_items=bool(due_now or this_week or on_radar),
        dinner=_suggest_dinner_time(events),
        travel=travel,
        location=LOCATION,
        format_time=format_event_time,
        format_day=format_week_event_day,
    )


def send_email(
    dennis_html: str,
    family_html: str,
    subject_override: str | None = None,
) -> None:
    """Send the itinerary email via SendGrid.

    Dennis gets the full version (with action items).
    Amy gets the family version (no action items).
    """
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY not set — cannot send email")
        return

    today = datetime.now(TIMEZONE)
    subject = subject_override or (
        f"Stefanitsis Family Itinerary — {today.strftime('%A, %B %-d')}"
    )

    sg = SendGridAPIClient(api_key)

    sends = [
        ("dennis.stefanitsis@gmail.com", dennis_html),
        ("amylynnfischer@gmail.com", family_html),
    ]

    for recipient, html in sends:
        message = Mail(
            from_email=Email(SENDER, "Family Itinerary"),
            to_emails=To(recipient),
            subject=subject,
            html_content=HtmlContent(html),
        )
        # Reply-To Dennis's personal Gmail so Amy can reply with
        # carpool updates that get picked up by the next run
        message.reply_to = Email("dennis.stefanitsis@gmail.com",
                                 "Family Itinerary")
        try:
            response = sg.send(message)
            logger.info(
                f"Sent to {recipient} — status {response.status_code}"
            )
        except Exception as e:
            logger.error(f"Failed to send to {recipient}: {e}")


def _get_tomorrow_events(service) -> list[dict]:
    """Fetch tomorrow's calendar events."""
    now = datetime.now(TIMEZONE)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = tomorrow + timedelta(days=1)
    return _fetch_events(service, tomorrow, end)


def generate_evening_summary(
    tomorrow_events: list[dict], weather: dict
) -> str:
    """Generate an evening briefing summary focused on tomorrow prep.

    Args:
        tomorrow_events: Tomorrow's calendar events
        weather: Weather data dict (tomorrow's forecast)

    Returns:
        AI-generated evening summary (plain text, 2-4 sentences)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    tomorrow = datetime.now(TIMEZONE) + timedelta(days=1)
    events_text = _format_events_for_prompt(tomorrow_events)

    weather_text = ""
    if weather:
        weather_text = (
            f"Tomorrow's weather: {weather['description']}, "
            f"high {weather['high']}°, low {weather['low']}°. "
            f"Rain chance: {weather['rain_chance']}%."
        )

    prompt = f"""{FAMILY_CONTEXT}

Tomorrow is {tomorrow.strftime('%A, %B %-d, %Y')}.
{weather_text}

Tomorrow's calendar events:
{events_text}

Write 2-4 sentences previewing tomorrow for the family. Focus on:
- What to prep tonight (pack bags, lay out clothes, set alarms)
- Early morning logistics if applicable
- Weather-related prep (rain gear by the door, sunscreen in backpacks)
- Any conflicts or tight transitions between events

Tone: warm, practical, like a note on the kitchen counter. \
No greetings, sign-offs, emojis, or bullet points. Flowing sentences."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Evening summary failed: {e}")
        return ""


def _get_rest_of_day_events(service) -> list[dict]:
    """Fetch events from now through end of today."""
    now = datetime.now(TIMEZONE)
    end_of_day = now.replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    return _fetch_events(service, now, end_of_day)


def generate_midday_summary(
    events: list[dict], weather: dict, carpool_context: str
) -> str:
    """Generate a midday update summary focused on the afternoon ahead.

    Args:
        events: Remaining events for today
        weather: Current weather data
        carpool_context: Any carpool replies received since morning

    Returns:
        AI-generated midday summary (plain text, 2-4 sentences)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    today = datetime.now(TIMEZONE)
    events_text = _format_events_for_prompt(events)

    weather_text = ""
    if weather:
        weather_text = (
            f"Current weather: {weather['description']}, "
            f"{weather['temp']}°F. "
            f"Rest of day: high {weather['high']}°, low {weather['low']}°. "
            f"Rain chance: {weather['rain_chance']}%."
        )

    carpool_note = ""
    if carpool_context:
        carpool_note = (
            "\n\nCarpool updates received since the morning email:\n"
            f"{carpool_context}\n"
            "Incorporate any carpool changes into your summary."
        )

    prompt = f"""{FAMILY_CONTEXT}

It is {today.strftime('%A, %B %-d, %Y')} at {today.strftime('%-I:%M %p')}.
{weather_text}

Remaining events today:
{events_text}
{carpool_note}

Write 2-4 sentences as a midday check-in for the family. Focus on:
- What's coming up this afternoon/evening
- Any carpool changes or logistics updates that came in
- Weather changes from this morning (rain moving in, temp swings)
- Tight transitions between after-school events

Tone: warm, casual, like a quick text to the family. No greetings, \
sign-offs, emojis, or bullet points. Flowing sentences."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Midday summary failed: {e}")
        return ""


def main(mode: str = "morning") -> None:
    """Pull calendar + weather, generate AI summary, render and send.

    Args:
        mode: 'morning', 'midday', or 'evening' briefing.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logger.info(f"Running {mode} briefing...")
    service = _build_calendar_service()

    # Scan for carpool replies from Amy/Dennis to previous itineraries
    logger.info("Checking for carpool replies...")
    carpool_context = get_carpool_updates()
    if carpool_context:
        logger.info("Found carpool updates from email replies")

    if mode == "evening":
        # Evening briefing — focused on tomorrow
        logger.info("Fetching tomorrow's events...")
        tomorrow_events = _get_tomorrow_events(service) if service else []
        logger.info(f"Found {len(tomorrow_events)} event(s) tomorrow")

        logger.info("Enriching events with family context...")
        enrich_events(tomorrow_events, carpool_context=carpool_context)

        logger.info("Fetching weather...")
        weather = get_weather()

        logger.info("Generating evening summary...")
        summary = generate_evening_summary(tomorrow_events, weather)

        # Evening email: tomorrow's events as "today", no week-ahead
        dennis_html = render_email(
            tomorrow_events, week_ahead=[], weather=weather,
            summary=summary, special_dates=[],
            action_items={}, travel=[],
            evening_mode=True,
        )
        family_html = dennis_html  # Same version for both

        today = datetime.now(TIMEZONE)
        tomorrow = today + timedelta(days=1)
        subject = (
            f"Tomorrow's Preview — {tomorrow.strftime('%A, %B %-d')}"
        )
        send_email(dennis_html, family_html, subject_override=subject)

    elif mode == "midday":
        # Midday update — rest of today + carpool changes
        logger.info("Fetching remaining events today...")
        events = _get_rest_of_day_events(service) if service else []
        logger.info(f"Found {len(events)} event(s) remaining today")

        logger.info("Enriching events with family context...")
        enrich_events(events, carpool_context=carpool_context)

        logger.info("Fetching weather...")
        weather = get_weather()

        logger.info("Generating midday summary...")
        summary = generate_midday_summary(events, weather, carpool_context)

        today = datetime.now(TIMEZONE)
        dennis_html = render_email(
            events, week_ahead=[], weather=weather,
            summary=summary, special_dates=[],
            action_items={}, travel=[],
            evening_mode=False,
            midday_mode=True,
        )
        family_html = dennis_html  # Same version for both

        subject = (
            f"Midday Update — {today.strftime('%A, %B %-d')}"
        )
        send_email(dennis_html, family_html, subject_override=subject)

    else:
        # Morning briefing — existing behavior
        logger.info("Fetching today's events...")
        events = get_today_events(service) if service else []
        logger.info(f"Found {len(events)} event(s) today")

        logger.info("Fetching week-ahead events...")
        week_ahead = get_week_ahead_events(service) if service else []
        logger.info(f"Found {len(week_ahead)} event(s) rest of week")

        logger.info("Enriching events with family context...")
        enrich_events(events, carpool_context=carpool_context)
        enrich_events(week_ahead, carpool_context=carpool_context)

        logger.info("Checking upcoming birthdays/anniversaries...")
        special_dates = get_upcoming_special_dates()
        if special_dates:
            logger.info(
                f"Upcoming: {', '.join(s['label'] for s in special_dates)}"
            )

        logger.info("Fetching weather...")
        weather = get_weather()
        if weather:
            logger.info(
                f"{weather['description']} — {weather['temp']}°F "
                f"(H: {weather['high']}° / L: {weather['low']}°, "
                f"rain: {weather['rain_chance']}%, UV: {weather['uv_index']})"
            )

        logger.info("Checking upcoming travel...")
        travel = get_upcoming_travel(service)

        logger.info("Scanning personal Gmail for action items...")
        action_items = get_gmail_action_items()

        logger.info("Generating AI summary...")
        summary = generate_summary(events, weather)

        # Dennis gets the full version with action items
        dennis_html = render_email(
            events, week_ahead, weather, summary,
            special_dates, action_items, travel,
        )
        # Amy gets the family version without action items
        family_html = render_email(
            events, week_ahead, weather, summary,
            special_dates, action_items={}, travel=travel,
        )
        send_email(dennis_html, family_html)

    logger.info("Done.")


if __name__ == "__main__":
    import sys
    run_mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    main(mode=run_mode)
