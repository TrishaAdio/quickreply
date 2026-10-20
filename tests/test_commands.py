"""Smaller commands: channel setters, captions, archive status, backups list,
link source, .unban, .ping/.help, and the legacy ChannelGuard import."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl import types as T

from fakes import FakeEvent, jpeg
from qr.database import screenshots as shots
from qr.handlers import router
from qr.services import legacy_import, profile_backup


async def cmd(app, account, text, **kwargs):
    kwargs.setdefault("chat_id", account.user.id)
    event = FakeEvent(text, sender_id=account.user.id, **kwargs)
    await router.on_outgoing(app, event)
    return event


async def test_channel_setters_store_and_check_permissions(app, account, post_channel, archive_channel):
    event = await cmd(app, account, ".setpostchannel @newpostchannel")
    assert "Posting channel set: Posts (@newpostchannel)" in event.text_out
    assert app.cfg["publish_channel"] == "@newpostchannel"
    archive_channel.can_post = False
    event = await cmd(app, account, f".setarchive {archive_channel.chat_id}")
    assert "Archive channel set" in event.text_out and "cannot post there yet" in event.text_out
    event = await cmd(app, account, ".setpostchannel @nobody")
    assert "Couldn't resolve" in event.text_out
    # inside the channel, no argument
    inside = FakeEvent(".setpostchannel", chat_id=post_channel.chat_id, is_channel=True,
                       chat=post_channel.entity)
    await router.on_outgoing(app, inside)
    assert "Posting channel set" in inside.text_out


async def test_postcaption_preview_always_contains_date(app, account):
    event = await cmd(app, account, ".postcaption Proofs {part}/{parts}")
    assert "Posting caption saved" in event.text_out
    assert app.cfg["post_caption"] == "Proofs {part}/{parts}"
    from qr.services.posting_service import render_caption
    assert render_caption("Proofs {part}/{parts}", "2026-10-20", 1, 2, 10).startswith("2026-10-20\n")


async def test_archive_status_and_toggle(app, account):
    await shots.add(app.db, app.self_id, jpeg("x"), captured_at=datetime(2026, 10, 20, 6, tzinfo=timezone.utc),
                    source_chat_id=1, source_message_id=1)
    event = await cmd(app, account, ".archive")
    assert "2026-10-20: 1 pending" in event.text_out and "@ohthatsthepoint" in event.text_out
    await cmd(app, account, ".archive off")
    assert app.cfg["archive_enabled"] is False
    event = await cmd(app, account, ".archive run")
    assert "Built: -" in event.text_out


async def test_backups_list(app, account):
    await profile_backup.create_backup(app)
    event = await cmd(app, account, ".backups")
    assert "account 111" in event.text_out and "[manual, complete]" in event.text_out


async def test_linksource_resolves_to_numeric_id(app, account, client):
    client.users["@guard"] = SimpleNamespace(id=9001)
    event = await cmd(app, account, ".linksource @guard")
    assert app.cfg["link_source"] == 9001 and "9001" in event.text_out
    await cmd(app, account, ".linksource off")
    assert app.cfg["link_source"] is None


async def test_unban_lifts_bans_in_admin_groups(app, account, client, archive_channel):
    async def permissions(entity, user=None):
        return SimpleNamespace(is_banned=True)

    client.get_permissions = permissions
    event = await cmd(app, account, ".unban 5555", chat_id=777)
    assert "Unbanned 5555 in 2 of 2 chat(s)" in event.text_out
    assert sum(type(r).__name__ == "EditBannedRequest" for r in client.requests) == 2


async def test_ping_and_help(app, account):
    event = await cmd(app, account, ".ping", chat_id=777)
    assert "QuickReply is running" in event.text_out and "Posting channel: NOT SET" in event.text_out
    event = await cmd(app, account, ".help", chat_id=777)
    assert "<b>Backup / restore</b>" in event.text_out


def _legacy_dir(tmp_path):
    root = tmp_path / "channelguard"
    (root / "data").mkdir(parents=True)
    (root / "quickreply.py").write_text("# legacy")
    (root / "quickreply.session").write_bytes(b"SQLite format 3\x00legacy")
    (root / ".env").write_text("API_ID=777\nAPI_HASH=0123456789abcdef0123456789abcdef\nSHORTCUT=/promo\n"
                               "RIO_PCT=60\nMARCO_PCT=40\nPAYMENT_CHANNEL=-1001999\nGREET_NEW=0\nLINK_SOURCE=\n")
    (root / "data" / "pay.json").write_text(json.dumps({
        "post_channel": -1001888, "done_template": "Paid {amount} {link}",
        "done_entities": [{"_type": "MessageEntityBold", "offset": 0, "length": 4}],
        "payments": [
            {"order_id": "ANIAAA", "amount": "10.00", "ts": 1.0, "status": "valid", "post_message_id": 5,
             "invite_links": ["https://t.me/+secret"]},
            {"order_id": "ANIBBB", "amount": "20.00", "ts": 2.0, "status": "valid", "source_message_id": 9,
             "source_chat_id": 1},
            {"amount": "1"},
        ]}))
    (root / "data" / "greeting.json").write_text(json.dumps({"chat_id": 111, "message_id": 42}))
    (root / "data" / "greeted.json").write_text(json.dumps([1, 2, 3]))
    return root


async def test_legacy_import_reuses_old_quickreply_data(app, tmp_path, monkeypatch):
    from qr.config import settings as config
    monkeypatch.setattr(legacy_import, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    root = _legacy_dir(tmp_path)
    assert legacy_import.looks_like_legacy(root)
    assert legacy_import.legacy_credentials(root) == {"API_ID": "777",
                                                      "API_HASH": "0123456789abcdef0123456789abcdef"}
    assert legacy_import.copy_session(root).startswith("copied")
    assert legacy_import.copy_session(root) == "kept existing QuickReply session"
    report = await legacy_import.import_legacy(app, root)
    cfg = await app.settings.load(app.self_id)
    assert cfg["shortcut"] == "promo" and cfg["rio_pct"] == 60.0 and cfg["greet_new"] is False
    assert cfg["payment_channel"] == -1001888            # pay.json is the live store
    assert cfg["done_template"] == "Paid {amount} " and cfg["done_entities"]
    assert any("removed the {link} token" in line for line in report)
    assert cfg["greeting"] == {"chat_id": 111, "message_id": 42}
    assert await app.db.greeted.count_documents({}) == 3
    payments = {p["order_id"]: p async for p in app.db.payments.find({})}
    assert set(payments) == {"ANIAAA", "ANIBBB"}
    assert "invite_links" not in payments["ANIAAA"]       # bot/keyword data not imported
    assert payments["ANIBBB"]["post_status"] == "imported_unposted"   # never auto-posted
    assert any("payments: 2 imported" in line for line in report)
    again = await legacy_import.import_legacy(app, root)  # idempotent
    assert any("0 imported" in line for line in again)
    assert await app.db.payments.count_documents({}) == 2


async def test_imported_unposted_receipts_are_not_auto_posted(app, post_channel):
    from qr.handlers import payments
    await app.set(payment_channel=post_channel.chat_id)
    await app.db.payments.insert_one({"account_id": app.self_id, "order_id": "OLD1", "amount": "5",
                                      "ts": 1.0, "status": "valid", "source_chat_id": 1,
                                      "source_message_id": 2, "post_status": "imported_unposted"})
    await payments.recover_payments(app, interrupted_adds=True)
    assert post_channel.messages == []


async def test_save_recognises_custom_emoji_status_until(app, account):
    account.user.emoji_status = T.EmojiStatusUntil(document_id=1, until=datetime(2030, 1, 1, tzinfo=timezone.utc))
    await profile_backup.create_backup(app)
    backup = await app.db.backups.find_one({})
    assert backup["profile"]["emoji_status"]["document_id"] == 1
