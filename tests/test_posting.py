""".post / .postbatch / .postnow against fake Telegram + real MongoDB."""
from __future__ import annotations

import asyncio
import glob
import os
import tempfile
from datetime import datetime, timedelta, timezone

from telethon.errors import RPCError
from telethon.tl import types as T

from fakes import FakeEvent, flood, jpeg
from qr.database import screenshots as shots
from qr.database.mongo import sha256_hex
from qr.handlers import router
from qr.services import archive_service as A, posting_service as PS


def zip_for(day: str, count: int, tag: str = "") -> bytes:
    items = []
    for index in range(count):
        data = jpeg(f"{day}-{tag}-{index}")
        items.append(({"sha256": sha256_hex(data), "extension": ".jpg",
                       "captured_at": datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
                       + timedelta(minutes=index)}, data))
    return A.build_zip(day, 1, items)[0]


def zip_message(account, data: bytes, name: str):
    doc = account.make_document(data, "application/zip", name)
    message = account.message("", media=T.MessageMediaDocument(document=doc))
    account.saved.append(message)
    return message


async def post_cmd(app, account, text, reply=None):
    event = FakeEvent(text, chat_id=account.user.id, reply=reply, sender_id=account.user.id)
    await router.on_outgoing(app, event)
    return event


def captions(channel):
    return [m.message for m in channel.messages if m.message]


async def test_post_requires_a_posting_channel(app, account):
    message = zip_message(account, zip_for("2026-10-20", 1), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".post", reply=message)
    assert "No posting channel configured" in event.text_out


async def test_post_publishes_one_archive_in_albums_with_dated_captions(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    message = zip_message(account, zip_for("2026-10-20", 12), "payment_screenshots_2026-10-20.zip")
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "quickreply-post-*")))
    event = await post_cmd(app, account, ".post", reply=message)
    assert "All archives posted." in event.text_out, event.text_out
    assert len(post_channel.messages) == 12
    groups = {m.grouped_id for m in post_channel.messages}
    assert len(groups) == 2                                 # 10 + 2 albums
    assert all("2026-10-20" in c for c in captions(post_channel))
    assert ["Part 1/2" in captions(post_channel)[0], "Part 2/2" in captions(post_channel)[1]] == [True, True]
    job = await app.db.post_jobs.find_one({})
    assert job["status"] == "posted" and all(i["status"] == "posted" for i in job["items"])
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "quickreply-post-*")))
    assert after == before                                  # temp dir cleaned up


async def test_post_rejects_malicious_zip_without_posting(app, account, post_channel):
    import io
    import zipfile
    await app.set(publish_channel=post_channel.chat_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../etc/x.jpg", jpeg("x"))
    message = zip_message(account, buffer.getvalue(), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".post", reply=message)
    assert "Archive rejected" in event.text_out and "path traversal" in event.text_out
    assert post_channel.messages == []


async def test_same_archive_is_never_posted_twice(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    data = zip_for("2026-10-20", 3)
    await post_cmd(app, account, ".post", reply=zip_message(account, data, "payment_screenshots_2026-10-20.zip"))
    event = await post_cmd(app, account, ".post",
                           reply=zip_message(account, data, "payment_screenshots_2026-10-20.zip"))
    assert "already posted earlier" in event.text_out
    assert len(post_channel.messages) == 3


async def test_postbatch_sorts_oldest_first_and_reports_failures(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    first = zip_message(account, zip_for("2026-10-21", 1), "payment_screenshots_2026-10-21.zip")
    zip_message(account, b"not a zip at all", "payment_screenshots_2026-10-18.zip")
    zip_message(account, zip_for("2026-10-19", 2), "payment_screenshots_2026-10-19.zip")
    zip_message(account, zip_for("2026-10-20", 1), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".postbatch", reply=first)
    days = [c.splitlines()[0].rsplit(" ", 1)[-1] for c in captions(post_channel)]
    assert days == ["2026-10-19", "2026-10-20", "2026-10-21"]
    assert "3 of 4 archive(s) fully posted" in event.text_out
    assert "payment_screenshots_2026-10-18.zip" in event.text_out
    assert "All archives posted" not in event.text_out


async def test_flood_wait_is_respected_and_job_resumes_without_duplicates(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    app.client.send_failures = [flood(5), None, flood(5000)]   # short wait, ok, long wait
    message = zip_message(account, zip_for("2026-10-20", 12), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".post", reply=message)
    assert 6 in app.slept                                   # slept through the 5s flood
    assert "PENDING" in event.text_out and "rate limit" in event.text_out
    assert len(post_channel.messages) == 10
    event = await post_cmd(app, account, ".postnow")
    assert "All archives posted." in event.text_out, event.text_out
    assert len(post_channel.messages) == 12


async def test_ambiguous_upload_is_reconciled_not_reposted(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    app.client.lose_reply_once = True
    message = zip_message(account, zip_for("2026-10-20", 12), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".post", reply=message)
    assert "FAILED" in event.text_out
    job = await app.db.post_jobs.find_one({})
    assert job["chunks"][0]["status"] == "sending"
    event = await post_cmd(app, account, ".postnow")
    assert "All archives posted." in event.text_out, event.text_out
    assert len(post_channel.messages) == 12                 # first album not re-sent


async def test_rejected_album_falls_back_to_single_items(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    app.client.send_failures = [RPCError(None, "PHOTO_INVALID_DIMENSIONS", 400)]
    message = zip_message(account, zip_for("2026-10-20", 3), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".post", reply=message)
    assert "All archives posted." in event.text_out, event.text_out
    assert len(post_channel.messages) == 3


async def test_failed_items_are_not_marked_published(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    reject = lambda: RPCError(None, "MEDIA_INVALID", 400)  # noqa: E731
    app.client.send_failures = [reject(), reject(), reject(), None, None]
    message = zip_message(account, zip_for("2026-10-20", 2), "payment_screenshots_2026-10-20.zip")
    event = await post_cmd(app, account, ".post", reply=message)
    job = await app.db.post_jobs.find_one({})
    statuses = sorted(i["status"] for i in job["items"])
    assert statuses == ["failed", "posted"]
    assert job["status"] == "partial" and "PARTIAL" in event.text_out


async def test_concurrent_workers_do_not_double_post(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    data = zip_for("2026-10-20", 4)
    inspected = A.inspect_zip(data, "payment_screenshots_2026-10-20.zip")
    message = zip_message(account, data, "payment_screenshots_2026-10-20.zip")
    job, _ = await PS.create_job(app, inspected, {"chat_id": account.user.id, "message_id": message.id})
    from conftest import make_app
    other = make_app(app.client, app.db, account)
    await other.load_settings()
    first, second = await asyncio.gather(PS.run_job(app, job["_id"]), PS.run_job(other, job["_id"]))
    assert sorted([first.status, second.status]) == ["busy", "posted"]
    assert len(post_channel.messages) == 4


async def test_postnow_checks_destination_permissions(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    post_channel.can_post = False
    event = await post_cmd(app, account, ".postnow")
    assert "Cannot post" in event.text_out


async def test_postnow_with_nothing_pending_says_so(app, account, post_channel):
    await app.set(publish_channel=post_channel.chat_id)
    event = await post_cmd(app, account, ".postnow")
    assert "Nothing to post" in event.text_out


async def test_postnow_publishes_delivered_daily_archives(app, account, post_channel, archive_channel):
    await app.set(publish_channel=post_channel.chat_id)
    for index, day in enumerate(("2026-10-19", "2026-10-18")):
        await shots.add(app.db, app.self_id, jpeg(day), source_chat_id=1, source_message_id=index,
                        captured_at=datetime.fromisoformat(f"{day}T06:00:00+00:00"))
    await A.run_cycle(app, now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert len(archive_channel.messages) == 2
    assert await app.db.screenshots.count_documents({}) == 0     # purged after delivery
    event = await post_cmd(app, account, ".postnow")
    assert "All archives posted." in event.text_out, event.text_out
    assert [c.splitlines()[0].rsplit(" ", 1)[-1] for c in captions(post_channel)] == ["2026-10-18", "2026-10-19"]
    archive = await app.db.archives.find_one({"archive_date": "2026-10-18"})
    assert archive["post_status"] == "posted"
    event = await post_cmd(app, account, ".postnow")
    assert "Nothing to post" in event.text_out
