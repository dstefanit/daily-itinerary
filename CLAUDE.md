# Daily Itinerary

Morning email with today's calendar events and weather forecast for Dennis and Amy.

## Stack
- Python, Google Calendar API (service account), OpenWeatherMap, SendGrid, Jinja2
- Scheduled via GitHub Actions cron (6:30am PT daily)

## Running
```bash
cp .env.example .env   # Fill in keys
pip install -r requirements.txt
python itinerary.py
```

## Setup
1. Google Cloud Console → create project → enable Calendar API → create service account
2. Download service account JSON key → paste as single-line JSON into `GOOGLE_SERVICE_ACCOUNT_KEY`
3. Dennis shares Google Calendar with service account email (read-only)
4. Amy shares her calendar too (optional) → add her calendar ID to `CALENDAR_IDS` in `itinerary.py`
5. OpenWeatherMap → sign up → free API key → `OPENWEATHERMAP_API_KEY`
6. SendGrid API key — currently sharing Glacier Point's key (TODO: create a separate SendGrid account/key for this project)

## Files
| File | Purpose |
|------|---------|
| `itinerary.py` | Main script — calendar + weather + render + send |
| `templates/email.html` | Jinja2 HTML email template |
| `.github/workflows/daily-send.yml` | GitHub Actions cron schedule |
