"""Backup documents (``backups`` collection)."""
from __future__ import annotations

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING

from qr.database.mongo import utcnow


async def latest(db, account_id: int, *, kinds=("manual",), statuses=("complete", "partial")):
    return await db.backups.find_one(
        {"account_id": int(account_id), "kind": {"$in": list(kinds)},
         "status": {"$in": list(statuses)}},
        sort=[("created_at", DESCENDING)],
    )


async def by_id(db, backup_id: str):
    try:
        oid = ObjectId(str(backup_id))
    except (InvalidId, TypeError):
        return None
    return await db.backups.find_one({"_id": oid})


async def find_duplicate(db, account_id: int, fingerprint: str):
    return await db.backups.find_one(
        {"account_id": int(account_id), "fingerprint": fingerprint,
         "status": "complete", "kind": "manual"},
        sort=[("created_at", DESCENDING)],
    )


async def insert(db, document: dict) -> ObjectId:
    result = await db.backups.insert_one(document)
    return result.inserted_id


async def set_status(db, backup_id, status: str, **extra) -> None:
    await db.backups.update_one(
        {"_id": backup_id}, {"$set": {"status": status, "verified_at": utcnow(), **extra}})


async def touch(db, backup_id) -> None:
    await db.backups.update_one({"_id": backup_id}, {"$set": {"last_confirmed_at": utcnow()}})


async def recent(db, account_id: int | None = None, limit: int = 10) -> list:
    query = {"status": {"$in": ["complete", "partial"]}}
    if account_id is not None:
        query["account_id"] = int(account_id)
    cursor = db.backups.find(
        query, {"account_id": 1, "kind": 1, "status": 1, "created_at": 1, "summary": 1,
                "profile.first_name": 1, "profile.username": 1},
    ).sort("created_at", DESCENDING).limit(limit)
    return await cursor.to_list(length=limit)
