"""Archive + posting commands (.post .postbatch .postnow .archive ...)."""
from __future__ import annotations

from qr.database import screenshots as shots
from qr.handlers.save import respond_long
from qr.services import archive_service, posting_service as PS
from qr.services.archive_service import ArchiveRejected, inspect_zip
from qr.services.clock import parse_day, today_ist
from qr.services.telegram_utils import can_post_media, resolve_chat

MAX_BATCH = 100


async def _zip_job_from_message(app, message):
    data = await PS.download_zip(app, message)
    inspected = inspect_zip(data, PS.zip_name(message))
    job, created = await PS.create_job(
        app, inspected, {"chat_id": int(message.chat_id), "message_id": int(message.id)})
    return job, created, inspected


async def cmd_post(app, event) -> None:
    force = "force" in (event.raw_text or "").lower().split()[1:]
    reply = await event.get_reply_message()
    if reply is None or not PS.is_zip_message(reply):
        await app.ack(event, "Reply to a screenshot ZIP (payment_screenshots_YYYY-MM-DD.zip) with .post")
        return
    try:
        await PS.destination(app)
    except ValueError as error:
        await app.ack(event, str(error))
        return
    await app.ack(event, "Validating archive...")
    try:
        job, _created, inspected = await _zip_job_from_message(app, reply)
    except ArchiveRejected as error:
        await app.ack(event, f"Archive rejected, nothing was posted: {error}")
        return
    if not force:
        other = await PS.repo.posted_for(app.db, app.self_id, job["archive_date"], job["part"], job["_id"])
        if other:
            await app.ack(event, f"{job['archive_date']} part {job['part']} was already posted from a "
                                 "different ZIP. Use .post force to publish this one as well.")
            return
    rejected = "".join(f"\nNot posted: {name} ({reason})" for name, reason in inspected.rejected[:10])
    await app.ack(event, f"Posting {len(inspected.items)} screenshot(s) for {inspected.archive_date}..."
                         + rejected)
    results = await PS.process(app, [job["_id"]])
    await respond_long(app, event, PS.summary(results) + rejected)


async def _batch_messages(app, event):
    parts = (event.raw_text or "").split()
    reply = await event.get_reply_message()
    found = []
    if reply is not None:
        async for message in app.client.iter_messages(event.chat_id, min_id=reply.id - 1,
                                                      max_id=event.id, reverse=True):
            if PS.is_zip_message(message):
                found.append(message)
            if len(found) >= MAX_BATCH:
                break
        return found
    if len(parts) > 1 and parts[1].isdigit():
        wanted = min(int(parts[1]), MAX_BATCH)
        async for message in app.client.iter_messages(event.chat_id, max_id=event.id, limit=500):
            if PS.is_zip_message(message):
                found.append(message)
                if len(found) >= wanted:
                    break
        return found
    return None


async def cmd_postbatch(app, event) -> None:
    messages = await _batch_messages(app, event)
    if messages is None:
        await app.ack(event, "Reply to the FIRST ZIP of a series with .postbatch (every ZIP from it "
                             "to this command is processed), or use .postbatch <N> for the last N ZIPs.")
        return
    if not messages:
        await app.ack(event, "No ZIP archives found in that range.")
        return
    try:
        await PS.destination(app)
    except ValueError as error:
        await app.ack(event, str(error))
        return
    await app.ack(event, f"Validating {len(messages)} archive(s)...")
    keys, failures = [], []
    for message in messages:
        name = PS.zip_name(message)
        try:
            job, _created, _inspected = await _zip_job_from_message(app, message)
            if job["_id"] not in keys:
                keys.append(job["_id"])
        except ArchiveRejected as error:
            failures.append(f"{name}: {error}")
        except Exception as error:  # noqa: BLE001
            failures.append(f"{name}: {type(error).__name__}: {error}")
    await app.ack(event, f"Posting {len(keys)} archive(s) oldest first"
                         + (f"; {len(failures)} rejected" if failures else "") + "...")
    results = await PS.process(app, keys) if keys else []
    await respond_long(app, event, PS.summary(results, failures))


async def cmd_postnow(app, event) -> None:
    parts = (event.raw_text or "").split()
    day = None
    if len(parts) > 1:
        try:
            day = parse_day(parts[1]).isoformat()
        except ValueError as error:
            await app.ack(event, f"{error}. Usage: .postnow [YYYY-MM-DD]")
            return
    try:
        _chat_id, _entity, title = await PS.destination(app)
    except ValueError as error:
        await app.ack(event, str(error))
        return
    await app.ack(event, f"Posting channel verified: {title}. Looking for pending archives...")
    await PS.queue_delivered_archives(app, day)
    jobs = await PS.repo.eligible(app.db, app.self_id, day)
    if not jobs:
        await app.ack(event, "Nothing to post: there are no undelivered archive jobs"
                             + (f" for {day}." if day else ".")
                             + " Daily ZIPs appear here after they are delivered to the archive channel.")
        return
    results = await PS.process(app, [job["_id"] for job in jobs])
    await respond_long(app, event, PS.summary(results))


async def _set_channel(app, event, key: str, label: str, need_docs: bool) -> None:
    parts = (event.raw_text or "").split(maxsplit=1)
    argument = parts[1].strip() if len(parts) > 1 else ""
    try:
        if argument.casefold() in ("", "here", "this"):
            if not getattr(event, "is_channel", False) and not getattr(event, "is_group", False):
                await app.ack(event, f"Run it inside the channel, or name it: {parts[0]} <@username|id>")
                return
            chat_id, entity = event.chat_id, await event.get_chat()
            title = getattr(entity, "title", None) or str(chat_id)
        else:
            chat_id, entity, title = await resolve_chat(app.client, argument)
    except Exception as error:  # noqa: BLE001
        await app.ack(event, f"Couldn't resolve {argument}: {type(error).__name__}: {error}")
        return
    ok, why = await can_post_media(app.client, entity)
    stored = (f"@{entity.username}" if getattr(entity, "username", None) else int(chat_id))
    await app.set(**{key: stored})
    note = "" if ok else f"\nWarning: this account cannot post there yet ({why})."
    await app.ack(event, f"{label} set: {title} ({stored}).{note}")


async def cmd_setpostchannel(app, event) -> None:
    await _set_channel(app, event, "publish_channel", "Posting channel", need_docs=False)


async def cmd_setarchive(app, event) -> None:
    await _set_channel(app, event, "archive_channel", "Archive channel", need_docs=True)


async def cmd_postcaption(app, event) -> None:
    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) < 2:
        await app.ack(event, "Current caption:\n" + (app.cfg.get("post_caption") or "") +
                      "\n\nTokens: {date} {pretty_date} {part} {parts} {count}")
        return
    await app.set(post_caption=parts[1][:900])
    preview = PS.render_caption(parts[1], today_ist(), 1, 3, 10)
    await app.ack(event, "Posting caption saved. Preview:\n" + preview)


async def cmd_archive(app, event) -> None:
    parts = (event.raw_text or "").split()
    sub = parts[1].lower() if len(parts) > 1 else "status"
    if sub in ("on", "off"):
        await app.set(archive_enabled=sub == "on")
        await app.ack(event, f"Daily archiving {'enabled' if sub == 'on' else 'disabled'}.")
        return
    if sub == "run":
        await app.ack(event, "Running the archive cycle...")
        result = await archive_service.run_cycle(app)
        lines = [f"Built: {', '.join(result['built']) or '-'}",
                 f"Delivered: {', '.join(result['delivered']) or '-'}",
                 f"Purged: {', '.join(result['purged']) or '-'}"]
        if result["errors"]:
            lines.append("Problems (screenshots kept, will retry):\n" + "\n".join(result["errors"][:10]))
        await respond_long(app, event, "\n".join(lines))
        return
    counts = await shots.counts(app.db, app.self_id)
    archives = await app.db.archives.find({"account_id": app.self_id}).sort(
        [("archive_date", -1), ("part", -1)]).limit(10).to_list(10)
    lines = [f"Archive channel: {app.cfg.get('archive_channel')} "
             f"({'on' if app.cfg.get('archive_enabled', True) else 'off'})",
             f"Today (Asia/Kolkata): {today_ist()} - archives seal after midnight IST."]
    for day, by_status in sorted(counts.items()):
        lines.append(f"{day}: " + ", ".join(f"{n} {s}" for s, n in sorted(by_status.items())))
    if archives:
        lines.append("\nRecent archives:")
        for archive in archives:
            delivery = archive.get("delivery") or {}
            error = f" - {delivery['error']}" if delivery.get("error") and archive["status"] != "purged" else ""
            lines.append(f"{archive['filename']}: {archive['status']}, {archive.get('item_count', 0)} item(s), "
                         f"post {archive.get('post_status', 'pending')}{error}")
    await respond_long(app, event, "\n".join(lines))
