import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from scout.uptime import Uptime, WebhookWatch


def make(tmp_path: Path, alive_age_s: float | None):
    if alive_age_s is not None:
        f = tmp_path / "alive"
        f.touch()
        t = time.time() - alive_age_s
        os.utime(f, (t, t))
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    settings = SimpleNamespace(data_dir=tmp_path, allowed_chat_id=1, tz=ZoneInfo("Europe/Madrid"))
    return Uptime(app, settings, db=None), app


@pytest.mark.asyncio
async def test_restart_is_silent_and_outage_is_reported(tmp_path):
    up, app = make(tmp_path, 40)
    await up.report_downtime()
    app.bot.send_message.assert_not_called()
    up, app = make(tmp_path, 3 * 3600)
    await up.report_downtime()
    text = app.bot.send_message.call_args.args[1]
    assert "was down" in text and "3 h" in text


@pytest.mark.asyncio
async def test_first_start_silent(tmp_path):
    up, app = make(tmp_path, None)
    await up.report_downtime()
    app.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_poll_failures_warn_once_then_recover(tmp_path):
    up, app = make(tmp_path, None)
    await up.poll_result(False, "boom")
    app.bot.send_message.assert_not_called()
    await up.poll_result(False, "boom")
    await up.poll_result(False, "boom")
    assert app.bot.send_message.call_count == 1
    await up.poll_result(True)
    assert "working again" in app.bot.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_webhook_watch(tmp_path):
    up, app = make(tmp_path, None)
    ww = WebhookWatch(up)
    bad = SimpleNamespace(last_error_date=datetime.now(timezone.utc), pending_update_count=3,
                          last_error_message="SSL error", url="https://bots.rabbia.net/scoutbot")
    app.bot.get_webhook_info = AsyncMock(return_value=bad)
    await ww.check()
    await ww.check()
    assert app.bot.send_message.call_count == 1
    app.bot.get_webhook_info = AsyncMock(return_value=SimpleNamespace(last_error_date=None, pending_update_count=0,
                                                                       last_error_message=None, url=""))
    await ww.check()
    assert "again" in app.bot.send_message.call_args.args[1]
