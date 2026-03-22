"""
Daily Itinerary — pulls Google Calendar events and weather,
sends a formatted morning email to Dennis and Amy.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from jinja2 import Environment, FileSystemLoader
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content, HtmlContent

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

# Google Calendar IDs to pull from
# Add Amy's calendar ID here once she shares it with the service account
CALENDAR_IDS = [
    "dennis.stefanitsis@gmail.com",
]


def get_calendar_events() -> list[dict]:
    """Fetch today's events from all configured Google Calendars.

    Returns:
        List of event dicts with keys: start, end, summary, location, all_day
    """
    # Load service account credentials from env var (JSON string)
    sa_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not sa_key:
        logger.warning("GOOGLE_SERVICE_ACCOUNT_KEY not set — skipping calendar")
        return []

    key_data = json.loads(sa_key)
    creds = service_account.Credentials.from_service_account_info(
        key_data, scopes=["https://www.googleapis.com/auth/calendar.readonly"]
    )
    service = build("calendar", "v3", credentials=creds)

    # Today midnight-to-midnight in Pacific
    now = datetime.now(TIMEZONE)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    all_events = []
    for cal_id in CALENDAR_IDS:
        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            for event in result.get("items", []):
                start = event["start"]
                end = event["end"]

                # All-day events use 'date', timed events use 'dateTime'
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
                })
        except Exception as e:
            logger.error(f"Failed to fetch calendar {cal_id}: {e}")

    # Sort by start time, all-day events first
    all_events.sort(key=lambda e: (not e["all_day"], e["start"]))
    return all_events


def get_weather() -> dict:
    """Fetch current weather and daily forecast for Lafayette, CA.

    Returns:
        Dict with keys: temp, high, low, description, icon
    """
    api_key = os.environ.get("OPENWEATHERMAP_API_KEY")
    if not api_key:
        logger.warning("OPENWEATHERMAP_API_KEY not set — skipping weather")
        return {}

    try:
        # One Call API 3.0 — current + daily forecast
        url = "https://api.openweathermap.org/data/3.0/onecall"
        params = {
            "lat": LAT,
            "lon": LON,
            "appid": api_key,
            "units": "imperial",
            "exclude": "minutely,hourly,alerts",
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        current = data["current"]
        today_forecast = data["daily"][0]

        return {
            "temp": round(current["temp"]),
            "high": round(today_forecast["temp"]["max"]),
            "low": round(today_forecast["temp"]["min"]),
            "description": current["weather"][0]["description"].title(),
            "icon": current["weather"][0]["icon"],
        }
    except Exception as e:
        logger.error(f"Weather API failed: {e}")
        return {}


def format_event_time(event: dict) -> str:
    """Format an event's time for display."""
    if event["all_day"]:
        return "All Day"
    start = event["start"]
    if hasattr(start, "astimezone"):
        start = start.astimezone(TIMEZONE)
    return start.strftime("%-I:%M %p")


def render_email(events: list[dict], weather: dict) -> str:
    """Render the HTML email using Jinja2 template.

    Args:
        events: List of calendar event dicts
        weather: Weather data dict

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
        location=LOCATION,
        format_time=format_event_time,
    )


def send_email(html: str) -> None:
    """Send the itinerary email via SendGrid.

    Args:
        html: Rendered HTML email body
    """
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY not set — cannot send email")
        return

    today = datetime.now(TIMEZONE)
    subject = f"Your Day — {today.strftime('%A, %B %-d')}"

    sg = SendGridAPIClient(api_key)

    for recipient in RECIPIENTS:
        message = Mail(
            from_email=Email(SENDER, "Daily Itinerary"),
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
    """Pull calendar + weather, render email, send to all recipients."""
    # Support loading .env file locally (python-dotenv optional)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logger.info("Fetching calendar events...")
    events = get_calendar_events()
    logger.info(f"Found {len(events)} event(s)")

    logger.info("Fetching weather...")
    weather = get_weather()
    if weather:
        logger.info(
            f"{weather['description']} — {weather['temp']}°F "
            f"(H: {weather['high']}° / L: {weather['low']}°)"
        )

    html = render_email(events, weather)
    send_email(html)
    logger.info("Done.")


if __name__ == "__main__":
    main()
