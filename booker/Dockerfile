# scout-booker: Chromium with a persistent Eventbrite login, driven by Playwright.
# Build context is this folder (booker/). Multi-arch image, runs on the Pi 5 (arm64).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Madrid \
    DISPLAY=:99 \
    PROFILE_DIR=/profile \
    PORT=8100

# Virtual display, light window manager, VNC + noVNC for the one-time login.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify fluxbox tzdata fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# --ignore-installed: the base image has a Debian-managed typing_extensions that pip cannot uninstall
RUN python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed -r requirements.txt

COPY scoutbooker ./scoutbooker
COPY entrypoint.sh .

VOLUME ["/profile"]
EXPOSE 8100 6080

HEALTHCHECK --interval=5m --timeout=10s --start-period=1m CMD python3 -c "import os,urllib.request as u; r=u.Request('http://127.0.0.1:8100/health', headers={'X-Booker-Token': os.environ.get('BOOKER_TOKEN','')}); u.urlopen(r, timeout=5)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
