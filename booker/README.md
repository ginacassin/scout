# scout-booker

A separate container that claims **free** Eventbrite tickets at the second sales open, for events
you armed in Scout with **🤖 Auto-book at opening**. It keeps a logged-in Chromium profile and drives
it with Playwright. If anything is off (a captcha, a paid total, extra form fields, a page it doesn't
recognise, a sold-out page, a crash), it stops and immediately sends
**"⚠️ Couldn't auto-book … book manually NOW"** with buttons for the event page and the checkout.

Rules it keeps: one attempt per event (recorded on disk before it starts), free tickets only,
never solves captchas, never sees your password. Eventbrite's terms forbid automation; you've
accepted that risk.

## How it fits

```
Scout (scout)  --HTTP-->  scout-booker:8100   (Docker network only)
                             |  Chromium on Xvfb :99, profile in /profile/chromium
                             |  noVNC :6080 only while you log in
                             '--> Telegram Bot API (sendMessage / sendPhoto only)
```

## Dokploy setup (once)

1. **Create** → Application, name **`scout-booker`**, in the same project as Scout.
2. **Source**: the same Git repo and branch as Scout.
   **Build type** Dockerfile, **Dockerfile path** `booker/Dockerfile`, **Docker context path** `booker`
   (in some Dokploy versions this is "Build Path: `/booker`" with Dockerfile `Dockerfile`).
3. **Environment**:
   ```
   BOOKER_TOKEN=<long random string, same value in Scout>
   BOT_TOKEN=<same as Scout>
   TELEGRAM_ALLOWED_CHAT_ID=<same as Scout>
   TZ=Europe/Madrid
   BOOKER_QUANTITY=1
   VNC_PUBLIC_URL=http://192.168.1.100:6080/vnc.html
   VNC_PASSWORD=<optional; noVNC will ask for it>
   ```
   Optional tuning: `BOOKER_PREOPEN_S` (90), `BOOKER_POLL_MS` (300), `BOOKER_RELOAD_EVERY_S` (2.5),
   `BOOKER_WINDOW_S` (180), `LOGIN_TIMEOUT_S` (900).
4. **Volumes** → Volume mount: name **`scout-booker-profile`**, mount path **`/profile`**
   (holds the browser profile with your Eventbrite session, `jobs.json` and screenshots).
5. **Advanced → Ports**: publish **6080 → 6080** (TCP). Either only while you log in, or keep it on;
   it's only reachable from your LAN (the Pi isn't port-forwarded) and noVNC only runs in login mode.
   Do **not** publish 8100: Scout reaches it on the Docker network.
6. **Advanced → Resources / Swarm settings**: give it **shared memory 1 GB** (`shm_size: 1gb`;
   Chromium crashes with Docker's 64 MB default). Memory: ~60 MB idle, **~400–500 MB while a booking
   or login runs** (Chromium closes in between). A 768 MB limit is comfortable on the 8 GB Pi.
7. **Network**: Scout and scout-booker must share a network so `http://scout-booker:8100` resolves.
   Same Dokploy project usually suffices; otherwise attach both to `dokploy-network` and use the
   service's internal name as `BOOKER_URL`.
8. Deploy. In **Scout's** environment add:
   ```
   BOOKER_URL=http://scout-booker:8100
   BOOKER_TOKEN=<same string>
   BOOKER_VNC_URL=http://192.168.1.100:6080/vnc.html   # optional, overrides the booker's value
   ```
   and redeploy Scout. `BOOKER_URL=off` hides the feature.

## Log in to Eventbrite (once, and whenever the session expires)

1. In Telegram: `/autobook login`.
2. On a computer or phone on your home Wi-Fi open http://192.168.1.100:6080/vnc.html → **Connect**.
3. In the Chromium window, log in to eventbrite.es yourself (Google/Apple sign-in also works).
4. `/autobook done`. Scout replies whether the session is now logged in.

Login mode closes itself after 15 minutes. The session cookie stays in the `scout-booker-profile`
volume; the booker checks it again 90 s before every booking and warns you when you arm an event
while logged out.

## API (X-Booker-Token header required when BOOKER_TOKEN is set)

| Method | Path | Body / result |
|---|---|---|
| POST | `/book` | `{event_id, eventbrite_id, event_url, opens_at (ISO), title, quantity?}` → `{job, changed}`; 409 if already attempted |
| DELETE | `/book/{event_id}` | cancel; 409 once the attempt has started at/after opening |
| GET | `/jobs` | `{jobs: [...]}` with `state`: scheduled, running, booked, failed:&lt;reason&gt;, cancelled |
| GET | `/health` | `{ok, logged_in: true/false/null, browser}` |
| POST | `/check-login` | `{logged_in}` (opens eventbrite.es/account-settings/ in the profile) |
| POST | `/login/start` | `{ok, vnc_url, closes_in_s}` |
| POST | `/login/stop` | `{ok}` |

## What happens at opening

* **T − 90 s**: Chromium starts (headed on Xvfb), checks the login, opens the event page.
* **T**: looks at the page every 300 ms and reloads every 2.5 s until **Reservar / Conseguir entradas /
  Get tickets / Reserve a spot** is enabled (max 3 min), clicks it and finds the checkout iframe
  (`/checkout-external`). If the event page never shows such a button, it opens
  `checkout-external?eid=…` directly after 20 s.
* Picks the quantity (stops if there are several ticket types to choose from).
* Presses Register / Continue / Realizar pedido step by step. Before every press: captcha → stop;
  payment form or any non-zero amount → stop; empty required field → stop; can't confirm it's free → stop.
* Success ("Gracias por tu pedido", order number, confirmation URL) → "✅ Booked!" with a screenshot.

Screenshots of every attempt are kept in `/profile/shots/`.

## Fixing selectors

All Eventbrite wording and URL patterns live in `scoutbooker/eb_selectors.py`
(`RESERVE_RE`, `ADVANCE_RE`, `SUCCESS_TEXT_RE`, `LOGIN_CHECK_PATH`, …). Tests:

```
cd booker && pip install -r requirements-dev.txt && pytest -q
```

## Verify live before trusting it

These can't be tested without the real site; do a dry run on a free Eventbrite event that is
already on sale (arm it with an opening time 3 minutes ahead via `POST /book`) and watch over noVNC:

1. `/account-settings/` redirects to `/signin/` when logged out (login detection).
2. The event page's button label matches `RESERVE_RE` and opens an iframe whose URL contains `checkout`.
3. The quantity control is a `<select>` with numeric options, or a +/- stepper.
4. Logged-in checkout prefills name/email (no empty required fields) and the button labels match `ADVANCE_RE`.
5. The confirmation page matches `SUCCESS_TEXT_RE` / `SUCCESS_URL_RE`.
6. Chromium runs with 1 GB shm on the Pi and the Playwright image tag `v1.63.0-noble` pulls for arm64.
