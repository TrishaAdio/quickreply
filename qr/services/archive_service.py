"""Daily payment-screenshot archives.

Lifecycle of one archive (``archives`` collection, ``_id = acct:date:part``)::

    building -> built -> delivering -> delivered -> purged
                  ^           |
                  +-- failed <+   (retried with back-off; sources kept)

* Days are Asia/Kolkata calendar days: ``[00:00, next 00:00)``.
* A day is archived only after it has completely ended.
* Source screenshots are deleted **only** after the ZIP was delivered to the
  archive channel and the delivered document was read back and matched
  (size + filename). A crash between "Telegram accepted" and "recorded" is
  reconciled by looking for that document in the channel before re-sending.
"""
from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from qr import ui
from qr.database import screenshots as shots
from qr.database.mongo import ARCHIVE_BUCKET, SCREENSHOT_BUCKET, sha256_hex, utcnow
from qr.services.clock import day_is_complete, day_window, parse_day
from qr.services.telegram_utils import FloodDeferred, can_post_media, flood_safe, resolve_chat

ARCHIVE_NAME_RE = re.compile(
    r"^payment_screenshots_(\d{4}-\d{2}-\d{2})(?:_part(\d+))?\.zip$", re.IGNORECASE)
MAX_ITEMS_PER_ARCHIVE = 1000
LEASE_SECONDS = 600
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1


def archive_filename(day: str, part: int = 1) -> str:
    return f"payment_screenshots_{day}.zip" if part == 1 else f"payment_screenshots_{day}_part{part}.zip"


def archive_key(account_id: int, day: str, part: int) -> str:
    return f"{int(account_id)}:{day}:{int(part)}"


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * (2 ** min(max(attempts - 1, 0), 7))))


# ---------------------------------------------------------------------------
# ZIP creation + verification
# ---------------------------------------------------------------------------
def build_zip(day: str, part: int, items: list[tuple[dict, bytes]]) -> tuple[bytes, dict]:
    """Create the ZIP (images + manifest). ``items`` = ``[(screenshot_doc, bytes)]``."""
    start, end = day_window(day)
    entries = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, (doc, data) in enumerate(sorted(items, key=lambda i: (i[0]["captured_at"], i[0]["sha256"])), 1):
            name = f"{index:04d}_{doc['sha256'][:12]}{doc.get('extension') or '.jpg'}"
            archive.writestr(zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0)), data,
                             compress_type=zipfile.ZIP_STORED)
            entries.append({
                "file": name,
                "id": doc["sha256"][:16],
                "sha256": doc["sha256"],
                "size": len(data),
                "captured_at": doc["captured_at"].isoformat(),
            })
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "archive_date": day,
            "part": part,
            "timezone": "Asia/Kolkata",
            "window_start": start.isoformat(),
            "window_end_exclusive": end.isoformat(),
            "created_at": utcnow().isoformat(),
            "item_count": len(entries),
            "items": entries,
        }
        archive.writestr(zipfile.ZipInfo(MANIFEST_NAME, date_time=(2000, 1, 1, 0, 0, 0)),
                         json.dumps(manifest, indent=2), compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue(), manifest


def verify_zip(data: bytes, expected: dict) -> None:
    """Re-open a freshly built ZIP and check every member against the manifest."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"corrupt member {bad}")
        manifest = json.loads(archive.read(MANIFEST_NAME))
        if manifest["item_count"] != expected["item_count"] or \
                manifest["archive_date"] != expected["archive_date"]:
            raise ValueError("manifest mismatch")
        names = set(archive.namelist()) - {MANIFEST_NAME}
        if len(names) != manifest["item_count"]:
            raise ValueError("member count does not match manifest")
        for item in manifest["items"]:
            if sha256_hex(archive.read(item["file"])) != item["sha256"]:
                raise ValueError(f"checksum mismatch for {item['file']}")


# ---------------------------------------------------------------------------
# Safe inspection of untrusted ZIPs (.post / .postbatch)
# ---------------------------------------------------------------------------
class ArchiveRejected(ValueError):
    """The whole archive is unsafe or malformed; nothing is extracted."""


MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 1000
MAX_TOTAL_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_EXTS = {".jpg", ".jpeg", ".png"}
DOCUMENT_IMAGE_EXTS = {".webp"}
EXECUTABLE_EXTS = {
    ".exe", ".dll", ".bat", ".cmd", ".com", ".scr", ".msi", ".ps1", ".vbs", ".vbe", ".js",
    ".jse", ".jar", ".sh", ".bash", ".py", ".pyc", ".pyw", ".so", ".dylib", ".apk", ".app",
    ".bin", ".elf", ".lnk", ".reg", ".hta", ".wsf", ".cpl", ".msc", ".pif", ".run", ".deb",
    ".rpm", ".php", ".pl", ".rb",
}


@dataclass
class ArchiveItem:
    name: str          # safe generated name, e.g. 0001.jpg
    original: str      # member name inside the ZIP (for reports only)
    data: bytes
    sha256: str
    as_photo: bool     # False -> sent as a document (webp / > 10 MB)


@dataclass
class InspectedArchive:
    filename: str
    sha256: str
    archive_date: str
    part: int
    items: list[ArchiveItem] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (member, reason)
    manifest: dict | None = None


def _image_kind(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _unsafe_path(name: str) -> str | None:
    if "\x00" in name:
        return "NUL byte in file name"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return "absolute path"
    if any(part == ".." for part in normalized.split("/")):
        return "path traversal"
    return None


def inspect_zip(data: bytes, filename: str) -> InspectedArchive:
    """Validate an untrusted archive completely in memory."""
    if len(data) > MAX_ZIP_BYTES:
        raise ArchiveRejected(f"archive is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ArchiveRejected("not a valid ZIP file")
    name_match = ARCHIVE_NAME_RE.match(filename or "")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as error:
        raise ArchiveRejected(f"malformed ZIP ({error})") from None

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveRejected(f"too many entries ({len(infos)} > {MAX_MEMBERS})")
        if sum(i.file_size for i in infos) > MAX_TOTAL_UNCOMPRESSED:
            raise ArchiveRejected("uncompressed size is too large (possible ZIP bomb)")
        seen_names = set()
        for info in infos:
            problem = _unsafe_path(info.filename)
            if problem:
                raise ArchiveRejected(f"{problem}: {info.filename!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ArchiveRejected(f"symbolic link not allowed: {info.filename!r}")
            if info.flag_bits & 0x1:
                raise ArchiveRejected("encrypted archives are not supported")
            lowered = info.filename.lower()
            ext = "." + lowered.rsplit(".", 1)[-1] if "." in lowered.rsplit("/", 1)[-1] else ""
            if ext in EXECUTABLE_EXTS:
                raise ArchiveRejected(f"executable file not allowed: {info.filename!r}")
            if info.compress_size and info.file_size > 1024 * 1024 and \
                    info.file_size / max(info.compress_size, 1) > 200:
                raise ArchiveRejected(f"suspicious compression ratio: {info.filename!r}")
            if lowered in seen_names:
                raise ArchiveRejected(f"duplicate entry {info.filename!r}")
            seen_names.add(lowered)

        manifest = None
        manifest_info = next((i for i in infos if i.filename == MANIFEST_NAME), None)
        if manifest_info is not None:
            try:
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("items", []), list):
                    raise ValueError("unexpected structure")
                parse_day(str(manifest.get("archive_date", "")))
            except (ValueError, UnicodeError) as error:
                raise ArchiveRejected(f"invalid manifest.json ({error})") from None

        if manifest is not None:
            day = manifest["archive_date"]
            part = int(manifest.get("part") or 1)
            if name_match and (name_match.group(1) != day or int(name_match.group(2) or 1) != part):
                raise ArchiveRejected(
                    f"file name date/part ({filename}) does not match manifest ({day} part {part})")
        elif name_match:
            day, part = name_match.group(1), int(name_match.group(2) or 1)
            parse_day(day)
        else:
            raise ArchiveRejected(
                "cannot determine the archive date: no manifest.json and the file name is "
                "not payment_screenshots_YYYY-MM-DD.zip")

        result = InspectedArchive(filename=filename, sha256=sha256_hex(data),
                                  archive_date=day, part=part, manifest=manifest)
        listed = {}
        if manifest is not None:
            for item in manifest.get("items") or []:
                if not isinstance(item, dict) or "file" not in item:
                    raise ArchiveRejected("manifest item without a file name")
                listed[item["file"]] = item.get("sha256")
            missing = [n for n in listed if n not in {i.filename for i in infos}]
            if missing:
                raise ArchiveRejected(f"manifest lists missing file(s): {', '.join(missing[:5])}")
            if manifest.get("item_count") is not None and int(manifest["item_count"]) != len(listed):
                raise ArchiveRejected("manifest item_count does not match its item list")

        index = 0
        for info in sorted(infos, key=lambda i: i.filename):
            if info.is_dir() or info.filename == MANIFEST_NAME:
                continue
            base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if info.filename.startswith("__MACOSX/") or base.startswith("._"):
                result.rejected.append((info.filename, "macOS metadata file, not an image"))
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                result.rejected.append((info.filename, "file larger than 50 MB"))
                continue
            with archive.open(info) as handle:
                content = handle.read(MAX_MEMBER_BYTES + 1)
            if len(content) > MAX_MEMBER_BYTES:
                result.rejected.append((info.filename, "file larger than 50 MB"))
                continue
            if content[:2] == b"MZ" or content[:4] == b"\x7fELF" or content[:2] == b"#!":
                raise ArchiveRejected(f"executable content not allowed: {info.filename!r}")
            kind = _image_kind(content)
            if kind is None:
                result.rejected.append((info.filename, "not a JPEG/PNG/WEBP image"))
                continue
            digest = sha256_hex(content)
            if manifest is not None:
                if info.filename not in listed:
                    result.rejected.append((info.filename, "not listed in manifest.json"))
                    continue
                if listed[info.filename] and listed[info.filename] != digest:
                    raise ArchiveRejected(f"checksum mismatch for {info.filename!r}")
            index += 1
            as_photo = kind in PHOTO_EXTS and len(content) <= MAX_PHOTO_BYTES
            result.items.append(ArchiveItem(
                name=f"{index:04d}{kind}", original=info.filename, data=content,
                sha256=digest, as_photo=as_photo))
    if not result.items:
        raise ArchiveRejected("archive contains no valid screenshots"
                              + (f" ({len(result.rejected)} unsupported file(s))" if result.rejected else ""))
    return result


# ---------------------------------------------------------------------------
# Building, delivery and purge
# ---------------------------------------------------------------------------
async def _claim_lease(db, key: str, worker: str):
    now = utcnow()
    return await db.archives.find_one_and_update(
        {"_id": key, "$or": [{"lease_until": {"$lt": now}}, {"lease_owner": worker},
                             {"lease_until": None}]},
        {"$set": {"lease_owner": worker, "lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
        return_document=ReturnDocument.AFTER,
    )


async def _release(db, key: str, worker: str, **values):
    await db.archives.update_one(
        {"_id": key, "lease_owner": worker},
        {"$set": {"lease_owner": None, "lease_until": None, "updated_at": utcnow(), **values}})


async def build_for_date(app, day: str, *, now=None) -> list[str]:
    """Seal all pending screenshots of a finished day into archive part(s)."""
    db, account_id = app.db, app.self_id
    if not day_is_complete(day, now):
        raise ValueError(f"{day} has not ended yet in Asia/Kolkata; archives cover whole days")
    built = []
    while True:
        # Resume an interrupted build for this date first.
        archive = await db.archives.find_one(
            {"account_id": account_id, "archive_date": day, "status": "building"})
        if archive is None:
            if not await db.screenshots.find_one(
                    {"account_id": account_id, "archive_date": day, "status": "pending"}):
                break
            parts = await db.archives.count_documents({"account_id": account_id, "archive_date": day})
            part = parts + 1
            key = archive_key(account_id, day, part)
            try:
                await db.archives.insert_one({
                    "_id": key, "account_id": account_id, "archive_date": day, "part": part,
                    "filename": archive_filename(day, part), "status": "building",
                    "created_at": utcnow(), "lease_owner": None, "lease_until": None,
                    "delivery": {"attempts": 0}, "post_status": "pending",
                })
            except DuplicateKeyError:  # another worker created this part
                continue
            archive = await db.archives.find_one({"_id": key})
        key = archive["_id"]
        if await _claim_lease(db, key, app.worker_id) is None:
            break  # someone else is building it
        # Claim pending screenshots (bounded) for this archive.
        pending = await db.screenshots.find(
            {"account_id": account_id, "archive_date": day, "status": "pending"},
            {"_id": 1}).sort("captured_at", 1).limit(MAX_ITEMS_PER_ARCHIVE).to_list(None)
        already = await db.screenshots.count_documents({"archive_id": key})
        room = max(0, MAX_ITEMS_PER_ARCHIVE - already)
        if pending and room:
            await db.screenshots.update_many(
                {"_id": {"$in": [p["_id"] for p in pending[:room]]}, "status": "pending"},
                {"$set": {"status": "archiving", "archive_id": key}})
        docs = await db.screenshots.find({"archive_id": key}).to_list(None)
        if not docs:
            await db.archives.delete_one({"_id": key, "status": "building"})
            break
        items = [(doc, await db.get_blob(SCREENSHOT_BUCKET, doc["blob_id"])) for doc in docs]
        for doc, data in items:
            if sha256_hex(data) != doc["sha256"]:
                raise RuntimeError(f"stored screenshot {doc['_id']} failed its checksum")
        data, manifest = build_zip(day, archive["part"], items)
        verify_zip(data, manifest)
        blob_id, digest = await db.put_blob(
            ARCHIVE_BUCKET, data, archive["filename"],
            {"account_id": account_id, "archive_date": day}, dedupe=False)
        if not await db.verify_blob(ARCHIVE_BUCKET, blob_id, digest):
            raise RuntimeError("archive ZIP failed read-back verification in MongoDB")
        await _release(db, key, app.worker_id, status="built", built_at=utcnow(),
                       item_ids=[d["_id"] for d in docs], item_count=len(docs),
                       sha256=digest, size=len(data), blob_id=blob_id,
                       window_start=manifest["window_start"],
                       window_end_exclusive=manifest["window_end_exclusive"])
        await db.record_audit(account_id, "archive_built", archive=key, items=len(docs), sha256=digest)
        built.append(key)
    return built


def delivery_caption(archive: dict) -> str:
    return (f"{archive['filename']}\n"
            f"Date: {archive['archive_date']} (Asia/Kolkata, 00:00-24:00)\n"
            f"Screenshots: {archive['item_count']}\n"
            f"SHA-256: {archive['sha256'][:16]}")


def _matches(message, archive: dict) -> bool:
    document = getattr(getattr(message, "media", None), "document", None)
    if document is None or int(getattr(document, "size", -1)) != int(archive["size"]):
        return False
    names = [getattr(a, "file_name", None) for a in getattr(document, "attributes", []) or []]
    return archive["filename"] in names


async def _find_delivered(app, chat, archive: dict):
    try:
        async for message in app.client.iter_messages(chat, search=archive["filename"], limit=30):
            if getattr(message, "out", True) and _matches(message, archive):
                return message
    except Exception as error:  # noqa: BLE001
        ui.warn(f"archive reconciliation search failed: {type(error).__name__}: {error}")
        raise
    return None


async def deliver(app, archive: dict) -> tuple[bool, str]:
    """Send a built archive to the archive channel and verify it landed."""
    db = app.db
    key = archive["_id"]
    if await _claim_lease(db, key, app.worker_id) is None:
        return False, "another worker is delivering this archive"
    archive = await db.archives.find_one({"_id": key})
    phase = "prepare"
    try:
        target = app.cfg.get("archive_channel")
        if not target:
            raise RuntimeError("no archive channel configured (.setarchive)")
        chat_id, entity, _title = await resolve_chat(app.client, target)
        if archive["status"] == "delivering":
            # Crash/timeout after an earlier send: reconcile before re-sending.
            found = await _find_delivered(app, entity, archive)
            if found is not None:
                return await _mark_delivered(app, archive, chat_id, found)
        ok, why = await can_post_media(app.client, entity)
        if not ok:
            raise RuntimeError(f"cannot post to {target}: {why}")
        data = await db.get_blob(ARCHIVE_BUCKET, archive["blob_id"])
        if sha256_hex(data) != archive["sha256"]:
            raise RuntimeError("archive ZIP in MongoDB failed its checksum")
        attempts = int((archive.get("delivery") or {}).get("attempts") or 0) + 1
        await db.archives.update_one({"_id": key}, {"$set": {
            "status": "delivering", "delivery.attempts": attempts,
            "delivery.started_at": utcnow(), "delivery.chat_id": chat_id}})
        def payload():
            stream = io.BytesIO(data)
            stream.name = archive["filename"]
            return stream

        phase = "send"
        sent = await flood_safe(app, lambda: app.client.send_file(
            entity, file=payload(), caption=delivery_caption(archive), force_document=True))
        phase = "verify"
        # Read the delivered document back before trusting it.
        check = await app.client.get_messages(entity, ids=sent.id)
        if check is None or not _matches(check, archive):
            raise RuntimeError("delivered document could not be verified (size/name mismatch)")
        return await _mark_delivered(app, archive, chat_id, check)
    except Exception as error:  # noqa: BLE001
        current = await db.archives.find_one({"_id": key}) or {}
        attempts = int((current.get("delivery") or {}).get("attempts") or 1)
        delay = (timedelta(seconds=error.seconds + 5) if isinstance(error, FloodDeferred)
                 else retry_delay(attempts))
        # If a send may have reached Telegram, stay "delivering" so the next
        # attempt reconciles before re-sending. Sources are never touched.
        status = "delivering" if phase in ("send", "verify") or \
            current.get("status") == "delivering" else "failed"
        await _release(db, key, app.worker_id, status=status,
                       **{"delivery.error": f"{type(error).__name__}: {error}"[:500],
                          "delivery.next_retry_at": utcnow() + delay})
        return False, f"{type(error).__name__}: {error}"


async def _mark_delivered(app, archive, chat_id, message):
    await _release(app.db, archive["_id"], app.worker_id, status="delivered", **{
        "delivery.chat_id": chat_id, "delivery.message_id": message.id,
        "delivery.delivered_at": utcnow(), "delivery.verified": True,
        "delivery.error": None})
    await app.db.record_audit(app.self_id, "archive_delivered", archive=archive["_id"],
                              chat_id=chat_id, message_id=message.id, items=archive["item_count"])
    return True, f"delivered as message {message.id}"


async def purge(app, archive: dict) -> tuple[bool, str]:
    """Delete source screenshots + local ZIP after verified delivery."""
    db = app.db
    archive = await db.archives.find_one({"_id": archive["_id"]})
    delivery = archive.get("delivery") or {}
    if archive.get("status") != "delivered" or not delivery.get("verified") or not delivery.get("message_id"):
        return False, "archive delivery is not verified; nothing deleted"
    failures = 0
    removed = 0
    async for doc in db.screenshots.find({"archive_id": archive["_id"]}):
        if await db.delete_blob(SCREENSHOT_BUCKET, doc["blob_id"]):
            await db.screenshots.delete_one({"_id": doc["_id"]})
            removed += 1
        else:
            failures += 1
    if archive.get("blob_id") and not await db.delete_blob(ARCHIVE_BUCKET, archive["blob_id"]):
        failures += 1
    if failures:
        await db.archives.update_one({"_id": archive["_id"]}, {"$set": {
            "purge_error": f"{failures} object(s) could not be deleted; will retry"}})
        return False, f"{failures} object(s) could not be deleted"
    await db.archives.update_one({"_id": archive["_id"]}, {"$set": {
        "status": "purged", "purged_at": utcnow(), "blob_id": None, "purge_error": None}})
    await db.record_audit(app.self_id, "archive_purged", archive=archive["_id"],
                          deleted_screenshots=removed, delivered_message_id=delivery.get("message_id"))
    return True, f"deleted {removed} source screenshot(s)"


async def run_cycle(app, *, now=None) -> dict:
    """One scheduler pass: build finished days, deliver, purge."""
    summary = {"built": [], "delivered": [], "purged": [], "errors": []}
    if not app.cfg.get("archive_enabled", True):
        return summary
    db, account_id = app.db, app.self_id
    async with app.archive_lock:
        for day in await shots.pending_dates(db, account_id):
            if not day_is_complete(day, now):
                continue
            try:
                summary["built"].extend(await build_for_date(app, day, now=now))
            except Exception as error:  # noqa: BLE001
                summary["errors"].append(f"build {day}: {type(error).__name__}: {error}")
        # Resume half-built archives whose day has no pending items left.
        async for archive in db.archives.find({"account_id": account_id, "status": "building"}):
            try:
                summary["built"].extend(await build_for_date(app, archive["archive_date"], now=now))
            except Exception as error:  # noqa: BLE001
                summary["errors"].append(f"build {archive['_id']}: {error}")
        due = {"$or": [{"delivery.next_retry_at": None}, {"delivery.next_retry_at": {"$exists": False}},
                       {"delivery.next_retry_at": {"$lte": now or utcnow()}}]}
        cursor = db.archives.find({"account_id": account_id,
                                   "status": {"$in": ["built", "failed", "delivering"]}, **due}
                                  ).sort([("archive_date", 1), ("part", 1)])
        for archive in await cursor.to_list(None):
            ok, detail = await deliver(app, archive)
            (summary["delivered"] if ok else summary["errors"]).append(
                archive["_id"] if ok else f"deliver {archive['_id']}: {detail}")
        async for archive in db.archives.find({"account_id": account_id, "status": "delivered"}):
            ok, detail = await purge(app, archive)
            (summary["purged"] if ok else summary["errors"]).append(
                archive["_id"] if ok else f"purge {archive['_id']}: {detail}")
    return summary
