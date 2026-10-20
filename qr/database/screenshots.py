"""Active payment screenshots awaiting their daily ZIP (``screenshots``)."""
from __future__ import annotations

from datetime import datetime

from pymongo.errors import DuplicateKeyError

from qr.database.mongo import SCREENSHOT_BUCKET, sha256_hex, utcnow
from qr.services.clock import as_utc, ist_date

ACTIVE = ("pending", "archiving")


async def add(db, account_id: int, data: bytes, *, captured_at: datetime,
              source_chat_id: int, source_message_id: int, mime_type: str = "image/jpeg",
              extension: str = ".jpg", order_id: str | None = None) -> tuple[dict, bool]:
    """Store one screenshot. Returns ``(document, created)``.

    Assignment policy: the archive date is the Asia/Kolkata date of the
    proof message's Telegram timestamp (when it was *received*), not when
    ``/add`` ran - a proof sent at 23:59 and logged at 00:05 stays on the
    earlier day. If that day's ZIP has already been delivered, the late item
    goes into an extra ``_partN`` archive for the same date.
    """
    if not data:
        raise ValueError("empty screenshot")
    account_id = int(account_id)
    captured_at = as_utc(captured_at)
    digest = sha256_hex(data)

    existing = await db.screenshots.find_one({
        "account_id": account_id, "source_chat_id": int(source_chat_id),
        "source_message_id": int(source_message_id)})
    if existing:
        return existing, False
    same_bytes = await db.screenshots.find_one(
        {"account_id": account_id, "sha256": digest, "status": {"$in": list(ACTIVE)}})
    if same_bytes:
        return same_bytes, False

    blob_id, _ = await db.put_blob(
        SCREENSHOT_BUCKET, data, f"screenshot{extension}",
        {"account_id": account_id, "kind": "payment_screenshot"}, dedupe=False)
    document = {
        "account_id": account_id,
        "archive_date": ist_date(captured_at),
        "captured_at": captured_at,
        "stored_at": utcnow(),
        "source_chat_id": int(source_chat_id),
        "source_message_id": int(source_message_id),
        "sha256": digest,
        "size": len(data),
        "mime_type": mime_type,
        "extension": extension,
        "blob_id": blob_id,
        "order_id": order_id,
        "status": "pending",
        "archive_id": None,
    }
    try:
        result = await db.screenshots.insert_one(document)
    except DuplicateKeyError:
        await db.delete_blob(SCREENSHOT_BUCKET, blob_id)
        existing = await db.screenshots.find_one({
            "account_id": account_id, "source_chat_id": int(source_chat_id),
            "source_message_id": int(source_message_id)})
        return existing, False
    document["_id"] = result.inserted_id
    return document, True


async def pending_dates(db, account_id: int) -> list[str]:
    dates = await db.screenshots.distinct(
        "archive_date", {"account_id": int(account_id), "status": "pending"})
    return sorted(dates)


async def counts(db, account_id: int) -> dict:
    pipeline = [
        {"$match": {"account_id": int(account_id)}},
        {"$group": {"_id": {"date": "$archive_date", "status": "$status"}, "n": {"$sum": 1}}},
    ]
    out: dict = {}
    async for row in await db.screenshots.aggregate(pipeline):
        out.setdefault(row["_id"]["date"], {})[row["_id"]["status"]] = row["n"]
    return out
