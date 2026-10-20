""".save - account backup stored in MongoDB + GridFS."""
from __future__ import annotations

from telethon.tl import types as T

from fakes import FakeEvent, jpeg
from qr.database.mongo import MEDIA_BUCKET
from qr.handlers import router
from qr.services import profile_backup


def populate(account):
    """A realistic Premium business account."""
    full = account.full
    account.user.color = T.PeerColor(color=5, background_emoji_id=777)
    account.user.profile_color = T.PeerColor(color=9, background_emoji_id=None)
    account.user.emoji_status = T.EmojiStatus(document_id=4242)
    full.about = "Payments & support - DM me"
    full.birthday = T.Birthday(day=14, month=2, year=1995)
    account.add_profile_photo(jpeg("old-photo"))
    account.add_profile_photo(jpeg("current-photo"), video=b"\x00\x00\x00\x18ftypmp42video")
    full.fallback_photo = account.make_photo(jpeg("public-fallback"))
    bold = T.MessageEntityBold(offset=0, length=5)
    emoji = T.MessageEntityCustomEmoji(offset=6, length=2, document_id=5368324170671202286)
    link = T.MessageEntityTextUrl(offset=9, length=4, url="https://t.me/+abcDEF123")
    demo = account.add_shortcut("demo", [account.message("Hello \U0001F60A join", entities=[bold, emoji, link])])
    photo = account.make_photo(jpeg("price-card"))
    account.add_shortcut("price", [account.message("Price list", media=T.MessageMediaPhoto(photo=photo))])
    grouped = 9_000_001
    account.add_shortcut("album", [
        account.message("first", media=T.MessageMediaPhoto(photo=account.make_photo(jpeg("a1"))), grouped_id=grouped),
        account.message("", media=T.MessageMediaPhoto(photo=account.make_photo(jpeg("a2"))), grouped_id=grouped),
    ])
    pdf = account.make_document(b"%PDF-1.4 terms", "application/pdf", "terms.pdf")
    account.add_shortcut("terms", [account.message("Terms", media=T.MessageMediaDocument(document=pdf))])
    markup = T.ReplyInlineMarkup(rows=[T.KeyboardButtonRow(buttons=[T.KeyboardButtonUrl(text="Pay", url="https://pay.example")])])
    account.add_shortcut("pay", [account.message("Tap to pay", reply_markup=markup)])
    away = account.add_shortcut("away", [account.message("I'm away, back soon")])
    full.business_work_hours = T.BusinessWorkHours(timezone_id="Asia/Kolkata", weekly_open=[
        T.BusinessWeeklyOpen(start_minute=540, end_minute=1260)])
    full.business_location = T.BusinessLocation(address="MG Road, Bengaluru",
                                                geo_point=T.GeoPoint(long=77.6, lat=12.97, access_hash=0))
    full.business_intro = T.BusinessIntro(title="Welcome", description="Ask me anything")
    full.business_greeting_message = T.BusinessGreetingMessage(
        shortcut_id=demo, recipients=T.BusinessRecipients(new_chats=True), no_activity_days=14)
    full.business_away_message = T.BusinessAwayMessage(
        shortcut_id=away, schedule=T.BusinessAwayMessageScheduleAlways(),
        recipients=T.BusinessRecipients(existing_chats=True, new_chats=True), offline_only=True)
    account.chat_links.append(T.BusinessChatLink(link="https://t.me/m/xyz", message="Hi! I want to buy",
                                                 views=3, title="Buy"))


async def test_save_stores_profile_business_and_quick_replies(app, account):
    populate(account)
    await app.set(done_template="Thanks {name}", shortcut="demo")
    event = FakeEvent(".save", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "saved and verified (complete)" in event.text_out, event.text_out

    backup = await app.db.backups.find_one({"kind": "manual"})
    assert backup["schema_version"] == 1 and backup["account_id"] == 111
    assert backup["status"] == "complete" and backup["created_at"]
    profile = backup["profile"]
    assert (profile["first_name"], profile["last_name"], profile["username"]) == ("Alice", "Smith", "alice")
    assert profile["about"] == "Payments & support - DM me"
    assert profile["birthday"] == {"day": 14, "month": 2, "year": 1995}
    assert profile["color"] == {"color": 5, "background_emoji_id": 777}
    assert profile["emoji_status"]["document_id"] == 4242
    assert len(profile["photos"]) == 2 and profile["photos"][0]["is_current"]
    assert profile["photos"][0]["video_blob_id"] is not None
    assert profile["fallback_photo"]["sha256"]

    shortcuts = {s["name"]: s for s in backup["quick_replies"]}
    assert [s["name"] for s in backup["quick_replies"]] == ["demo", "price", "album", "terms", "pay", "away"]
    demo = shortcuts["demo"]["messages"][0]
    assert demo["text"] == "Hello \U0001F60A join"
    assert {e["_type"] for e in demo["entities"]} == {"MessageEntityBold", "MessageEntityCustomEmoji",
                                                      "MessageEntityTextUrl"}
    assert demo["custom_emoji_ids"] == [5368324170671202286]
    assert shortcuts["demo"]["command"] == "/demo"
    assert shortcuts["price"]["messages"][0]["media"]["kind"] == "photo"
    assert len({m["grouped_id"] for m in shortcuts["album"]["messages"]}) == 1
    assert shortcuts["terms"]["messages"][0]["media"]["file_name"] == "terms.pdf"
    assert shortcuts["pay"]["messages"][0]["reply_markup"]["_"] == "ReplyInlineMarkup"

    business = backup["business"]
    assert business["greeting"]["shortcut_name"] == "demo"
    assert business["away"]["shortcut_name"] == "away"
    assert business["location"]["address"] == "MG Road, Bengaluru"
    assert business["work_hours"]["timezone_id"] == "Asia/Kolkata"
    assert business["chat_links"][0]["title"] == "Buy"
    assert backup["app_settings"]["done_template"] == "Thanks {name}"

    fields = backup["fields"]
    assert fields["saved_locations"]["status"] == "unsupported"
    assert fields["profile_photos"] == {"status": "saved", "count": 2}
    assert fields["personal_channel"]["status"] == "empty"
    # every referenced media object is in GridFS and matches its checksum
    for blob_id, sha in profile_backup.media_refs(backup):
        assert await app.db.verify_blob(MEDIA_BUCKET, blob_id, sha)


async def test_repeated_save_does_not_duplicate(app, account):
    populate(account)
    first = await profile_backup.create_backup(app)
    files_after_first = await app.db.db[f"{MEDIA_BUCKET}.files"].count_documents({})
    second = await profile_backup.create_backup(app)
    assert second["unchanged"] and second["backup_id"] == first["backup_id"]
    assert await app.db.backups.count_documents({}) == 1
    assert await app.db.db[f"{MEDIA_BUCKET}.files"].count_documents({}) == files_after_first
    account.full.about = "new bio"
    third = await profile_backup.create_backup(app)
    assert not third["unchanged"] and await app.db.backups.count_documents({}) == 2


async def test_failed_field_makes_backup_partial_and_is_reported(app, account, client):
    populate(account)
    client.fail_requests["GetUserPhotosRequest"] = RuntimeError("photos unavailable")
    event = FakeEvent(".save", chat_id=account.user.id, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    assert "PARTIAL" in event.text_out and "profile_photos" in event.text_out
    backup = await app.db.backups.find_one({})
    assert backup["status"] == "partial"
    assert backup["fields"]["profile_photos"]["status"] == "failed"
    assert backup["profile"]["photos"] == []        # never claims photos it did not get


async def test_non_premium_account_reports_quick_replies_unsupported(app, account, client):
    account.user.premium = False
    client.fail_requests["GetQuickRepliesRequest"] = RuntimeError("PREMIUM_ACCOUNT_REQUIRED")
    client.fail_requests["GetBusinessChatLinksRequest"] = RuntimeError("PREMIUM_ACCOUNT_REQUIRED")
    result = await profile_backup.create_backup(app)
    backup = await app.db.backups.find_one({})
    assert result["status"] == "complete"
    assert backup["fields"]["quick_replies"]["status"] == "unsupported"
    assert backup["fields"]["business_chat_links"]["status"] == "unsupported"
