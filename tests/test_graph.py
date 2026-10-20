""".graph / .tgraph: DM tracking, hourly aggregation, peaks, INR per hour,
history scan, rendering and command wiring."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from PIL import Image
from telethon.tl import types as T

from fakes import FakeEvent
from qr.config.settings import IST
from qr.handlers import core, router
from qr.services import dm_stats, graph_render

DAY = "2026-10-20"


def ist(hour, minute=0, day=20, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=IST)


def dm(sender, at, msg_id, bot=False):
    return FakeEvent("hi", chat_id=sender, sender_id=sender, out=False, is_private=True, msg_id=msg_id,
                     date=at.astimezone(timezone.utc), sender=SimpleNamespace(bot=bot, is_self=False))


async def feed(app, events):
    await app.set(greet_new=False)
    for event in events:
        await core.on_incoming(app, event)


async def test_dms_are_recorded_per_ist_hour_without_text(app):
    await feed(app, [dm(1, ist(0, 0), 10), dm(1, ist(0, 30), 11), dm(2, ist(23, 59, second=59), 12),
                     dm(3, ist(0, 0, day=21), 13)])
    rows = await app.db.dm_events.find({}).to_list(None)
    assert {(r["date"], r["hour"]) for r in rows} == {(DAY, 0), (DAY, 23), ("2026-10-21", 0)}
    assert all(set(r) == {"_id", "account_id", "user_id", "at", "date", "hour"} for r in rows)


async def test_duplicates_bots_service_self_and_link_source_are_not_counted(app):
    await app.set(link_source=88)
    await feed(app, [dm(1, ist(10), 10), dm(1, ist(10), 10),          # same message twice
                     dm(2, ist(10), 11, bot=True), dm(777000, ist(10), 12),
                     dm(app.self_id, ist(10), 13), dm(88, ist(10), 14)])
    group = FakeEvent("hi", chat_id=-100, sender_id=5, out=False, is_private=False)
    await core.on_incoming(app, group)
    assert await app.db.dm_events.count_documents({}) == 1


async def test_tracking_can_be_switched_off(app, account):
    event = FakeEvent(".config dm_tracking off", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert app.cfg["dm_tracking"] is False
    await feed(app, [dm(1, ist(10), 10)])
    assert await app.db.dm_events.count_documents({}) == 0


async def test_day_stats_peaks_window_and_inr_per_hour(app):
    events = [dm(u, ist(20, m), 100 + i) for i, (u, m) in enumerate(
        [(1, 1), (1, 2), (2, 3), (3, 4), (4, 5)])]            # 5 DMs / 4 people at 20h
    events += [dm(5, ist(21, 1), 200), dm(1, ist(21, 2), 201), dm(6, ist(9, 0), 202)]
    await feed(app, events)
    base = {"account_id": app.self_id, "name": "x"}
    await app.db.payments.insert_many([
        {**base, "order_id": "A", "amount": "499.00", "status": "valid", "ts": ist(21, 10).timestamp()},
        {**base, "order_id": "B", "amount": "1500.50", "status": "cleared", "ts": ist(21, 50).timestamp()},
        {**base, "order_id": "C", "amount": "999.00", "status": "fake", "ts": ist(21, 55).timestamp()},
        {**base, "order_id": "D", "amount": "200.00", "status": "valid", "ts": ist(9, 30).timestamp()},
        {**base, "order_id": "E", "amount": "50.00", "status": "valid", "ts": ist(0, 0, day=21).timestamp()},
    ])
    stats = await dm_stats.day_stats(app, DAY, now=ist(12, 0, day=22))
    assert stats.dms[20] == 5 and stats.senders[20] == 4 and stats.dms[21] == 2 and stats.dms[9] == 1
    assert stats.total_dms == 8 and stats.unique_senders == 6
    assert stats.peak_dm_hour == 20
    assert stats.busiest_window() == (19, 7)                  # 19-22 holds 0+5+2
    assert stats.inr[21] == Decimal("1999.50") and stats.payments[21] == 2   # fake excluded
    assert stats.inr[9] == Decimal("200.00")
    assert stats.total_inr == Decimal("2199.50")              # next-day payment excluded
    assert stats.peak_inr_hour == 21
    assert stats.now_hour is None                              # a finished day
    text = dm_stats.summary_text(stats)
    assert "Peak DMs: 20:00\u201321:00 (5)" in text and "Peak income: 21:00\u201322:00 (\u20b91,999.50)" in text


async def test_today_has_live_now_marker(app):
    stats = await dm_stats.day_stats(app, DAY, now=ist(15, 40))
    assert (stats.now_hour, stats.now_minute) == (15, 40)


async def test_scan_backfills_history_and_never_double_counts(app, client):
    alice = T.User(id=501, first_name="A", access_hash=1)
    bot = T.User(id=502, first_name="B", access_hash=1, bot=True)
    old = T.User(id=503, first_name="C", access_hash=1)

    def message(mid, at, out=False):
        return T.Message(id=mid, peer_id=T.PeerUser(501), date=at.astimezone(timezone.utc), message="x", out=out)

    client.peer_messages[501] = [message(4, ist(23, 59, day=19)), message(1, ist(9)),   # oldest first
                                 message(2, ist(9, 5), out=True), message(3, ist(18)),
                                 message(5, ist(0, 1, day=21))]
    client.peer_messages[502] = [message(6, ist(10))]
    client.user_dialogs = [
        SimpleNamespace(is_user=True, entity=alice, date=ist(0, 1, day=21), pinned=False),
        SimpleNamespace(is_user=True, entity=bot, date=ist(10), pinned=False),
        SimpleNamespace(is_user=True, entity=old, date=ist(10, day=18), pinned=False),
    ]
    await feed(app, [dm(501, ist(18), 3)])                     # already tracked live
    result = await dm_stats.scan_day(app, DAY)
    assert result == {"chats": 1, "messages": 2, "added": 1}
    stats = await dm_stats.day_stats(app, DAY)
    assert stats.dms[9] == 1 and stats.dms[18] == 1 and stats.total_dms == 2
    assert (await dm_stats.scan_day(app, DAY))["added"] == 0   # idempotent


def test_render_produces_glow_card_for_every_state():
    states = [dm_stats.synthetic(DAY, seed=1), dm_stats.DayStats(date=DAY, now_hour=11, now_minute=5),
              dm_stats.DayStats(date=DAY)]
    full = dm_stats.synthetic(DAY, seed=2)
    full.test, full.now_hour = False, 23
    full.inr = [Decimal("0")] * 23 + [Decimal("123456789")]   # huge single value, last hour
    states.append(full)
    for stats in states:
        data = graph_render.render(stats, account="Alice @alice")
        image = Image.open(io.BytesIO(data))
        assert image.format == "JPEG" and image.size == (graph_render.W, graph_render.H) == (2400, 1350)
        image.load()


def test_render_png_option_and_rupee_font():
    data = graph_render.render(dm_stats.synthetic(DAY, seed=3), fmt="PNG")
    assert Image.open(io.BytesIO(data)).format == "PNG"
    for kind in ("display", "text"):
        rupee = graph_render.font(kind, 800, 60)
        assert isinstance(rupee, graph_render.ImageFont.FreeTypeFont)   # bundled font loaded
        assert rupee.getmask("\u20b9").getbbox() != rupee.getmask("\U0010fffd").getbbox()


def test_money_and_hour_helpers():
    assert graph_render.compact_inr(950) == "\u20b9950"
    assert graph_render.compact_inr(6796) == "\u20b96.8k"
    assert graph_render.compact_inr(150000) == "\u20b91.5L"
    assert graph_render.compact_inr(21000000) == "\u20b92.1Cr"
    assert graph_render.h12(0) == "12 AM" and graph_render.h12(21) == "9 PM" and graph_render.h12(12) == "12 PM"
    assert graph_render.range12(19, 22) == "7\u201310 PM"
    assert graph_render.range12(11, 14) == "11 AM\u20132 PM"
    assert graph_render.range12(22, 25) == "10 PM\u20131 AM"


def test_heat_palette_runs_cool_to_hot():
    assert graph_render.heat(0.0) == graph_render.BLUE
    assert graph_render.heat(1.0) == graph_render.HOT


def test_synthetic_day_is_plausible():
    stats = dm_stats.synthetic(DAY, seed=5)
    assert stats.test and stats.total_dms > 100
    assert 0 < stats.unique_senders <= stats.total_dms
    assert stats.peak_dm_hour >= 17                           # evening peak shape


async def test_graph_and_tgraph_commands_send_the_card(app, account, client):
    await feed(app, [dm(1, datetime.now(IST), 10)])
    before = await app.db.dm_events.count_documents({})
    event = FakeEvent(".tgraph", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "Test graph sent" in event.text_out
    chat, info = client.sent[-1]
    assert chat == "me" and info["caption"].startswith("TEST GRAPH (synthetic data)")
    assert await app.db.dm_events.count_documents({}) == before   # test touched nothing

    event = FakeEvent(".graph", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "Graph for" in event.text_out
    chat, info = client.sent[-1]
    assert chat == "me" and "DMs: 1 from 1 people" in info["caption"]
    photo = account.media[max(account.media)]
    assert Image.open(io.BytesIO(photo)).size == (2400, 1350)

    event = FakeEvent(".graph 2026-13-40", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "Usage: .graph" in event.text_out


async def test_graph_is_saved_messages_only(app, account, client):
    event = FakeEvent(".graph", chat_id=555, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "in your Saved Messages" in event.text_out
    assert client.sent == []


def test_resolve_day():
    now = datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)   # 00:30 IST on the 21st
    assert dm_stats.resolve_day("", now) == "2026-10-21"
    assert dm_stats.resolve_day("yesterday", now) == "2026-10-20"
    assert dm_stats.resolve_day("2026-10-01", now) == "2026-10-01"
