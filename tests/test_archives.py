"""Daily screenshot archives: IST boundaries, ZIP build/verify, delivery, purge."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from fakes import jpeg, png
from qr.config.settings import IST
from qr.database import screenshots as shots
from qr.database.mongo import ARCHIVE_BUCKET, SCREENSHOT_BUCKET
from qr.services import archive_service as A
from qr.services.clock import day_is_complete, day_window, ist_date

UTC = timezone.utc
AFTER = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)  # both test days are over


# ---------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------
def test_day_window_is_ist_midnight_to_midnight():
    start, end = day_window("2026-10-20")
    assert start == datetime(2026, 10, 20, 0, 0, tzinfo=IST).astimezone(UTC)
    assert start == datetime(2026, 10, 19, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 10, 20, 18, 30, tzinfo=UTC)
    assert end - start == timedelta(days=1)


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 10, 20, 0, 0, 0, tzinfo=IST), "2026-10-20"),            # start inclusive
    (datetime(2026, 10, 20, 23, 59, 59, 999999, tzinfo=IST), "2026-10-20"),
    (datetime(2026, 10, 21, 0, 0, 0, tzinfo=IST), "2026-10-21"),            # end exclusive
    (datetime(2026, 10, 19, 18, 29, 59, tzinfo=UTC), "2026-10-19"),
    (datetime(2026, 10, 19, 18, 30, 0, tzinfo=UTC), "2026-10-20"),
    (datetime(2026, 10, 20, 18, 29, 59, tzinfo=UTC), "2026-10-20"),
])
def test_date_assignment_at_midnight(moment, expected):
    assert ist_date(moment) == expected


def test_day_is_complete_only_after_next_ist_midnight():
    assert not day_is_complete("2026-10-20", datetime(2026, 10, 20, 18, 29, 59, tzinfo=UTC))
    assert day_is_complete("2026-10-20", datetime(2026, 10, 20, 18, 30, tzinfo=UTC))


# ---------------------------------------------------------------------------
# ZIP build / inspection
# ---------------------------------------------------------------------------
def _doc(tag, minute):
    from qr.database.mongo import sha256_hex
    data = jpeg(tag)
    return ({"sha256": sha256_hex(data), "extension": ".jpg",
             "captured_at": datetime(2026, 10, 20, 5, minute, tzinfo=UTC)}, data)


def test_build_zip_manifest_counts_and_roundtrip():
    items = [_doc("b", 2), _doc("a", 1)]
    data, manifest = A.build_zip("2026-10-20", 1, items)
    A.verify_zip(data, manifest)
    assert manifest["item_count"] == 2 and manifest["archive_date"] == "2026-10-20"
    assert manifest["window_start"].startswith("2026-10-19T18:30")
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(archive.namelist())[0].startswith("0001_")
    inspected = A.inspect_zip(data, A.archive_filename("2026-10-20"))
    assert inspected.archive_date == "2026-10-20" and len(inspected.items) == 2
    assert not inspected.rejected
    # manifest holds only safe ids - no order ids / names
    assert set(manifest["items"][0]) == {"file", "id", "sha256", "size", "captured_at"}


def _zip(entries: dict, *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


NAME = "payment_screenshots_2026-10-20.zip"


@pytest.mark.parametrize("entries,kwargs,reason", [
    ({"../evil.jpg": jpeg("x")}, {}, "path traversal"),
    ({"/abs.jpg": jpeg("x")}, {}, "absolute path"),
    ({"C:/win.jpg": jpeg("x")}, {}, "absolute path"),
    ({"run.exe": b"MZ..."}, {}, "executable"),
    ({"script.sh": b"#!/bin/sh"}, {}, "executable"),
    ({"hidden.jpg": b"MZ\x90\x00 not an image"}, {}, "executable content"),
    ({"a.jpg": jpeg("x")}, {"symlink": "link.jpg"}, "symbolic link"),
    ({"manifest.json": b"{bad json"}, {}, "invalid manifest"),
])
def test_malicious_or_malformed_archives_are_rejected(entries, kwargs, reason):
    with pytest.raises(A.ArchiveRejected, match=reason):
        A.inspect_zip(_zip(entries, **kwargs), NAME)


def test_non_zip_and_undated_archives_are_rejected():
    with pytest.raises(A.ArchiveRejected, match="not a valid ZIP"):
        A.inspect_zip(b"plain text", NAME)
    with pytest.raises(A.ArchiveRejected, match="cannot determine the archive date"):
        A.inspect_zip(_zip({"a.jpg": jpeg("a")}), "random.zip")


def test_manifest_and_filename_must_agree():
    data, _manifest = A.build_zip("2026-10-20", 1, [_doc("a", 1)])
    with pytest.raises(A.ArchiveRejected, match="does not match manifest"):
        A.inspect_zip(data, "payment_screenshots_2026-10-21.zip")


def test_checksum_tampering_is_rejected():
    data, manifest = A.build_zip("2026-10-20", 1, [_doc("a", 1)])
    source = zipfile.ZipFile(io.BytesIO(data))
    tampered = {n: source.read(n) for n in source.namelist()}
    member = manifest["items"][0]["file"]
    tampered[member] = jpeg("different")
    with pytest.raises(A.ArchiveRejected, match="checksum mismatch"):
        A.inspect_zip(_zip(tampered), NAME)


def test_unsupported_files_are_reported_not_silently_skipped():
    inspected = A.inspect_zip(_zip({"a.jpg": jpeg("a"), "notes.txt": b"hello", "b.png": png("b"),
                                    "__MACOSX/._a.jpg": b"meta"}), NAME)
    assert [i.original for i in inspected.items] == ["a.jpg", "b.png"]
    reasons = dict(inspected.rejected)
    assert "notes.txt" in reasons and "__MACOSX/._a.jpg" in reasons


def test_zip_bomb_ratio_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.jpg", b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024))
    with pytest.raises(A.ArchiveRejected, match="compression ratio"):
        A.inspect_zip(buffer.getvalue(), NAME)


# ---------------------------------------------------------------------------
# MongoDB-backed lifecycle
# ---------------------------------------------------------------------------
async def _add(app, tag, when, msg_id):
    return await shots.add(app.db, app.self_id, jpeg(tag), captured_at=when,
                           source_chat_id=5, source_message_id=msg_id)


async def test_screenshots_are_deduplicated_and_dated_by_receipt_time(app):
    late_evening = datetime(2026, 10, 20, 23, 59, tzinfo=IST)
    doc, created = await _add(app, "a", late_evening, 1)
    assert created and doc["archive_date"] == "2026-10-20"
    _doc2, created = await _add(app, "a", late_evening, 1)     # same message again
    assert not created
    _doc3, created = await _add(app, "a", late_evening, 2)     # same bytes, other message
    assert not created
    assert await app.db.screenshots.count_documents({}) == 1


async def test_today_is_not_archived_until_it_ends(app):
    await _add(app, "a", datetime(2026, 10, 20, 12, 0, tzinfo=IST), 1)
    summary = await A.run_cycle(app, now=datetime(2026, 10, 20, 18, 0, tzinfo=UTC))
    assert summary["built"] == []
    with pytest.raises(ValueError, match="has not ended"):
        await A.build_for_date(app, "2026-10-20", now=datetime(2026, 10, 20, 18, 0, tzinfo=UTC))


async def test_full_cycle_builds_delivers_verifies_then_purges(app, client, archive_channel):
    await _add(app, "a", datetime(2026, 10, 20, 0, 0, tzinfo=IST), 1)
    await _add(app, "b", datetime(2026, 10, 20, 23, 59, 59, tzinfo=IST), 2)
    await _add(app, "c", datetime(2026, 10, 21, 0, 0, tzinfo=IST), 3)   # next day
    summary = await A.run_cycle(app, now=AFTER)
    assert summary["errors"] == []
    archive = await app.db.archives.find_one({"archive_date": "2026-10-20"})
    assert archive["status"] == "purged" and archive["item_count"] == 2
    assert archive["filename"] == "payment_screenshots_2026-10-20.zip"
    assert archive["delivery"]["verified"] and archive["delivery"]["message_id"]
    # delivered document is a valid ZIP with the manifest
    delivered = archive_channel.messages[0]
    data = client.account.media[delivered.media.document.id]
    inspected = A.inspect_zip(data, archive["filename"])
    assert len(inspected.items) == 2 and inspected.archive_date == "2026-10-20"
    # sources deleted, other day separate, audit kept
    assert await app.db.screenshots.count_documents({"archive_date": "2026-10-20"}) == 0
    assert await app.db.db[f"{SCREENSHOT_BUCKET}.files"].count_documents({}) == 0
    assert await app.db.db[f"{ARCHIVE_BUCKET}.files"].count_documents({}) == 0
    assert await app.db.archives.count_documents({"archive_date": "2026-10-21"}) == 1
    actions = {a["action"] async for a in app.db.audit.find({})}
    assert {"archive_built", "archive_delivered", "archive_purged"} <= actions


async def test_delivery_failure_keeps_every_screenshot(app, client, archive_channel):
    await _add(app, "a", datetime(2026, 10, 20, 9, 0, tzinfo=IST), 1)
    archive_channel.can_post = False
    summary = await A.run_cycle(app, now=AFTER)
    assert summary["errors"]
    archive = await app.db.archives.find_one({})
    assert archive["status"] == "failed" and "cannot post" in archive["delivery"]["error"]
    assert await app.db.screenshots.count_documents({}) == 1
    assert await app.db.db[f"{SCREENSHOT_BUCKET}.files"].count_documents({}) == 1
    # retry after the back-off once permissions are fixed
    archive_channel.can_post = True
    await A.run_cycle(app, now=datetime.now(UTC) + timedelta(hours=2))
    archive = await app.db.archives.find_one({})
    assert archive["status"] == "purged"
    assert len(archive_channel.messages) == 1


async def test_crash_after_telegram_accepts_is_reconciled_without_duplicate(app, client, archive_channel):
    await _add(app, "a", datetime(2026, 10, 20, 9, 0, tzinfo=IST), 1)
    client.lose_reply_once = True          # upload lands, but we never get the reply
    await A.run_cycle(app, now=AFTER)
    archive = await app.db.archives.find_one({})
    assert archive["status"] == "delivering"
    assert await app.db.screenshots.count_documents({}) == 1   # nothing deleted yet
    await A.run_cycle(app, now=datetime.now(UTC) + timedelta(hours=2))
    archive = await app.db.archives.find_one({})
    assert archive["status"] == "purged"
    assert len(archive_channel.messages) == 1   # reconciled, not re-sent


async def test_late_screenshot_after_delivery_goes_into_part_two(app, archive_channel):
    await _add(app, "a", datetime(2026, 10, 20, 9, 0, tzinfo=IST), 1)
    await A.run_cycle(app, now=AFTER)
    await _add(app, "late", datetime(2026, 10, 20, 22, 0, tzinfo=IST), 2)
    await A.run_cycle(app, now=AFTER)
    names = sorted([a["filename"] async for a in app.db.archives.find({})])
    assert names == ["payment_screenshots_2026-10-20.zip", "payment_screenshots_2026-10-20_part2.zip"]
    assert len(archive_channel.messages) == 2


async def test_purge_refuses_without_verified_delivery(app):
    await _add(app, "a", datetime(2026, 10, 20, 9, 0, tzinfo=IST), 1)
    await A.build_for_date(app, "2026-10-20", now=AFTER)
    archive = await app.db.archives.find_one({})
    ok, detail = await A.purge(app, archive)
    assert not ok and "not verified" in detail
    assert await app.db.screenshots.count_documents({}) == 1


async def test_interrupted_build_resumes_with_same_items(app):
    await _add(app, "a", datetime(2026, 10, 20, 9, 0, tzinfo=IST), 1)
    # simulate a crash after claiming: archive 'building', screenshot 'archiving'
    key = A.archive_key(app.self_id, "2026-10-20", 1)
    await app.db.archives.insert_one({"_id": key, "account_id": app.self_id, "archive_date": "2026-10-20",
                                      "part": 1, "filename": A.archive_filename("2026-10-20"),
                                      "status": "building", "lease_owner": None, "lease_until": None,
                                      "delivery": {"attempts": 0}})
    await app.db.screenshots.update_many({}, {"$set": {"status": "archiving", "archive_id": key}})
    built = await A.build_for_date(app, "2026-10-20", now=AFTER)
    assert built == [key]
    archive = await app.db.archives.find_one({"_id": key})
    assert archive["status"] == "built" and archive["item_count"] == 1
    assert await app.db.archives.count_documents({}) == 1
