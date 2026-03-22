# Stefanitsis Family Itinerary

Daily morning email with calendar events, weather, and an AI-generated summary for the Stefanitsis family (Dennis, Amy, Anna 12, Sophia 10) in Lafayette, CA.

## Stack
- Python, Google Calendar API (service account), OpenWeatherMap, Claude Haiku (AI summary), SendGrid, Jinja2
- Scheduled via GitHub Actions cron (6:30am PT daily)

## Calendars
| Calendar | ID |
|----------|-----|
| Dennis | `dennis.stefanitsis@gmail.com` |
| Amy | `amylynnfischer@gmail.com` |
| Family | `family05389224298174643941@group.calendar.google.com` |

## Running
```bash
cp .env.example .env   # Fill in keys
pip install -r requirements.txt
python itinerary.py
```

## Env Vars
| Variable | Source |
|----------|--------|
| `GOOGLE_SERVICE_ACCOUNT_KEY` | GCP service account JSON (single line) |
| `SENDGRID_API_KEY` | Shared from Glacier Point (TODO: separate) |
| `OPENWEATHERMAP_API_KEY` | openweathermap.org free tier |
| `ANTHROPIC_API_KEY` | console.anthropic.com |

## Files
| File | Purpose |
|------|---------|
| `itinerary.py` | Main script — calendar + weather + AI summary + render + send |
| `templates/email.html` | Jinja2 HTML email template |
| `.github/workflows/daily-send.yml` | GitHub Actions cron schedule |
