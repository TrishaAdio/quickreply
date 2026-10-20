""".setup - restore a backup onto the current (target) account."""
from __future__ import annotations

import re
from datetime import timedelta

from telethon.tl import types as T

from conftest import make_app
from fakes import FakeAccount, FakeChannel, FakeClient, FakeEvent
from qr.database.mongo import utcnow
from qr.handlers import router
from qr.services import profile_backup, profile_restore
from test_backup import populate


async def source_backup(db):
    source = FakeAccount(111)
    populate(source)
    app = make_app(FakeClient(source), db, source)
    await app.load_settings()
    await app.register_account()
    await app.set(done_template="Thanks {name}!", rio_pct=60.0)
    result = await profile_backup.create_backup(app)
    return source, result["backup_id"]


async def target(db, premium=True):
    account = FakeAccount(222, first="Bob", last="", username="bob", premium=premium)
    client = FakeClient(account, [FakeChannel(-1001000000009, "Mine", "mine")])
    client.occupied = {"alice"}                # source still owns @alice
    app = make_app(client, db, account)
    await app.load_settings()
    await app.register_account()
    return account, client, app


async def send(app, account, text):
    event = FakeEvent(text, chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    return event


async def test_preview_changes_nothing_then_confirm_restores(db):
    _source, backup_id = await source_backup(db)
    account, client, app = await target(db)
    preview = await send(app, account, ".setup 111")
    text = preview.text_out
    assert "RESTORE PREVIEW" in text and "Target: Bob @bob (id 222)" in text
    assert "will change: first_name" in text
    code = re.search(r"\.setup confirm (\w+)", text).group(1)
    assert account.user.first_name == "Bob" and account.shortcuts == {}   # dry run only

    done = await send(app, account, f".setup confirm {code}")
    report = done.text_out
    assert "RESTORE PARTIAL" in report and "FAILED: username" in report   # @alice still taken
    assert "Rollback: .setup " in report

    # profile
    assert (account.user.first_name, account.user.last_name) == ("Alice", "Smith")
    assert account.full.about == "Payments & support - DM me"
    assert (account.full.birthday.day, account.full.birthday.month) == (14, 2)
    assert account.user.color.color == 5 and account.user.color.background_emoji_id == 777
    assert account.user.emoji_status.document_id == 4242
    assert len(account.photos) == 2
    assert account.full.fallback_photo is not None
    # current photo uploaded last -> it is current; it was a video avatar
    assert client.account.video.get(account.full.profile_photo.id)
    # quick replies, order, formatting, media, album
    assert account.shortcut_order == ["demo", "price", "album", "terms", "pay", "away"]
    demo = account.shortcuts["demo"]["messages"][0]
    assert demo.message == "Hello \U0001F60A join"
    assert {type(e).__name__ for e in demo.entities} == {"MessageEntityBold", "MessageEntityCustomEmoji",
                                                         "MessageEntityTextUrl"}
    assert isinstance(account.shortcuts["price"]["messages"][0].media, T.MessageMediaPhoto)
    album = account.shortcuts["album"]["messages"]
    assert len(album) == 2 and album[0].grouped_id == album[1].grouped_id
    terms = account.shortcuts["terms"]["messages"][0].media.document
    assert any(getattr(a, "file_name", "") == "terms.pdf" for a in terms.attributes)
    assert "inline buttons cannot be attached" in report
    # business mapped onto the NEW shortcut ids
    assert account.full.business_greeting_message.shortcut_id == account.shortcuts["demo"]["id"]
    assert account.full.business_away_message.shortcut_id == account.shortcuts["away"]["id"]
    assert account.full.business_location.address == "MG Road, Bengaluru"
    assert account.full.business_intro.title == "Welcome"
    assert account.full.business_work_hours.timezone_id == "Asia/Kolkata"
    assert [l.title for l in account.chat_links] == ["Buy"]
    # QuickReply settings follow
    assert app.cfg["done_template"] == "Thanks {name}!" and app.cfg["rio_pct"] == 60.0

    history = await db.restore_history.find_one({"account_id": 222})
    assert history["status"] == "partial" and history["fields"]["username"]["status"] == "failed"
    assert await db.backups.count_documents({"account_id": 222, "kind": "pre_restore"}) == 1


async def test_second_restore_reports_already_correct(db):
    _source, backup_id = await source_backup(db)
    account, client, app = await target(db)
    backup = await db.backups.find_one({})
    await profile_restore.run(app, backup, dry_run=False)
    uploads = sum(1 for r in client.requests if type(r).__name__ == "UploadProfilePhotoRequest")
    report = await profile_restore.run(app, backup, dry_run=False)
    statuses = {k: v["status"] for k, v in report.fields.items()}
    for field in ("first_name", "about", "birthday", "color", "emoji_status", "profile_photos",
                  "quick_reply /demo", "quick_reply /album", "business_location", "business_greeting",
                  "business_chat_links", "quickreply_settings"):
        assert statuses[field] == "already_correct", (field, statuses[field])
    assert statuses["fallback_photo"] == "already_correct"
    # nothing is uploaded twice
    assert sum(1 for r in client.requests if type(r).__name__ == "UploadProfilePhotoRequest") == uploads


async def test_non_premium_target_reports_unsupported_fields(db):
    await source_backup(db)
    account, _client, app = await target(db, premium=False)
    backup = await db.backups.find_one({})
    report = await profile_restore.run(app, backup, dry_run=False)
    for field in ("quick_replies", "business_location", "emoji_status", "color", "business_greeting"):
        assert report.fields[field]["status"] == "unsupported"
        assert "Premium" in report.fields[field]["reason"]
    assert report.fields["first_name"]["status"] == "restored"
    assert account.shortcuts == {}


async def test_confirmation_code_is_single_use_and_expires(db):
    await source_backup(db)
    account, _client, app = await target(db)
    preview = await send(app, account, ".setup 111")
    code = re.search(r"\.setup confirm (\w+)", preview.text_out).group(1)
    wrong = await send(app, account, ".setup confirm ZZZZZZ")
    assert "invalid, expired or already used" in wrong.text_out
    await db.restore_sessions.update_one({"code": code}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}})
    expired = await send(app, account, f".setup confirm {code}")
    assert "invalid, expired or already used" in expired.text_out
    assert account.user.first_name == "Bob"


async def test_unregistered_source_is_refused(db):
    await source_backup(db)
    account, _client, app = await target(db)
    await db.accounts.delete_one({"_id": 111})
    event = await send(app, account, ".setup 111")
    assert "not a registered QuickReply account" in event.text_out
    event = await send(app, account, ".setup 999")
    assert "has no backup" in event.text_out


async def test_failed_pre_restore_backup_aborts_without_changes(db, monkeypatch):
    await source_backup(db)
    account, _client, app = await target(db)
    preview = await send(app, account, ".setup 111")
    code = re.search(r"\.setup confirm (\w+)", preview.text_out).group(1)

    async def broken(*args, **kwargs):
        raise RuntimeError("mongo write failed")

    monkeypatch.setattr(profile_backup, "create_backup", broken)
    event = await send(app, account, f".setup confirm {code}")
    assert "nothing was changed" in event.text_out
    assert account.user.first_name == "Bob"


async def test_partial_quick_reply_failure_does_not_claim_success(db):
    await source_backup(db)
    account, client, app = await target(db)
    client.fail_requests["SendMultiMediaRequest"] = RuntimeError("MEDIA_INVALID")
    backup = await db.backups.find_one({})
    report = await profile_restore.run(app, backup, dry_run=False)
    assert report.fields["quick_reply /album"]["status"] == "failed"
    assert report.fields["quick_reply /demo"]["status"] == "restored"
    assert "quick_reply /album" in report.failed
