"""refresh-organizers: older reposted markets get their organiser from the saved items."""

from __future__ import annotations

import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from scout.db import Account, Database, Event, SourceItem
from scout.organizers import refresh_organizers


class Parser:
    def __init__(self, by_caption):
        self.by_caption, self.calls = by_caption, []

    async def parse(self, **kw):
        self.calls.append(kw["caption"])
        org = self.by_caption.get(kw["caption"])
        return SimpleNamespace(events=[SimpleNamespace(organizer=org)])


async def test_refresh_sets_organizer_only_where_someone_else_runs_it(tmp_path):
    db = Database(":memory:")
    with db.session() as s:
        mm = Account(handle="mercadillosmad")
        alma = Account(handle="almamarket_home")
        s.add_all([mm, alma])
        s.flush()
        e1 = Event(account_id=mm.id, title="Repost", dates="[]", user_decision="yes", status="announced")
        e2 = Event(account_id=mm.id, title="Own", dates="[]", user_decision="yes", status="announced")
        e3 = Event(account_id=mm.id, title="Merged", dates="[]", user_decision="yes", status="announced")
        e4 = Event(account_id=mm.id, title="Not on calendar", dates="[]", user_decision="maybe", status="announced")
        s.add_all([e1, e2, e3, e4])
        s.flush()
        s.add(SourceItem(account_id=mm.id, ig_id="story:1", kind="story", caption="flyer by @lacasadelaabuela",
                         parsed_json="{}", event_ids=json.dumps([e1.id])))
        s.add(SourceItem(account_id=mm.id, ig_id="post:2", kind="post", caption="our market",
                         parsed_json="{}", event_ids=json.dumps([e2.id])))
        s.add(SourceItem(account_id=alma.id, ig_id="post:3", kind="post", caption="alma's own post",
                         parsed_json=json.dumps({"events": [{"organizer": None}]}), event_ids=json.dumps([e3.id])))
        ids = (e1.id, e2.id, e3.id, e4.id)
    parser = Parser({"flyer by @lacasadelaabuela": "lacasadelaabuela", "our market": "mercadillosmad"})
    scout = SimpleNamespace(parser=parser, settings=SimpleNamespace(tz=ZoneInfo("Europe/Madrid")))
    app = SimpleNamespace(bot_data={})
    out = await refresh_organizers(app, scout, db, ["mercadillosmad"])
    assert out.startswith("2 of 3 markets updated")
    with db.session() as s:
        assert [s.get(Event, i).organizer for i in ids] == ["lacasadelaabuela", None, "almamarket_home", None]
    assert "alma's own post" not in parser.calls  # decided from its own parse, no Gemini call
