"""Publish screenshot ZIPs to the posting channel (.post / .postbatch / .postnow).

Each archive becomes a ``post_jobs`` document keyed by the ZIP's SHA-256, so
the same archive can never be queued twice. Screenshots are sent in albums of
at most 10 photos ("chunks"). A chunk is marked ``sending`` *before* the
upload; after a crash the chunk is reconciled against the channel (matching
caption among this account's recent posts) before anything is re-sent.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta

from telethon.errors import RPCError

from qr import ui
from qr.database import jobs as repo
from qr.database.mongo import ARCHIVE_BUCKET, utcnow
from qr.services.archive_service import ArchiveRejected, InspectedArchive, MAX_ZIP_BYTES, inspect_zip
from qr.services.clock import as_utc, parse_day
from qr.services.telegram_utils import FloodDeferred, can_post_media, flood_safe, resolve_chat

ALBUM_SIZE = 10
PACE_SECONDS = 3.0


@dataclass
class JobResult:
    key: str
    archive_date: str
    filename: str
    status: str
    posted: int = 0
    failed: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        extra = []
        if self.failed:
            extra.append(f"{self.failed} failed")
        if self.rejected:
            extra.append(f"{self.rejected} rejected")
        tail = (" (" + ", ".join(extra) + ")") if extra else ""
        error = f" - {self.errors[0]}" if self.errors else ""
        return f"{self.archive_date} {self.filename}: {self.status.upper()} {self.posted} posted{tail}{error}"


def pretty_date(day: str) -> str:
    return parse_day(day).strftime("%d %b %Y")


def render_caption(template: str, day: str, part: int, parts: int, count: int) -> str:
    text = (template or "{date}")
    for token, value in (("{date}", day), ("{pretty_date}", pretty_date(day)),
                         ("{part}", str(part)), ("{parts}", str(parts)), ("{count}", str(count))):
        text = text.replace(token, value)
    if day not in text and pretty_date(day) not in text:
        text = f"{day}\n{text}"  # the archive date is mandatory in every caption
    return text[:1024]


def plan_chunks(items: list[dict], album_size: int = ALBUM_SIZE) -> list[dict]:
    """Photos in albums of <= album_size; documents individually; order kept."""
    chunks, current = [], []
    for item in items:
        if item["as_photo"]:
            current.append(item["name"])
            if len(current) == album_size:
                chunks.append({"kind": "album", "items": current})
                current = []
        else:
            if current:
                chunks.append({"kind": "album", "items": current})
                current = []
            chunks.append({"kind": "document", "items": [item["name"]]})
    if current:
        chunks.append({"kind": "album", "items": current})
    for index, chunk in enumerate(chunks, 1):
        chunk.update({"index": index, "status": "pending", "message_ids": [], "attempts": 0,
                      "error": None, "started_at": None})
    return chunks


def is_zip_message(message) -> bool:
    document = getattr(getattr(message, "media", None), "document", None)
    if document is None:
        return False
    names = [getattr(a, "file_name", "") or "" for a in getattr(document, "attributes", []) or []]
    return (getattr(document, "mime_type", "") in ("application/zip", "application/x-zip-compressed")
            or any(n.lower().endswith(".zip") for n in names))


def zip_name(message) -> str:
    document = message.media.document
    for attribute in document.attributes or []:
        if getattr(attribute, "file_name", None):
            return attribute.file_name
    return "archive.zip"


# ---------------------------------------------------------------------------
# Loading archives
# ---------------------------------------------------------------------------
async def download_zip(app, message) -> bytes:
    size = int(getattr(message.media.document, "size", 0) or 0)
    if size > MAX_ZIP_BYTES:
        raise ArchiveRejected(f"archive is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB")
    data = await app.client.download_media(message, file=bytes)
    if not data:
        raise ArchiveRejected("could not download the archive")
    return data


async def load_source(app, job: dict) -> bytes:
    source = job.get("source") or {}
    if source.get("archive_id"):
        archive = await app.db.archives.find_one({"_id": source["archive_id"]})
        if archive and archive.get("blob_id"):
            return await app.db.get_blob(ARCHIVE_BUCKET, archive["blob_id"])
        delivery = (archive or {}).get("delivery") or {}
        source = {"chat_id": delivery.get("chat_id"), "message_id": delivery.get("message_id")}
    if not source.get("chat_id") or not source.get("message_id"):
        raise RuntimeError("the archive's source message is unknown")
    message = await app.client.get_messages(source["chat_id"], ids=source["message_id"])
    if message is None or not is_zip_message(message):
        raise RuntimeError("the archive's source message is no longer available")
    return await download_zip(app, message)


async def create_job(app, inspected: InspectedArchive, source: dict) -> tuple[dict, bool]:
    items = [{"name": i.name, "original": i.original, "sha256": i.sha256,
              "as_photo": i.as_photo, "status": "pending", "error": None, "message_id": None}
             for i in inspected.items]
    rejected = [{"original": name, "reason": reason} for name, reason in inspected.rejected]
    key = repo.job_key(app.self_id, inspected.sha256)
    document = {
        "_id": key, "account_id": app.self_id, "archive_date": inspected.archive_date,
        "part": inspected.part, "filename": inspected.filename, "zip_sha256": inspected.sha256,
        "source": source, "status": "pending", "items": items, "rejected": rejected,
        "chunks": plan_chunks(items), "attempts": 0, "last_error": None, "target_chat_id": None,
    }
    job, created = await repo.create(app.db, document)
    if created:
        await app.db.record_audit(app.self_id, "post_job_created", job=key,
                                  archive_date=inspected.archive_date, items=len(items))
    return job, created


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------
async def destination(app):
    """Resolve + permission-check the posting channel. Raises ValueError."""
    target = app.cfg.get("publish_channel")
    if not target:
        raise ValueError("No posting channel configured. Run .setpostchannel inside the "
                         "channel, or .setpostchannel <@username|id>.")
    try:
        chat_id, entity, title = await resolve_chat(app.client, target)
    except Exception as error:  # noqa: BLE001
        raise ValueError(f"Posting channel {target} is not accessible "
                         f"({type(error).__name__}: {error})") from None
    ok, why = await can_post_media(app.client, entity)
    if not ok:
        raise ValueError(f"Cannot post in {title}: {why}")
    return chat_id, entity, title


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------
def _private_tempdir() -> str:
    path = tempfile.mkdtemp(prefix="quickreply-post-")
    os.chmod(path, stat.S_IRWXU)
    return path


async def _reconcile_chunk(app, entity, chunk: dict, caption: str) -> list[int] | None:
    """Find a chunk that Telegram accepted before we recorded it."""
    started = chunk.get("started_at")
    since = as_utc(started) - timedelta(seconds=10) if started else None
    found, grouped = [], None
    async for message in app.client.iter_messages(entity, limit=80):
        if since and as_utc(message.date) < since:
            break
        if not getattr(message, "out", False):
            continue
        if (message.message or "") == caption:
            grouped = getattr(message, "grouped_id", None)
            found.append(message.id)
            break
    if not found:
        return None
    if grouped:
        found = []
        async for message in app.client.iter_messages(entity, limit=80):
            if getattr(message, "grouped_id", None) == grouped:
                found.append(message.id)
    return sorted(found)


async def _send_chunk(app, entity, chunk: dict, paths: dict, caption: str) -> list:
    files = [paths[name] for name in chunk["items"]]
    if chunk["kind"] == "document":
        sent = await flood_safe(app, lambda: app.client.send_file(
            entity, file=files[0], caption=caption, force_document=True))
        return [sent]
    captions = [caption] + [""] * (len(files) - 1)
    sent = await flood_safe(app, lambda: app.client.send_file(
        entity, file=files if len(files) > 1 else files[0],
        caption=captions if len(files) > 1 else caption))
    return sent if isinstance(sent, list) else [sent]


async def _send_individually(app, key, entity, chunk, chunks, items, paths, caption) -> None:
    """Fallback after an album was rejected: photo first, then as a file."""
    for name in chunk["items"]:
        if items[name]["status"] in ("posted", "unverified"):
            continue
        modes = [False, True] if items[name]["as_photo"] else [True]
        last_error = None
        for force_document in modes:
            try:
                sent = await flood_safe(app, lambda n=name, fd=force_document: app.client.send_file(
                    entity, file=paths[n], caption=caption, force_document=fd))
                items[name].update({"status": "posted", "message_id": sent.id, "error": None})
                last_error = None
                break
            except FloodDeferred:
                await repo.save(app.db, key, app.worker_id, items=list(items.values()))
                raise
            except RPCError as error:
                last_error = error
            except Exception as error:  # noqa: BLE001
                # Unknown outcome: never re-send this item automatically.
                items[name].update({"status": "unverified",
                                    "error": f"may have been posted ({type(error).__name__}); check the channel"})
                await repo.save(app.db, key, app.worker_id, items=list(items.values()))
                raise
        if last_error is not None:
            items[name].update({"status": "failed",
                                "error": f"{type(last_error).__name__}: {last_error}"[:300]})
        await repo.save(app.db, key, app.worker_id, items=list(items.values()))
        await app.sleep(1)
    if all(items[n]["status"] == "posted" for n in chunk["items"]):
        chunk.update({"status": "sent", "error": None,
                      "message_ids": [items[n]["message_id"] for n in chunk["items"]]})
    await repo.save(app.db, key, app.worker_id, chunks=chunks)


async def run_job(app, key: str) -> JobResult:
    db = app.db
    job = await repo.claim(db, key, app.worker_id)
    if job is None:
        current = await db.post_jobs.find_one({"_id": key}) or {}
        return JobResult(key, current.get("archive_date", "?"), current.get("filename", "?"),
                         "busy", errors=["another worker is processing this archive"])
    result = JobResult(key, job["archive_date"], job["filename"], "running",
                       rejected=len(job.get("rejected") or []))
    workdir = None
    try:
        chat_id, entity, _title = await destination(app)
        if job.get("target_chat_id") not in (None, chat_id) and any(
                c["status"] in ("sent", "sending") for c in job["chunks"]):
            raise RuntimeError("this archive was partly posted to a different channel; "
                               "switch .setpostchannel back to finish it")
        data = await load_source(app, job)
        inspected = inspect_zip(data, job["filename"])
        if inspected.sha256 != job["zip_sha256"]:
            raise RuntimeError("the archive changed since it was queued")
        workdir = _private_tempdir()
        paths = {}
        for item in inspected.items:
            path = os.path.join(workdir, item.name)
            with open(path, "wb") as handle:
                handle.write(item.data)
            paths[item.name] = path
        await repo.save(db, key, app.worker_id, target_chat_id=chat_id,
                        attempts=int(job.get("attempts") or 0) + 1)

        items = {i["name"]: i for i in job["items"]}
        chunks = job["chunks"]
        parts = len(chunks)
        template = app.cfg.get("post_caption") or ""
        sent_any = False
        for chunk in chunks:
            if chunk["status"] == "sent":
                continue
            caption = render_caption(template, job["archive_date"], chunk["index"], parts,
                                     len(chunk["items"]))
            if chunk["status"] == "sending":
                ids = await _reconcile_chunk(app, entity, chunk, caption)
                if ids and len(ids) != len(chunk["items"]):
                    raise RuntimeError(
                        f"part {chunk['index']} is only partly visible in the channel; check it "
                        "manually before retrying")
                if ids:
                    chunk.update({"status": "sent", "message_ids": ids, "error": None})
                    for name, mid in zip(chunk["items"], ids, strict=True):
                        items[name].update({"status": "posted", "message_id": mid, "error": None})
                    await repo.save(db, key, app.worker_id, chunks=chunks, items=list(items.values()))
                    continue
            if sent_any:
                await app.sleep(PACE_SECONDS)
            if chunk["status"] == "failed":
                # The album was rejected earlier: retry only unposted items.
                await _send_individually(app, key, entity, chunk, chunks, items, paths, caption)
                sent_any = True
                continue
            chunk.update({"status": "sending", "started_at": utcnow(),
                          "attempts": int(chunk.get("attempts") or 0) + 1})
            await repo.save(db, key, app.worker_id, chunks=chunks)
            try:
                messages = await _send_chunk(app, entity, chunk, paths, caption)
            except FloodDeferred:
                chunk.update({"status": "pending", "error": "flood wait"})
                await repo.save(db, key, app.worker_id, chunks=chunks)
                raise
            except RPCError as error:
                # Telegram definitively rejected the request; nothing was posted.
                chunk.update({"status": "failed", "error": f"{type(error).__name__}: {error}"[:300]})
                await repo.save(db, key, app.worker_id, chunks=chunks)
                await _send_individually(app, key, entity, chunk, chunks, items, paths, caption)
                sent_any = True
                continue
            except Exception as error:  # noqa: BLE001
                # Network/timeout: Telegram may or may not have accepted the
                # album. Keep it "sending" so the next run reconciles first.
                chunk.update({"error": f"outcome unknown ({type(error).__name__}); "
                                       "will be reconciled before any retry"})
                await repo.save(db, key, app.worker_id, chunks=chunks)
                raise
            ids = [m.id for m in messages]
            if len(ids) != len(chunk["items"]):
                chunk.update({"status": "sending", "error": "Telegram returned an unexpected number of messages"})
                await repo.save(db, key, app.worker_id, chunks=chunks)
                raise RuntimeError(chunk["error"])
            chunk.update({"status": "sent", "message_ids": ids, "error": None})
            for name, mid in zip(chunk["items"], ids, strict=True):
                items[name].update({"status": "posted", "message_id": mid, "error": None})
            await repo.save(db, key, app.worker_id, chunks=chunks, items=list(items.values()))
            await repo.renew(db, key, app.worker_id)
            sent_any = True

        result.posted = sum(1 for i in items.values() if i["status"] == "posted")
        result.failed = sum(1 for i in items.values() if i["status"] != "posted")
        result.status = "posted" if result.failed == 0 else "partial"
        if result.failed:
            result.errors.append(next(i["error"] for i in items.values() if i["status"] != "posted") or "failed")
        await repo.release(db, key, app.worker_id, status=result.status, finished_at=utcnow(),
                           last_error=result.errors[0] if result.errors else None)
    except FloodDeferred as error:
        result.status = "pending"
        result.errors.append(f"Telegram rate limit: retry in {error.seconds}s (.postnow resumes)")
        await repo.release(db, key, app.worker_id, status="pending", last_error=result.errors[-1],
                           next_retry_at=utcnow() + timedelta(seconds=error.seconds))
    except Exception as error:  # noqa: BLE001
        result.status = "failed"
        result.errors.append(f"{type(error).__name__}: {error}"[:300])
        await repo.release(db, key, app.worker_id, status="failed", last_error=result.errors[-1])
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    final = await db.post_jobs.find_one({"_id": key}) or {}
    result.posted = sum(1 for i in final.get("items") or [] if i["status"] == "posted")
    result.failed = sum(1 for i in final.get("items") or [] if i["status"] != "posted")
    archive_id = (final.get("source") or {}).get("archive_id")
    if archive_id:
        await db.archives.update_one({"_id": archive_id}, {"$set": {
            "post_status": "posted" if result.status == "posted" else "pending",
            "post_job": key}})
    ui.info(f"[post] {result.line()}")
    return result


async def queue_delivered_archives(app, day: str | None = None) -> int:
    """Create jobs for delivered daily archives not yet published."""
    query = {"account_id": app.self_id, "status": {"$in": ["delivered", "purged"]},
             "post_status": {"$ne": "posted"}, "post_job": {"$exists": False}}
    if day:
        query["archive_date"] = day
    created = 0
    async for archive in app.db.archives.find(query).sort([("archive_date", 1), ("part", 1)]):
        try:
            data = await load_source(app, {"source": {"archive_id": archive["_id"]}})
            inspected = inspect_zip(data, archive["filename"])
            job, new = await create_job(app, inspected, {"archive_id": archive["_id"]})
            await app.db.archives.update_one({"_id": archive["_id"]}, {"$set": {"post_job": job["_id"]}})
            created += int(new)
        except Exception as error:  # noqa: BLE001
            ui.warn(f"archive {archive['_id']} could not be queued: {error}")
    return created


async def process(app, keys: list[str]) -> list[JobResult]:
    """Run jobs oldest date first; continue after recoverable errors."""
    docs = await app.db.post_jobs.find({"_id": {"$in": keys}}).to_list(None)
    docs.sort(key=lambda d: (d["archive_date"], d.get("part", 1), d["filename"]))
    results = []
    async with app.post_lock:
        for doc in docs:
            if doc["status"] == "posted":
                results.append(JobResult(doc["_id"], doc["archive_date"], doc["filename"], "posted",
                                         posted=len(doc["items"]), rejected=len(doc.get("rejected") or []),
                                         errors=["already posted earlier - not re-posted"]))
                continue
            results.append(await run_job(app, doc["_id"]))
    return results


def summary(results: list[JobResult], failures: list[str] | None = None) -> str:
    failures = failures or []
    ok = [r for r in results if r.status == "posted"]
    bad = [r for r in results if r.status != "posted"]
    total = len(results) + len(failures)
    header = ("All archives posted." if not bad and not failures else
              f"{len(ok)} of {total} archive(s) fully posted; {len(bad) + len(failures)} need attention.")
    lines = [header] + [r.line() for r in results] + [f"REJECTED {f}" for f in failures]
    return "\n".join(lines)
