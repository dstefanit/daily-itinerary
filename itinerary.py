"""
Stefanitsis Family Itinerary — pulls Google Calendar events and weather,
uses AI to summarize the day with practical nudges, sends a formatted
morning email.
"""

import os
import json
import logging
from datetime import datetime, timedelta
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
    return all_events


def get_today_events(service) -> list[dict]:
    """Fetch today's events (midnight-to-midnight Pacific)."""
    now = datetime.now(TIMEZONE)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    return _fetch_events(service, start_of_day, end_of_day)


def get_week_ahead_events(service) -> list[dict]:
    """Fetch events for the rest of the week (tomorrow through Sunday).

    Returns events grouped by day for the AI to summarize.
    """
    now = datetime.now(TIMEZONE)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    # Days until end of Sunday (weekday 6 = Sunday)
    days_until_sunday = 6 - now.weekday()
    if days_until_sunday <= 0:
        days_until_sunday = 7  # If today is Sunday, get next week
    end_of_week = tomorrow + timedelta(days=days_until_sunday)

    return _fetch_events(service, tomorrow, end_of_week)


def get_weather() -> dict:
    """Fetch current weather and daily forecast for Lafayette, CA.

    Returns:
        Dict with keys: temp, high, low, description, icon,
        rain_chance, conditions_detail
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

        # Rain probability (0-1 → percentage)
        rain_chance = round(today_forecast.get("pop", 0) * 100)

        # UV index for sunscreen nudge
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


def _format_events_for_prompt(events: list[dict], include_day: bool = False) -> str:
    """Format events as text lines for the AI prompt.

    Args:
        events: List of event dicts
        include_day: Whether to include the day name (for week-ahead)

    Returns:
        Formatted string of events
    """
    lines = []
    for e in events:
        time_str = "All Day" if e["all_day"] else e["start"].strftime("%-I:%M %p")
        loc = f" at {e['location']}" if e.get("location") else ""
        day_prefix = ""
        if include_day:
            day_dt = e["start"]
            if hasattr(day_dt, "astimezone"):
                day_dt = day_dt.astimezone(TIMEZONE)
            day_prefix = f"{day_dt.strftime('%A')} "
        lines.append(
            f"- [{e['calendar']}] {day_prefix}{time_str}: {e['summary']}{loc}"
        )
    return "\n".join(lines) if lines else "Nothing scheduled."


def generate_summary(
    events: list[dict],
    week_ahead: list[dict],
    weather: dict,
) -> str:
    """Use Claude to generate a friendly family day summary with nudges.

    Includes weather-based reminders (rain gear, sunscreen, jackets) and
    a brief look-ahead at the rest of the week.

    Args:
        events: Today's calendar events
        week_ahead: Events for the rest of the week
        weather: Weather data dict

    Returns:
        AI-generated summary string (plain text)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — skipping AI summary")
        return ""

    today = datetime.now(TIMEZONE)
    events_text = _format_events_for_prompt(events)
    week_text = _format_events_for_prompt(week_ahead, include_day=True)

    weather_text = ""
    if weather:
        weather_text = (
            f"Weather: {weather['description']}, currently {weather['temp']}°F, "
            f"high {weather['high']}°, low {weather['low']}°. "
            f"Rain chance: {weather['rain_chance']}%. "
            f"UV index: {weather['uv_index']}."
        )

    prompt = f"""You're writing a brief morning summary for the Stefanitsis family.

{FAMILY_CONTEXT}

Today is {today.strftime('%A, %B %-d, %Y')}.
{weather_text}

TODAY'S EVENTS (tagged by calendar — Dennis, Amy, or Family):
{events_text}

REST OF THE WEEK:
{week_text}

Write a morning note with these sections (use plain text, no markdown headers or \
formatting — just natural paragraph breaks):

1. TODAY (3-4 sentences): Summarize who has what today. Use your best judgment to \
figure out which family member each event is for based on the event name and calendar. \
Mention the weather briefly.

2. DON'T FORGET (1-2 short sentences, only if relevant): Practical nudges based on \
weather — e.g., "Grab an umbrella" if rain chance >30%, "Sunscreen for the kids" if \
UV≥6 or high>85°, "Bundle up" if low<50°. Skip this section entirely if weather is \
mild and clear.

3. LOOKING AHEAD (1-2 sentences): Mention anything notable coming up this week so \
the family can prep. If nothing stands out, skip this section.

Keep it conversational and warm — like a quick note on the kitchen counter. No \
greetings or sign-offs. Don't label the sections with headers, just flow naturally \
between them with a line break."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
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


def render_email(
    events: list[dict], weather: dict, summary: str
) -> str:
    """Render the HTML email using Jinja2 template.

    Args:
        events: List of calendar event dicts
        weather: Weather data dict
        summary: AI-generated day summary

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
        weather=weather,
        summary=summary,
        location=LOCATION,
        format_time=format_event_time,
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
    logger.info(f"Found {len(week_ahead)} event(s) this week")

    logger.info("Fetching weather...")
    weather = get_weather()
    if weather:
        logger.info(
            f"{weather['description']} — {weather['temp']}°F "
            f"(H: {weather['high']}° / L: {weather['low']}°, "
            f"rain: {weather['rain_chance']}%, UV: {weather['uv_index']})"
        )

    logger.info("Generating AI summary...")
    summary = generate_summary(events, week_ahead, weather)

    html = render_email(events, weather, summary)
    send_email(html)
    logger.info("Done.")


if __name__ == "__main__":
    main()
