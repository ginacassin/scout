#!/bin/sh
# Virtual display for the headed Chromium; noVNC (login mode) is started on demand by the app.
set -e
N="${DISPLAY#:}"
N="${N%%.*}"
rm -f "/tmp/.X${N}-lock" "/tmp/.X11-unix/X${N}"
Xvfb "$DISPLAY" -screen 0 1280x900x24 -nolisten tcp -ac >/dev/null 2>&1 &
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -e "/tmp/.X11-unix/X${N}" ] && break; sleep 0.3; done
fluxbox >/dev/null 2>&1 &
exec python3 -m scoutbooker
