"""Command authorization: only the account itself, admin commands only in
Saved Messages, forwarded/incoming/bot-chat messages never run commands."""
from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeEvent
from qr.handlers import core, router

ADMIN = (".save", ".setup 111", ".post", ".postbatch 3", ".postnow", ".archive run", ".backups",
         ".config rio_pct 10", ".postcaption x")


async def test_incoming_messages_never_run_commands(app, account, client):
    await app.set(greet_new=False)
    for text in ADMIN + ("/add 100 x", "/clear", "L"):
        event = FakeEvent(text, chat_id=account.user.id, sender_id=999, out=False)
        await router.on_outgoing(app, event)     # even if routed here by mistake
        await core.on_incoming(app, event)
        assert event.edits == [] and event.responses == []
    assert await app.db.backups.count_documents({}) == 0
    assert client.requests == []


async def test_forwarded_commands_are_ignored(app, account):
    event = FakeEvent(".save", chat_id=account.user.id, sender_id=account.user.id, fwd=True)
    await router.on_outgoing(app, event)
    assert event.edits == [] and await app.db.backups.count_documents({}) == 0


async def test_admin_commands_only_in_saved_messages(app, account):
    for text in ADMIN:
        event = FakeEvent(text, chat_id=555, sender_id=account.user.id)
        await router.on_outgoing(app, event)
        assert "in your Saved Messages" in event.text_out, text
    assert await app.db.backups.count_documents({}) == 0
    assert app.cfg["rio_pct"] == 55.0


async def test_bot_chat_slash_commands_are_left_to_the_bot(app, account):
    bot_chat = SimpleNamespace(bot=True, title="SomeBot")
    for text in ("/clear", "/stats", "/cancel", "/help", "/add 5 x"):
        event = FakeEvent(text, chat_id=4242, sender_id=account.user.id, chat=bot_chat)
        await router.on_outgoing(app, event)
        assert event.edits == [], text
    event = FakeEvent("/stats@SomeBot", chat_id=555, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert event.edits == []


async def test_cancel_without_reply_in_normal_chat_is_ignored(app, account):
    event = FakeEvent("/cancel", chat_id=555, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert event.edits == []


async def test_each_command_runs_once_even_if_delivered_twice(app, account):
    first = FakeEvent(".ping", chat_id=account.user.id, sender_id=account.user.id, msg_id=77)
    again = FakeEvent(".ping", chat_id=account.user.id, sender_id=account.user.id, msg_id=77)
    await router.on_outgoing(app, first)
    await router.on_outgoing(app, again)
    assert first.edits and again.edits == []
    claim = await app.db.command_claims.find_one({"_id": f"{account.user.id}:{account.user.id}:77"})
    assert claim["status"] == "completed"


async def test_setup_targets_only_the_current_account(app, account):
    event = FakeEvent(".setup", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "Target is always THIS account" in event.text_out and "(id 111)" in event.text_out


async def test_config_validates_values(app, account):
    event = FakeEvent(".config rio_pct 150", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "Invalid value" in event.text_out and app.cfg["rio_pct"] == 55.0
    event = FakeEvent(".config rio_pct 60", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert app.cfg["rio_pct"] == 60.0
    stored = await app.db.settings.find_one({"_id": 111})
    assert stored["rio_pct"] == 60.0
