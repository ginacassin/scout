"""Score the Gemini parser against the real cases in tests/fixtures/test_battery.json.

Usage (needs GEMINI_API_KEY; costs a few cents):
    GEMINI_API_KEY=... python scripts/eval_parser.py [--model gemini-3.5-flash-lite]

Inside the running container (Dokploy → scout-bot → Open Terminal):
    python scripts/eval_parser.py

Checks per case: kind, number of events, dates, region, booking method and booking-open time.
Cases whose info is only in the image (needs_ocr) are reported but not scored, because the
battery stores captions, not the images.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout.parser.gemini import GeminiParser  # noqa: E402

BATTERY = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "test_battery.json"
KIND_EQUIV = {"skip_old_pinned": "unrelated"}


def check(case: dict, result) -> list[str]:  # noqa: ANN001
    exp = case["expect"]
    errors = []
    kind = KIND_EQUIV.get(exp.get("kind"), exp.get("kind"))
    got_kind = result.kind.value
    acceptable = {kind}
    if kind in ("reminder", "update"):
        acceptable |= {"reminder", "update", "new_event"}  # without memory, a repeat can look new
    if kind == "teaser":
        acceptable |= {"new_event"} if exp.get("dates") else set()
    if got_kind not in acceptable:
        errors.append(f"kind {got_kind} ≠ {kind}")
    if "events" in exp and len(result.events) != exp["events"]:
        errors.append(f"{len(result.events)} events ≠ {exp['events']}")
    ev = result.events[0] if result.events else None
    if exp.get("dates") and ev:
        if sorted(ev.dates) != sorted(exp["dates"]):
            errors.append(f"dates {ev.dates} ≠ {exp['dates']}")
    if exp.get("region") in ("madrid", "outside") and ev and ev.region.value != exp["region"]:
        errors.append(f"region {ev.region.value} ≠ {exp['region']}")
    if exp.get("booking_method") in ("eventbrite", "free_entry") and ev and ev.booking_method.value != exp["booking_method"]:
        errors.append(f"booking {ev.booking_method.value} ≠ {exp['booking_method']}")
    if exp.get("booking_opens_at") and ev:
        want = datetime.fromisoformat(exp["booking_opens_at"]).replace(tzinfo=None)
        got = ev.booking_opens_at and datetime.fromisoformat(ev.booking_opens_at).replace(tzinfo=None)
        if got != want:
            errors.append(f"opens {got} ≠ {want}")
    if "address" in exp and exp["address"] is None and ev and ev.address:
        errors.append(f"invented address {ev.address!r}")
    return errors


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    args = ap.parse_args()
    parser = GeminiParser(os.environ["GEMINI_API_KEY"], args.model)
    cases = json.loads(BATTERY.read_text())["cases"]
    passed = scored = 0
    for case in cases:
        if case.get("needs_ocr") or not case.get("caption"):
            print(f"–  {case['id']:4} skipped (info only in image)")
            continue
        posted = datetime.fromisoformat(case["posted"])
        result = await parser.parse(
            handle=case["account"], item_kind="post", posted_at_local=posted.strftime("%A %Y-%m-%d 12:00"),
            today_local=posted.strftime("%A %Y-%m-%d"), caption=case["caption"], images=[],
            location=case.get("location"), coauthors=case.get("collab"), bio=case.get("bio"),
            bio_links=case.get("bio_links"),
        )
        errors = check(case, result)
        scored += 1
        if errors:
            print(f"✗  {case['id']:4} " + "; ".join(errors))
        else:
            passed += 1
            print(f"✓  {case['id']:4} {result.kind.value}")
    print(f"\n{passed}/{scored} cases pass ({args.model})")
    return 0 if passed == scored else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
