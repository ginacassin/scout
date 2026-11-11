"""Run a Scout task now, inside the scout-bot container (what Dokploy Schedules call).

    python -m scout.cron check-posts     # read every account's posts and bio
    python -m scout.cron list            # tasks, suggested crons, last runs
    python -m scout.cron refresh-organizers mercadillosmad   # tasks can take arguments

It asks the running bot to do the work and waits for it. Exit codes: 0 ok, 1 the task failed,
2 the bot isn't reachable (not running, still starting, or a different TASKS_PORT).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from .tasks import task_token, tasks_port

TIMEOUT_S = 45 * 60  # a story run with many accounts can take a while


async def call(task: str, args: list[str] | None = None) -> dict:
    token = task_token(os.environ.get("BOT_TOKEN", ""))
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", tasks_port()), timeout=10)
    writer.write((json.dumps({"token": token, "task": task, "args": args or []}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=TIMEOUT_S)
    writer.close()
    return json.loads(line or b'{"ok": false, "summary": "no answer"}')


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 1
    task, args = argv[0], argv[1:]
    try:
        resp = asyncio.run(call(task, args))
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"Scout bot not reachable on 127.0.0.1:{tasks_port()} ({exc}). Is scout-bot running?")
        return 2
    if task == "list" and resp.get("ok"):
        for t in resp["tasks"]:
            print(f"{t['name']:<18} {t['cron']:<24} last run {t['last_run']}\n    {t['description']}")
        return 0
    print(("OK " if resp.get("ok") else "FAILED ") + f"{task}: {resp.get('summary')}")
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
