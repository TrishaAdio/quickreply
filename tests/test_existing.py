"""Preserved QuickReply behaviour (ported from ChannelGuard's quickreply.py),
minus the removed keyword / admin-bot reservation part of /add."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

import pytest
from telethon.tl import types as T

from fakes import FakeEvent, jpeg
from qr.handlers import core, router
from qr.services import payments as P
from qr.services.entities import fit_media_caption, substitute, swap_link, u16

CUSTOMER = 555


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_amounts_are_positive_finite_and_round_to_paise():
    for raw in ("0", "-1", "nan", "inf", "abc"):
        with pytest.raises(ValueError):
            P.parse_amount(raw)
    assert P.parse_amount("1,999.999") == Decimal("2000.00")
    assert P.fmt_inr(Decimal("1234567.5")) == "\u20b912,34,567.50"
    assert P.fmt_inr(P.parse_amount("\u20b9 14 INR")) == "\u20b914"


def test_substitution_keeps_entities_on_utf16_offsets():
    text = "\U0001F60A {amount} paid"
    bold = T.MessageEntityBold(offset=u16("\U0001F60A "), length=u16("{amount}"))
    after = T.MessageEntityItalic(offset=u16("\U0001F60A {amount} "), length=4)
    new_text, (b, i) = substitute(text, [bold, after], {"{amount}": "\u20b91,000"})
    assert new_text == "\U0001F60A \u20b91,000 paid"
    assert new_text.encode("utf-16-le")[b.offset * 2:(b.offset + b.length) * 2].decode("utf-16-le") == "\u20b91,000"
    assert i.offset == u16("\U0001F60A \u20b91,000 ")


def test_full_width_and_legacy_tokens_are_canonicalised():
    text, _ = P.prepare_template("Order: \uff5bOrder ID\uff5d\nAmount: \u20b9500", [])
    assert text == "Order: {orderid}\nAmount: {amount}"


def test_swap_link_updates_hyperlink_and_raw_link():
    link = T.MessageEntityTextUrl(offset=0, length=4, url="https://t.me/+old")
    bold = T.MessageEntityBold(offset=5, length=15)
    text, entities = swap_link("Join t.me/+oldoldold now", [link, bold], "https://t.me/+NEW123")
    assert text == "Join https://t.me/+NEW123 now"
    assert entities[0].url == "https://t.me/+NEW123"
    assert entities[1].length == u16("https://t.me/+NEW123")


def test_media_caption_limit_uses_utf16_units():
    exact = "\U0001F600" * 512
    text, _e, clipped = fit_media_caption(exact, [])
    assert not clipped and u16(text) == 1024
    text, _e, clipped = fit_media_caption(exact + "x", [], preserve=("ANIABC",))
    assert clipped and u16(text) <= 1024 and text.startswith("ANIABC")


def test_broadcast_parser_uses_ist():
    start, end, keyword = core.parse_broadcast(
        "/broadcast 9:30 AM 18 JUL 2026 THANKS FOR", datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert start == datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc) and keyword == "THANKS FOR"
    with pytest.raises(ValueError, match="future"):
        core.parse_broadcast("/broadcast 9:30 AM 18 JUL 2027 X", datetime(2026, 7, 20, tzinfo=timezone.utc))


def test_help_lists_every_command():
    for command in ("/add", "/setdone", "/setchannelpostofpayment", ".setchannel", "/stats", "/clear",
                    "/cancel", ".unban", ".save", ".setup", ".post", ".postbatch", ".postnow",
                    ".setpostchannel", ".setarchive", ".archive", "/set", "/unset", "/show", "/away",
                    "/broadcast", ".linksource", ".config", ".ping", "L"):
        assert command in core.HELP_TEXT, command
    assert "{link}" not in core.HELP_TEXT and "keyword" not in core.HELP_TEXT.lower()


def test_every_command_has_a_route():
    import inspect
    source = inspect.getsource(router.dispatch) + inspect.getsource(router.greeting_commands)
    for name in router.ADMIN_COMMANDS | router.ANYWHERE_COMMANDS:
        assert f'"{name}"' in source, name


# ---------------------------------------------------------------------------
# /add without keywords
# ---------------------------------------------------------------------------
def proof(account, tag="proof", when=datetime(2026, 10, 20, 18, 29, tzinfo=timezone.utc), client=None):
    photo = account.make_photo(jpeg(tag))
    message = T.Message(id=7000 + len(tag), peer_id=T.PeerUser(CUSTOMER), date=when, message="",
                        out=False, media=T.MessageMediaPhoto(photo=photo))
    if client is not None:
        client.peer_messages.setdefault(CUSTOMER, []).append(message)
    return message


async def run(app, text, chat_id=CUSTOMER, **kwargs):
    event = FakeEvent(text, chat_id=chat_id, sender_id=app.self_id, **kwargs)
    await router.on_outgoing(app, event)
    return event


async def test_add_logs_payment_receipt_channel_post_and_screenshot(app, account, post_channel):
    await app.set(payment_channel=post_channel.chat_id)
    event = await run(app, "/add 1499 John Doe", reply=proof(account))
    payment = await app.db.payments.find_one({})
    assert payment["status"] == "valid" and payment["amount"] == "1499.00" and payment["name"] == "John Doe"
    receipt = event.edits[-1]
    assert "\u20b91,499" in receipt and "John Doe" in receipt
    assert payment["post_status"] == "posted"
    caption = post_channel.messages[-1].message
    assert payment["order_id"] in caption and "\u20b91,499" in caption
    shot = await app.db.screenshots.find_one({})
    assert shot["order_id"] == payment["order_id"]
    assert shot["archive_date"] == "2026-10-20"          # 23:59 IST receipt time
    assert "keyword" not in receipt.lower()


async def test_add_requires_a_replied_screenshot(app):
    event = await run(app, "/add 500 Ravi")
    assert "Reply to the payment screenshot" in event.text_out
    assert await app.db.payments.count_documents({}) == 0
    event = await run(app, "/add")
    assert "Usage" in event.text_out


async def test_duplicate_update_records_one_payment(app, account, post_channel):
    await app.set(payment_channel=post_channel.chat_id)
    reply = proof(account)
    first = FakeEvent("/add 10 A", chat_id=CUSTOMER, sender_id=app.self_id, reply=reply, msg_id=42)
    second = FakeEvent("/add 10 A", chat_id=CUSTOMER, sender_id=app.self_id, reply=reply, msg_id=42)
    await router.on_outgoing(app, first)
    await router.on_outgoing(app, second)
    assert await app.db.payments.count_documents({}) == 1
    assert second.edits == []


async def test_missing_payment_channel_queues_then_setchannel_delivers(app, account, client, post_channel):
    await run(app, "/add 250 Meera", reply=proof(account, client=client))
    payment = await app.db.payments.find_one({})
    assert payment["status"] == "valid" and payment["post_status"] == "not_configured"
    assert any("no payment channel" in m.message for m in account.saved)
    event = await run(app, f".setchannel {post_channel.chat_id}", chat_id=app.self_id)
    assert "Payment channel set" in event.text_out
    payment = await app.db.payments.find_one({})
    assert payment["post_status"] == "posted"
    assert len(post_channel.messages) == 1


async def test_stats_cancel_and_clear(app, account, post_channel):
    await app.set(payment_channel=post_channel.chat_id)
    await run(app, "/add 100 A", reply=proof(account, "p1"))
    await run(app, "/add 300 B", reply=proof(account, "p22"))
    stats = await run(app, "/stats", chat_id=app.self_id)
    assert "\u20b9400 - 2 payments" in stats.text_out
    post = post_channel.messages[0]
    cancel = FakeEvent("/cancel", chat_id=post_channel.chat_id, is_channel=True, reply=post,
                       sender_id=None)
    await router.on_outgoing(app, cancel)
    assert post.message.startswith("FAKE PAYMENT\n\n")
    stats = await run(app, "/stats", chat_id=app.self_id)
    assert "\u20b9300 - 1 payments" in stats.text_out
    cleared = await run(app, "/clear", chat_id=app.self_id)
    assert "1 payment(s) removed" in cleared.text_out
    stats = await run(app, "/stats", chat_id=app.self_id)
    assert "\u20b90 - 0 payments" in stats.text_out


async def test_setdone_from_formatted_post_keeps_entities(app, account, post_channel):
    template = account.message("Thanks {name}! Order {orderid}",
                               entities=[T.MessageEntityBold(offset=0, length=6)])
    event = await run(app, "/setdone", chat_id=app.self_id, reply=template)
    assert "1 formatting/emoji entity(ies) stored" in event.text_out
    await app.set(payment_channel=post_channel.chat_id)
    receipt = await run(app, "/add 5 Zed", reply=proof(account))
    payment = await app.db.payments.find_one({})
    assert receipt.edits[-1].startswith(f"Thanks Zed! Order {payment['order_id']}")


# ---------------------------------------------------------------------------
# greeting, link relay, L
# ---------------------------------------------------------------------------
async def test_greeting_is_sent_once_to_new_users_while_away(app, account, client, monkeypatch):
    greeting = account.message("Hi! I'll reply soon")
    account.saved.append(greeting)
    await run(app, "/set", chat_id=app.self_id, reply=greeting)
    assert app.cfg["greeting"]["message_id"] == greeting.id
    app.state.owner_active_until = 0

    async def new_user(app_, event):
        return True

    monkeypatch.setattr(core, "is_new_user", new_user)
    incoming = FakeEvent("hello", chat_id=999, sender_id=999, out=False, is_private=True)
    await core.on_incoming(app, incoming)
    await core.on_incoming(app, incoming)
    sent = [s for s in client.sent if s[0] == 999]
    assert len(sent) == 1 and sent[0][1]["text"] == "Hi! I'll reply soon"
    assert await app.db.greeted.count_documents({"user_id": 999}) == 1


async def test_link_relay_only_trusts_the_configured_source(app, account):
    account.add_shortcut("demo", [account.message("Join https://t.me/+oldlink here")])
    untrusted = FakeEvent("https://t.me/+evil123", chat_id=77, sender_id=77, out=False, is_private=True)
    await app.set(greet_new=False)
    await core.on_incoming(app, untrusted)
    assert "evil" not in account.shortcuts["demo"]["messages"][0].message
    await app.set(link_source=88)
    trusted = FakeEvent("https://t.me/+fresh456", chat_id=88, sender_id=88, out=False, is_private=True)
    await core.on_incoming(app, trusted)
    assert account.shortcuts["demo"]["messages"][0].message == "Join https://t.me/+fresh456 here"


async def test_uppercase_l_clears_and_blocks_private_chat(app, client):
    await run(app, "L", chat_id=CUSTOMER)
    names = [type(r).__name__ for r in client.requests]
    assert "DeleteHistoryRequest" in names and "BlockRequest" in names


# ---------------------------------------------------------------------------
# startup wiring
# ---------------------------------------------------------------------------
async def test_runtime_starts_registers_handlers_and_loops(mongo_url, tmp_path, monkeypatch):
    import asyncio
    import uuid

    import telethon
    from fakes import FakeAccount, FakeClient
    from qr import runtime
    from qr.config import settings as config

    db_name = f"qrrt_{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv("MONGO_URL", f"{mongo_url}{db_name}")
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    account = FakeAccount(4321)
    created = {}

    class FakeTelegram(FakeClient):
        def __init__(self, session, api_id, api_hash):
            super().__init__(account)
            self.handlers = []
            created["client"] = self
            created["session"] = session

        async def connect(self):
            return True

        async def is_user_authorized(self):
            return True

        def add_event_handler(self, callback, event):
            self.handlers.append((callback, event))

        async def run_until_disconnected(self):
            await asyncio.sleep(0.2)   # let the background loops run once

        async def disconnect(self):
            return None

    monkeypatch.setattr(telethon, "TelegramClient", FakeTelegram)
    from qr.services import legacy_import
    monkeypatch.setattr(legacy_import, "find_running_legacy",
                        lambda base, account_id: "pid=77 cwd=/old" if account_id == 4321 and refuse else None)
    refuse = True
    with pytest.raises(SystemExit):
        await runtime.run("quickreply")          # old ChannelGuard bot running -> refuse
    assert created["client"].handlers == []
    refuse = False
    await runtime.run("quickreply")
    client = created["client"]
    assert Path(created["session"]).parts[-2:] == ("sessions", "quickreply")
    kinds = sorted((e.incoming, e.outgoing) for _cb, e in client.handlers)
    assert kinds == [(False, True), (True, False)]
    from pymongo import AsyncMongoClient
    mongo = AsyncMongoClient(f"{mongo_url}{db_name}")
    database = mongo.get_default_database()
    assert await database.accounts.find_one({"_id": 4321})
    indexes = [i["key"] for i in await (await database.screenshots.list_indexes()).to_list()]
    assert any("source_message_id" in key for key in indexes)
    await mongo.drop_database(db_name)
    await mongo.close()
