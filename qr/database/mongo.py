"""MongoDB access layer (PyMongo's native asyncio API + GridFS).

One ``MONGO_URL`` drives everything. Collections and indexes are created
automatically by :meth:`Database.init`; nothing about them is configurable.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import ConfigurationError, OperationFailure, PyMongoError

from qr.config.validation import database_name, redact_mongo_url

try:  # PyMongo >= 4.10
    from gridfs import AsyncGridFSBucket
except ImportError:  # pragma: no cover - guarded by requirements.txt
    from gridfs.asynchronous import AsyncGridFSBucket  # type: ignore

SCHEMA_VERSION = 1
DM_RETENTION_DAYS = 180   # .graph history kept this long (TTL index)

# GridFS buckets. Screenshots and archive ZIPs live in their own buckets so a
# post-delivery purge can never touch backup media.
MEDIA_BUCKET = "media"
SCREENSHOT_BUCKET = "screenshots_fs"
ARCHIVE_BUCKET = "archives_fs"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DatabaseError(RuntimeError):
    """Readable database failure that never embeds the connection string."""


class Database:
    def __init__(self, url: str, *, db_name: str | None = None,
                 server_timeout_ms: int = 8000, client=None):
        self._url = url
        self.redacted_url = redact_mongo_url(url)
        self.db_name = db_name or database_name(url)
        self.client = client or AsyncMongoClient(
            url,
            serverSelectionTimeoutMS=server_timeout_ms,
            tz_aware=True,
            appname="QuickReply",
        )
        self.db = self.client[self.db_name]
        self.media = AsyncGridFSBucket(self.db, bucket_name=MEDIA_BUCKET)
        self.screenshot_fs = AsyncGridFSBucket(self.db, bucket_name=SCREENSHOT_BUCKET)
        self.archive_fs = AsyncGridFSBucket(self.db, bucket_name=ARCHIVE_BUCKET)

    # ---- collections ---------------------------------------------------
    def __getattr__(self, name):
        if name in COLLECTIONS:
            return self.db[name]
        raise AttributeError(name)

    def bucket(self, name: str):
        return {
            MEDIA_BUCKET: self.media,
            SCREENSHOT_BUCKET: self.screenshot_fs,
            ARCHIVE_BUCKET: self.archive_fs,
        }[name]

    # ---- lifecycle -----------------------------------------------------
    async def ping(self) -> None:
        try:
            await self.client.admin.command("ping")
            # Touch the target database so missing permissions surface now.
            await self.db.list_collection_names()
        except OperationFailure as error:
            if error.code in (18, 8000) or "auth" in str(error).lower():
                hint = "MongoDB rejected the username/password in MONGO_URL"
            elif error.code == 13:
                hint = f"the MongoDB user may not access database '{self.db_name}'"
            else:
                hint = f"MongoDB refused the request (code {error.code})"
            raise DatabaseError(f"{hint} ({self.redacted_url}).") from None
        except ConfigurationError:
            raise DatabaseError(
                f"MONGO_URL could not be resolved ({self.redacted_url}); check the "
                "cluster host name and your DNS/network.") from None
        except PyMongoError as error:
            raise DatabaseError(
                f"MongoDB is unreachable at {self.redacted_url} "
                f"({type(error).__name__}). Check MONGO_URL, network access "
                "and the Atlas IP access list."
            ) from None

    async def init(self) -> None:
        """Create indexes (idempotent). Collections are created on demand."""
        try:
            await self.greeted.create_index(
                [("account_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            await self.payments.create_index(
                [("account_id", ASCENDING), ("order_id", ASCENDING)], unique=True)
            await self.payments.create_index(
                [("account_id", ASCENDING), ("command_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"command_key": {"$type": "string"}},
            )
            await self.payments.create_index(
                [("account_id", ASCENDING), ("status", ASCENDING), ("ts", ASCENDING)])
            await self.payments.create_index(
                [("account_id", ASCENDING), ("post_chat_id", ASCENDING),
                 ("post_message_id", ASCENDING)])
            await self.command_claims.create_index(
                "created_at", expireAfterSeconds=86400)
            await self.backups.create_index(
                [("account_id", ASCENDING), ("created_at", DESCENDING)])
            await self.backups.create_index(
                [("account_id", ASCENDING), ("fingerprint", ASCENDING)])
            await self.screenshots.create_index(
                [("account_id", ASCENDING), ("source_chat_id", ASCENDING),
                 ("source_message_id", ASCENDING)], unique=True)
            await self.screenshots.create_index(
                [("account_id", ASCENDING), ("archive_date", ASCENDING),
                 ("status", ASCENDING)])
            await self.screenshots.create_index(
                [("account_id", ASCENDING), ("sha256", ASCENDING)])
            await self.archives.create_index(
                [("account_id", ASCENDING), ("status", ASCENDING),
                 ("archive_date", ASCENDING)])
            await self.post_jobs.create_index(
                [("account_id", ASCENDING), ("status", ASCENDING),
                 ("archive_date", ASCENDING)])
            await self.restore_sessions.create_index(
                "expires_at", expireAfterSeconds=0)
            await self.restore_history.create_index(
                [("account_id", ASCENDING), ("created_at", DESCENDING)])
            await self.audit.create_index(
                [("account_id", ASCENDING), ("ts", DESCENDING)])
            await self.dm_events.create_index(
                [("account_id", ASCENDING), ("date", ASCENDING)])
            await self.dm_events.create_index(
                "at", expireAfterSeconds=DM_RETENTION_DAYS * 86400)
            for bucket in (MEDIA_BUCKET, SCREENSHOT_BUCKET, ARCHIVE_BUCKET):
                await self.db[f"{bucket}.files"].create_index("metadata.sha256")
        except PyMongoError as error:
            raise DatabaseError(
                f"Could not initialise MongoDB indexes ({type(error).__name__}: "
                f"{error}). The MongoDB user needs readWrite on '{self.db_name}'."
            ) from None

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass

    async def healthcheck_roundtrip(self) -> None:
        """Harmless write/read/delete used by the installer."""
        collection = self.db["_qr_healthcheck"]
        marker = {"_id": f"probe-{utcnow().timestamp()}", "ts": utcnow()}
        await collection.insert_one(marker)
        found = await collection.find_one({"_id": marker["_id"]})
        await collection.delete_one({"_id": marker["_id"]})
        if not found:
            raise DatabaseError("MongoDB accepted a write but the read-back failed.")
        if await collection.estimated_document_count() == 0:
            await collection.drop()

    # ---- GridFS --------------------------------------------------------
    async def put_blob(self, bucket_name: str, data: bytes, filename: str,
                       metadata: dict | None = None, dedupe: bool = True):
        """Store bytes; returns ``(file_id, sha256)``. Dedupes by content."""
        digest = sha256_hex(data)
        files = self.db[f"{bucket_name}.files"]
        if dedupe:
            existing = await files.find_one(
                {"metadata.sha256": digest, "length": len(data)}, {"_id": 1})
            if existing:
                return existing["_id"], digest
        meta = dict(metadata or {})
        meta["sha256"] = digest
        file_id = await self.bucket(bucket_name).upload_from_stream(
            filename, data, metadata=meta)
        return file_id, digest

    async def get_blob(self, bucket_name: str, file_id) -> bytes:
        stream = await self.bucket(bucket_name).open_download_stream(file_id)
        return await stream.read()

    async def verify_blob(self, bucket_name: str, file_id, sha256: str | None = None) -> bool:
        try:
            data = await self.get_blob(bucket_name, file_id)
        except Exception:  # noqa: BLE001
            return False
        return sha256 is None or sha256_hex(data) == sha256

    async def delete_blob(self, bucket_name: str, file_id) -> bool:
        try:
            await self.bucket(bucket_name).delete(file_id)
            return True
        except Exception:  # noqa: BLE001 - already gone counts as deleted
            exists = await self.db[f"{bucket_name}.files"].find_one({"_id": file_id})
            return exists is None

    # ---- audit ---------------------------------------------------------
    async def record_audit(self, account_id: int, action: str, **details) -> None:
        """Minimal audit trail. Callers must never pass secrets or images."""
        try:
            await self.audit.insert_one({
                "account_id": int(account_id or 0),
                "action": action,
                "ts": utcnow(),
                "details": details,
            })
        except PyMongoError:
            pass


COLLECTIONS = {
    "accounts",          # accounts that ran QuickReply (authorised .setup sources)
    "settings",          # per-account runtime settings (templates, channels, ...)
    "greeted",           # users already sent the away/greeting reply
    "payments",          # /add payment log
    "command_claims",    # once-only command execution fence (TTL 24h)
    "backups",           # .save account backups
    "screenshots",       # active payment screenshots awaiting their daily ZIP
    "archives",          # daily ZIP manifests + delivery state
    "post_jobs",         # .post/.postbatch/.postnow jobs
    "restore_sessions",  # pending .setup confirmations (TTL)
    "restore_history",   # .setup reports
    "audit",             # minimal audit records
    "dm_events",         # one row per incoming DM (no text) for .graph, TTL
}
