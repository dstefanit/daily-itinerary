"""
Stefanitsis Family Itinerary — pulls Google Calendar events and weather,
uses AI to summarize the day with practical nudges, sends a formatted
morning email with week-ahead preview and upcoming birthdays.
"""

import os
import json
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests
from google.oauth2 import service_account
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

RECIPIENTS = [
    "dennis.stefanitsis@gmail.com",
    "amylynnfischer@gmail.com",
]
SENDER = "dennis@glacierpointinsurance.agency"

# Calendar ID → label for organizing events
CALENDARS = {
    "dennis.stefanitsis@gmail.com": "Dennis",
    "amylynnfischer@gmail.com": "Amy",
    "family05389224298174643941@group.calendar.google.com": "Family",
}

# Family context for the AI summary
FAMILY_CONTEXT = (
    "The Stefanitsis family: Dennis (dad), Amy (mom), "
    "Anna (12 year old daughter), Sophia (10 year old daughter). "
    "They live in Lafayette, CA."
)

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
SPECIAL_DATE_LOOKAHEAD_DAYS = 30


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

            for event in result.get("items", []):
                start = event["start"]
                end = event["end"]

                all_day = "date" in start
                if all_day:
                    start_dt = datetime.strptime(start["date"], "%Y-%m-%d")
                    end_dt = datetime.strptime(end["date"], "%Y-%m-%d")
                else:
                    start_dt = datetime.fromisoformat(start["dateTime"])
                    end_dt = datetime.fromisoformat(end["dateTime"])

                all_events.append({
                    "start": start_dt,
                    "end": end_dt,
                    "summary": event.get("summary", "(No title)"),
                    "location": event.get("location", ""),
                    "all_day": all_day,
                    "calendar": cal_label,
                })
        except Exception as e:
            logger.error(f"Failed to fetch calendar {cal_label}: {e}")

    all_events.sort(key=lambda e: (not e["all_day"], e["start"]))
    return _dedup_events(all_events)


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
        url = "https://api.openweathermap.org/data/3.0/onecall"
        params = {
            "lat": LAT,
            "lon": LON,
            "appid": api_key,
            "units": "imperial",
            "exclude": "minutely,alerts",
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        current = data["current"]
        today_forecast = data["daily"][0]

        rain_chance = round(today_forecast.get("pop", 0) * 100)
        uv_index = current.get("uvi", 0)

        return {
            "temp": round(current["temp"]),
            "high": round(today_forecast["temp"]["max"]),
            "low": round(today_forecast["temp"]["min"]),
            "description": current["weather"][0]["description"].title(),
            "icon": current["weather"][0]["icon"],
            "rain_chance": rain_chance,
            "uv_index": round(uv_index),
        }
    except Exception as e:
        logger.error(f"Weather API failed: {e}")
        return {}


def _format_events_for_prompt(events: list[dict]) -> str:
    """Format today's events as text lines for the AI prompt."""
    lines = []
    for e in events:
        time_str = "All Day" if e["all_day"] else e["start"].strftime("%-I:%M %p")
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
- Assign each event to the most likely family member based on the event name \
and which calendar it's on. Dennis's calendar = Dennis's events. Amy's calendar = \
Amy's events. Family calendar = shared/kids events.
- Mention the weather only if it's noteworthy (very hot, cold, rainy). Don't \
comment on normal pleasant weather.
- If rain chance >30%: mention grabbing an umbrella or rain jacket.
- If UV index ≥6 or high temp >85°F: mention sunscreen.
- If low temp <50°F: mention layering up or grabbing a jacket.
- If no events: say it's an open day and suggest something brief and fun.
- Tone: warm, casual, like a note left on the kitchen counter. No greetings, \
no sign-offs, no emojis, no bullet points. Just flowing sentences."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
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
    """Format an event's day name for the week-ahead section."""
    dt = event["start"]
    if hasattr(dt, "astimezone"):
        dt = dt.astimezone(TIMEZONE)
    return dt.strftime("%A")


def render_email(
    events: list[dict],
    week_ahead: list[dict],
    weather: dict,
    summary: str,
    special_dates: list[dict],
) -> str:
    """Render the HTML email using Jinja2 template.

    Args:
        events: Today's calendar events
        week_ahead: Events for the rest of the week
        weather: Weather data dict
        summary: AI-generated day summary
        special_dates: Upcoming birthdays/anniversaries

    Returns:
        Rendered HTML string
    """
    today = datetime.now(TIMEZONE)
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("email.html")

    return template.render(
        date_header=today.strftime("%A, %B %-d"),
        year=today.strftime("%Y"),
        events=events,
        week_ahead=week_ahead,
        weather=weather,
        summary=summary,
        special_dates=special_dates,
        location=LOCATION,
        format_time=format_event_time,
        format_day=format_week_event_day,
    )


def send_email(html: str) -> None:
    """Send the itinerary email via SendGrid."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY not set — cannot send email")
        return

    today = datetime.now(TIMEZONE)
    subject = (
        f"Stefanitsis Family Itinerary — {today.strftime('%A, %B %-d')}"
    )

    sg = SendGridAPIClient(api_key)

    for recipient in RECIPIENTS:
        message = Mail(
            from_email=Email(SENDER, "Family Itinerary"),
            to_emails=To(recipient),
            subject=subject,
            html_content=HtmlContent(html),
        )
        try:
            response = sg.send(message)
            logger.info(
                f"Sent to {recipient} — status {response.status_code}"
            )
        except Exception as e:
            logger.error(f"Failed to send to {recipient}: {e}")


def main() -> None:
    """Pull calendar + weather, generate AI summary, render and send."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    service = _build_calendar_service()

    logger.info("Fetching today's events...")
    events = get_today_events(service) if service else []
    logger.info(f"Found {len(events)} event(s) today")

    logger.info("Fetching week-ahead events...")
    week_ahead = get_week_ahead_events(service) if service else []
    logger.info(f"Found {len(week_ahead)} event(s) rest of week")

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

    logger.info("Generating AI summary...")
    summary = generate_summary(events, weather)

    html = render_email(events, week_ahead, weather, summary, special_dates)
    send_email(html)
    logger.info("Done.")


if __name__ == "__main__":
    main()
