"""Docker healthcheck: healthy while a poll has succeeded in the last 6 hours.

The first poll can be up to 3 h after start, so a missing heartbeat file is
treated as healthy for the first 6 h of the container's life.
"""

import os
import sys
import time
from pathlib import Path

MAX_AGE_S = 6 * 3600


def main() -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    beat = data_dir / "heartbeat"
    started = data_dir / "started"
    now = time.time()
    if beat.exists():
        return 0 if now - beat.stat().st_mtime < MAX_AGE_S else 1
    if started.exists():
        return 0 if now - started.stat().st_mtime < MAX_AGE_S else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
