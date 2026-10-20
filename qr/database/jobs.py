"""Posting jobs (``post_jobs``) with lease-based mutual exclusion."""
from __future__ import annotations

from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from qr.database.mongo import utcnow

LEASE_SECONDS = 300
RETRYABLE = ("pending", "running", "failed", "partial")


def job_key(account_id: int, zip_sha256: str) -> str:
    return f"{int(account_id)}:{zip_sha256}"


async def create(db, document: dict) -> tuple[dict, bool]:
    """Insert a job; an identical archive (same SHA-256) reuses its job."""
    document.setdefault("created_at", utcnow())
    document.setdefault("updated_at", utcnow())
    document.setdefault("lease_owner", None)
    document.setdefault("lease_until", None)
    try:
        await db.post_jobs.insert_one(document)
        return document, True
    except DuplicateKeyError:
        return await db.post_jobs.find_one({"_id": document["_id"]}), False


async def claim(db, key: str, worker: str):
    now = utcnow()
    return await db.post_jobs.find_one_and_update(
        {"_id": key, "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}},
                             {"lease_owner": worker}]},
        {"$set": {"lease_owner": worker, "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                  "status": "running", "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )


async def renew(db, key: str, worker: str) -> bool:
    result = await db.post_jobs.update_one(
        {"_id": key, "lease_owner": worker},
        {"$set": {"lease_until": utcnow() + timedelta(seconds=LEASE_SECONDS)}})
    return result.matched_count == 1


async def save(db, key: str, worker: str, **values) -> None:
    await db.post_jobs.update_one(
        {"_id": key, "lease_owner": worker}, {"$set": {"updated_at": utcnow(), **values}})


async def release(db, key: str, worker: str, **values) -> None:
    await db.post_jobs.update_one(
        {"_id": key, "lease_owner": worker},
        {"$set": {"lease_owner": None, "lease_until": None, "updated_at": utcnow(), **values}})


async def eligible(db, account_id: int, day: str | None = None) -> list[dict]:
    now = utcnow()
    query = {
        "account_id": int(account_id),
        "status": {"$in": list(RETRYABLE)},
        "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}],
    }
    if day:
        query["archive_date"] = day
    cursor = db.post_jobs.find(query).sort([("archive_date", 1), ("part", 1), ("filename", 1)])
    return await cursor.to_list(None)


async def posted_for(db, account_id: int, day: str, part: int, exclude_key: str):
    return await db.post_jobs.find_one({
        "account_id": int(account_id), "archive_date": day, "part": int(part),
        "status": "posted", "_id": {"$ne": exclude_key}})
