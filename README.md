# Scout

A personal Telegram bot that watches Instagram accounts announcing vintage markets, house-clearance sales and estate sales in Madrid, and alerts you when a new one appears.

- Reads posts, bios and stories through Apify (no Instagram login).
- Gemini turns each caption and flyer into a structured event: dates, opening sessions, address, booking method, booking opening time.
- Teasers, announcements, reminders and "last day" posts for one market are merged into a single event, so you get one alert.
- You answer ✅ Interested, 🤔 Maybe or ❌ No; Maybe events notify you again when the date or address is confirmed.

Design document: see the "Scout — Design Document" in the scout project.

## Commands

| Command | What it does |
| --- | --- |
| `/add @handle` | Follow an account (or paste its profile link) |
| `/remove` | Stop following one |
| `/list` | Accounts followed and when each was last read |
| `/upcoming` · `/maybe` | Your ✅ and 🤔 markets, grouped by week |
| `/check` | Read all accounts now |
| `/pause` · `/resume` | Mute and unmute alerts |
| `/status` | Last check, failing accounts, Apify spend |

## Schedule (Europe/Madrid)

- Posts and bios: 08:02, 11:02, 14:02, 17:02, 20:02, 23:02 (`POLL_HOURS`)
- Stories: 21:07 daily (stories live 24 h)
- Upkeep: 04:00 (past events, image pruning)

About $3.80/month of Apify credit for 6 accounts, inside the free $5.

## Deploy (Dokploy)

Service `scout-bot` in project **scout**: Git source `https://github.com/ginacassin/scout.git` (`main`), Dockerfile build, domain `https://bots.rabbia.net/scoutbot` → port 8000, volume `scout-data` → `/data`. Environment variables are listed in `.env.example`. After pushing, click **Deploy**.

## Development

```
pip install -r requirements.txt pytest pytest-asyncio
pytest                                  # offline tests, real post sequences
GEMINI_API_KEY=… python scripts/eval_parser.py   # score Gemini on the 26 real cases
```

Milestones: **M1** alerts and account management (this) · M2 Google Calendar sync · M3 Eventbrite booking watcher · M4 auto-booking free tickets.
