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
| `/note <id> <text>` | Remember something for a market (or reply to its card with text or a photo) |
| `/notes <id>` · `/delnote <id> <n>` | List or delete notes |
| `/today` · `/briefing [date]` | Briefing of the markets you're visiting that day |
| `/plan [date]` | Booked and armed markets in time order, with drive times |
| `/google` | Link your Google Drive so tickets and note photos are attached to calendar entries |
| `/hunt <id> [before HH:MM]` | Watch an Eventbrite market for released free tickets (any slot, or earlier than the one you booked) · `/hunt <id> off` · `/hunt` lists |

Cards of ✅ events show **🎟 I booked it** (Eventbrite markets: pick the day and time) and **📝 Add note**.
Booked events show "🎟 Booked Fri 13 Nov 10:30 (by you/auto)" and notes appear on the card, in the
calendar description and in a market-day reminder (08:30, or 1 h before a booked slot) with the
address and a Google Maps link.

## Bookings and the calendar

Saying ✅ puts every day of the market in the Scout calendar (plus a "🎟 Book:" reminder at the
booking opening). As soon as a slot is booked — by the auto-booker or with `/booked` — those entries
are deleted and **one timed entry** remains: "🎟 Alma Market — booked 10:30", with the full address,
the Instagram and Eventbrite links, your notes, and who booked it. `/unbook` restores the dates.

### Tickets and note photos on the calendar entry (`/google`, one-time setup)

Google Calendar can only attach Google Drive files, and a service account has no Drive storage on a
personal Gmail. So Scout puts them in **your** Drive (folder "Scout"), with the narrowest permission
there is — it only sees files it created — and attaches them to the calendar entries: the ticket QR
on the booked entry, note photos (⭐ sneak peeks, photos you sent) on every entry of the market.
Once linked, tickets are no longer pinned in the chat (they are only if an upload fails).

1. Google Cloud console, project of the calendar service account → **APIs & Services → Library**:
   enable **Google Drive API**.
2. **OAuth consent screen**: External, any app name, your email → **Publish app** (in "Testing" the
   link expires every 7 days; this scope needs no Google review).
3. **Credentials → Create credentials → OAuth client ID**, type **TVs and Limited Input devices**.
4. Dokploy → scout-bot → Environment: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` → Deploy.
5. Telegram: `/google`, open google.com/device, enter the code, allow. Existing tickets and note
   photos are attached straight away. `/google off` unlinks.

(Alternative for Google Workspace: a Shared Drive folder in `GDRIVE_FOLDER_ID`, written by the
service account.)

## Sneak peeks

Many accounts post photos of what will be on sale (mostly stories). Gemini recognises them and names
the notable pieces; Scout links them to the account's next market (or keeps them until it announces
one) and, once the account stops posting for a few minutes, sends them as an album of up to 10,
numbered, replying to the market's card, followed by ⭐ buttons. **⭐ 3** adds a note with that photo
("⭐ rattan armchair (photo 3)") — it shows on the card, in the morning briefing and, with the photo
attached, in the calendar (once the market is ✅); tap again to remove it. Reply to a photo with text
to note it in your own words. Bigger batches wait behind **👀 Show more**. ❌ markets get no peeks.

## Ticket hunt (released free tickets)

Only when you ask: `/hunt 18`, or **🔎 Watch for free tickets** on a ✅/🤔 card.

- **No ticket yet** → tells you when any free slot is available.
- **Already booked** → tells you only when a slot *earlier* than yours frees up (the first slots are the
  best). If you switch, cancel the old ticket on Eventbrite and send `/booked <id> HH:MM`; the hunt then
  looks for slots earlier than the new one.
- `/hunt 18 before 11:00` adds a cut-off.

Scout reads every slot's availability (Eventbrite API with `EVENTBRITE_TOKEN`, otherwise the public
page) every 15 minutes, and every 45 at night (00:00–05:30; `TICKET_HUNT_MINUTES`,
`TICKET_HUNT_NIGHT_MINUTES`), messages you only when a matching slot *becomes* free (at most 10 alerts
a day per market), silently at night, and stops
at **09:00 on the market's first day**. When Eventbrite only shows whole-event availability (one ticket
type, no times), the alert says so.

## Data retention

Events are marked past the day after they end and **deleted 14 days after their last day**
(`RETENTION_DAYS`), together with their Eventbrite watch, images and ticket files; teasers that never
got a date go after 60 days. Instagram items older than 30 days keep only their id (so a post is
never processed twice) — caption, parsed data and images are dropped. Google Calendar entries stay
as history.

## Schedule — Dokploy Schedules

Recurring jobs are **Dokploy schedules** on the scout-bot service (Schedules tab), so you can see,
change, pause and launch them yourself. Each runs one command inside the container, which asks the
running bot to do the job and prints the result (exit code 0 = ok):

| Task name | Cron (timezone **Europe/Madrid**) | Command |
| --- | --- | --- |
| Check posts | `2 8,11,14,17,20,23 * * *` | `python -m scout.cron check-posts` |
| Check stories | `0 13,21 * * *` | `python -m scout.cron check-stories` |
| Upkeep | `0 4 * * *` | `python -m scout.cron upkeep` |
| Morning briefing | `0 8 * * *` | `python -m scout.cron briefing` |
| Eventbrite refresh | `13 9 * * *` | `python -m scout.cron eventbrite-refresh` |
| Eventbrite login check | `17 12 * * *` | `python -m scout.cron eventbrite-login` |

Shell type Bash. To run one now: **Run** on its row (or `python -m scout.cron <task>` in the
container terminal); `python -m scout.cron list` shows each task's last run. If Check posts or
Check stories stops running (schedule deleted or disabled), Scout tells you once in Telegram.

What a task does: **check-posts** reads every account's posts and bio (Apify), **check-stories**
their stories (they live 24 h, so 13:00 and 21:00 catch them all), **upkeep** merges duplicates,
marks past markets, prunes images and deletes events 14 days after they end, **briefing** sends the
morning briefing on days with ✅/booked markets, **eventbrite-refresh** re-reads every watched
Eventbrite page, **eventbrite-login** warns once if the auto-booker is logged out.

Still inside the bot (they depend on each market's own times or run every minute): the booking
countdown (15 min, 1 min, now) and fast checks around an opening, the market-day and "1 h before
your slot" reminders, the ticket hunt, sneak-peek sending, the calendar retry, the heartbeat. Also
inside: after a restart, one posts check if the last is more than 3 h old and one stories check if
the last is more than 12 h old.

Local runs without Dokploy: `SCHEDULER=internal` makes the bot run the tasks itself on the crons
above (`POLL_HOURS` and `BRIEFING_TIME` then change those two).

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
