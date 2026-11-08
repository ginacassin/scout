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
| `/add @handle` | Follow an account (or paste its profile link); reads its recent posts and current stories right away |
| `/remove` | Stop following one |
| `/list` | Accounts followed and when each was last read |
| `/upcoming` · `/maybe` | Your ✅ and 🤔 markets, grouped by week |
| `/check` | Read all accounts now (posts, bios and stories) |
| `/pause` · `/resume` | Mute and unmute alerts |
| `/status` | Last check, failing accounts, Apify spend |
| `/booked <id> <HH:MM> [date]` | You booked a slot yourself (date optional for one-day markets; buttons otherwise) |
| `/unbook <id>` | Clear a booking; the calendar gets all dates back |
| `/ticket <id>` | Send the QR/ticket PDF next (or send a photo/PDF captioned `/ticket <id>` any time) |
| `/note <id> <text>` | Remember something for a market (or just reply to its card with the text) |
| `/notes <id>` · `/delnote <id> <n>` | List or delete notes |
| `/today` · `/briefing [date]` | Briefing of the markets you're visiting that day |
| `/plan [date]` | Booked and armed markets in time order, with drive times |

Cards of ✅ events show **🎟 I booked it** (Eventbrite markets: pick the day and time) and **📝 Add note**.
Booked events show "🎟 Booked Fri 13 Nov 10:30 (by you/auto)" and notes appear on the card, in the
calendar description and in a market-day reminder (08:30, or 1 h before a booked slot) with the
address and a Google Maps link.

## Bookings and the calendar

Saying ✅ puts every day of the market in the Scout calendar (plus a "🎟 Book:" reminder at the
booking opening). As soon as a slot is booked — by the auto-booker or with `/booked` — those entries
are deleted and **one timed entry** remains: "🎟 Alma Market — booked 10:30", with the full address,
the Instagram and Eventbrite links, your notes, and who booked it. `/unbook` restores the dates.

### Ticket QR on the calendar entry (optional, one-time setup)

Google Calendar can only attach Google Drive files, and a service account has no Drive storage of
its own, so uploads must go into a **Shared Drive** that the service account is a member of:

1. Enable the **Google Drive API** in the Google Cloud project of the service account.
2. In Google Drive create a Shared Drive (needs Google Workspace) or a folder inside one, and add the
   service account email (`…@….iam.gserviceaccount.com`) as **Content manager**.
3. Copy the folder id from its URL (`drive.google.com/drive/folders/<id>`) into `GDRIVE_FOLDER_ID`.

Files are never made public: they are visible only to the Shared Drive's members. Scopes default to
`calendar.events drive.file`; if uploads fail with "File not found" for the folder, set
`GOOGLE_SCOPES=calendar.events drive`. With a personal @gmail.com account (no Shared Drives) or a
folder in your own My Drive, the upload fails with `storageQuotaExceeded`; Scout tells you once and
carries on. **Either way** the QR/PDF is sent to the chat as a reply to the event card and pinned,
and the calendar entry says "Ticket QR: in your Scout chat (pinned)".

## Data retention

Events are marked past the day after they end and **deleted 14 days after their last day**
(`RETENTION_DAYS`), together with their Eventbrite watch, images and ticket files; teasers that never
got a date go after 60 days. Instagram items older than 30 days keep only their id (so a post is
never processed twice) — caption, parsed data and images are dropped. Google Calendar entries stay
as history.

## Schedule (Europe/Madrid)

- Posts and bios: 08:02, 11:02, 14:02, 17:02, 20:02, 23:02 (`POLL_HOURS`)
- Stories: 09:07 and 21:07 daily (stories live 24 h, so two reads 12 h apart catch every one); also on `/add` for the new account, and once after a restart if the last read is over 12 h old
- Upkeep: 04:00 (past events, image pruning, retention cleanup)
- Morning briefing: 08:00 on days with ✅/booked markets (`BRIEFING_TIME`): time order, Instagram account, what's on sale, hours, address, notes, drive times, one Maps route
- Reminder 1 h before a booked slot
- Eventbrite login check for the auto-booker: 12:17 daily (warns once if logged out)

Apify cost: posts about $0.0027 per account per check (6 checks a day), stories $0.0025 per story found plus a small start fee per run. With 14 accounts that is roughly $9–11/month, above the free $5; Scout warns at 80 % of `APIFY_MONTHLY_BUDGET_USD`. Fewer `POLL_HOURS` is the cheapest lever.

## Deploy (Dokploy)

Service `scout-bot` in project **scout**: Git source `https://github.com/ginacassin/scout.git` (`main`), Dockerfile build, domain `https://bots.rabbia.net/scoutbot` → port 8000, volume `scout-data` → `/data`. Environment variables are listed in `.env.example`. After pushing, click **Deploy**.

## Development

```
pip install -r requirements.txt pytest pytest-asyncio
pytest                                  # offline tests, real post sequences
GEMINI_API_KEY=… python scripts/eval_parser.py   # score Gemini on the 26 real cases
```

Milestones: **M1** alerts and account management (this) · M2 Google Calendar sync · M3 Eventbrite booking watcher · M4 auto-booking free tickets.
