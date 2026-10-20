"""Business quick replies: snapshot, recreate, and /demo link relay.

Quick replies (and every Business feature) require Telegram Premium.
"""
from __future__ import annotations

import random
from itertools import groupby

from telethon import utils as tl_utils
from telethon.errors import MessageNotModifiedError
from telethon.tl import types as T
from telethon.tl.functions.messages import (
    DeleteQuickReplyShortcutRequest,
    EditMessageRequest,
    GetQuickRepliesRequest,
    GetQuickReplyMessagesRequest,
    ReorderQuickRepliesRequest,
    SendMediaRequest,
    SendMessageRequest,
    SendMultiMediaRequest,
    UploadMediaRequest,
)

from qr.database.mongo import MEDIA_BUCKET
from qr.services.entities import (
    deserialize_entities,
    entity_fallbacks,
    is_entity_error,
    serialize_entities,
    swap_link,
    tl_from_plain,
    tl_to_plain,
)

MAX_BACKUP_MEDIA_BYTES = 50 * 1024 * 1024


def _rid() -> int:
    return random.randrange(-(2 ** 63), 2 ** 63)


async def list_shortcuts(client) -> list:
    result = await client(GetQuickRepliesRequest(hash=0))
    return list(getattr(result, "quick_replies", []) or [])


async def shortcut_messages(client, shortcut_id: int) -> list:
    result = await client(GetQuickReplyMessagesRequest(shortcut_id=shortcut_id, hash=0, id=None))
    messages = [m for m in (getattr(result, "messages", []) or []) if isinstance(m, T.Message)]
    return sorted(messages, key=lambda m: m.id)


# ---------------------------------------------------------------------------
# /demo link relay (existing behaviour)
# ---------------------------------------------------------------------------
async def swap_in_shortcut(client, shortcut_id: int, new_link: str) -> int:
    updated = 0
    for msg in await shortcut_messages(client, shortcut_id):
        swapped = swap_link(msg.message or "", msg.entities, new_link)
        if swapped is None:
            continue
        new_text, new_entities = swapped
        had_preview = isinstance(getattr(msg, "media", None), T.MessageMediaWebPage)
        try:
            await client(EditMessageRequest(
                peer=T.InputPeerSelf(), id=msg.id, message=new_text,
                entities=new_entities or None, quick_reply_shortcut_id=shortcut_id,
                no_webpage=not had_preview,
            ))
            updated += 1
        except MessageNotModifiedError:
            pass
    return updated


async def update_link(client, name: str, new_link: str) -> str:
    shortcuts = await list_shortcuts(client)
    target = next((q for q in shortcuts if q.shortcut == name), None)
    if target is None:
        await client(SendMessageRequest(
            peer=T.InputPeerSelf(), message=new_link,
            quick_reply_shortcut=T.InputQuickReplyShortcut(shortcut=name),
            random_id=_rid(),
        ))
        return "created (no existing post to preserve)"
    updated = await swap_in_shortcut(client, target.shortcut_id, new_link)
    return f"updated link in {updated} message(s)" if updated else "no link in the post"


async def find_link_in_shortcut(client, name: str):
    from qr.services.entities import FIND_LINK_RE

    for shortcut in await list_shortcuts(client):
        if shortcut.shortcut != name:
            continue
        for msg in await shortcut_messages(client, shortcut.shortcut_id):
            hit = FIND_LINK_RE.search(msg.message or "")
            if hit:
                return hit.group(0)
            for entity in msg.entities or []:
                url = getattr(entity, "url", None)
                if url and FIND_LINK_RE.search(url):
                    return url
    return None


# ---------------------------------------------------------------------------
# Snapshot (backup)
# ---------------------------------------------------------------------------
async def snapshot_media(client, db, account_id: int, media, issues: list) -> dict | None:
    """Describe a message's media and store its bytes in GridFS."""
    if media is None:
        return None
    if isinstance(media, T.MessageMediaWebPage):
        page = getattr(media, "webpage", None)
        return {"kind": "webpage", "url": getattr(page, "url", None)}
    if isinstance(media, T.MessageMediaGeo):
        geo = media.geo
        return {"kind": "geo", "lat": getattr(geo, "lat", None), "long": getattr(geo, "long", None)}
    if isinstance(media, T.MessageMediaVenue):
        geo = media.geo
        return {"kind": "venue", "lat": getattr(geo, "lat", None), "long": getattr(geo, "long", None),
                "title": media.title, "address": media.address, "provider": media.provider,
                "venue_id": media.venue_id, "venue_type": media.venue_type}
    if isinstance(media, T.MessageMediaContact):
        return {"kind": "contact", "phone_number": media.phone_number,
                "first_name": media.first_name, "last_name": media.last_name,
                "vcard": media.vcard}
    if isinstance(media, T.MessageMediaDice):
        return {"kind": "dice", "emoticon": media.emoticon}

    if isinstance(media, T.MessageMediaPhoto) and isinstance(media.photo, T.Photo):
        info = {"kind": "photo", "spoiler": bool(media.spoiler)}
        size = max((getattr(s, "size", 0) or max(getattr(s, "sizes", [0]) or [0])
                    for s in media.photo.sizes), default=0)
    elif isinstance(media, T.MessageMediaDocument) and isinstance(media.document, T.Document):
        document = media.document
        info = {
            "kind": "document",
            "spoiler": bool(media.spoiler),
            "mime_type": document.mime_type,
            "attributes": tl_to_plain(list(document.attributes or [])),
            "file_name": next((a.file_name for a in document.attributes or []
                               if isinstance(a, T.DocumentAttributeFilename)), None),
            "doc_ref": {"id": document.id, "access_hash": document.access_hash},
        }
        size = int(document.size or 0)
    else:
        issues.append(f"unsupported media type {type(media).__name__}")
        return {"kind": "unsupported", "type": type(media).__name__}

    info["size"] = size
    if size > MAX_BACKUP_MEDIA_BYTES:
        info["stored"] = False
        info["reason"] = f"larger than {MAX_BACKUP_MEDIA_BYTES // (1024 * 1024)} MB; only a reference was saved"
        issues.append(info["reason"])
        return info
    data = await client.download_media(media, file=bytes)
    if not data:
        info["stored"] = False
        info["reason"] = "download returned no data"
        issues.append("media download returned no data")
        return info
    blob_id, digest = await db.put_blob(
        MEDIA_BUCKET, data, info.get("file_name") or info["kind"],
        {"account_id": account_id, "kind": f"quick_reply_{info['kind']}"})
    info.update({"stored": True, "blob_id": blob_id, "sha256": digest, "size": len(data)})
    return info


async def snapshot_message(client, db, account_id: int, msg, issues: list) -> dict:
    data = {
        "id": msg.id,
        "date": msg.date,
        "text": msg.message or "",
        "entities": serialize_entities(msg.entities),
        "grouped_id": msg.grouped_id,
        "invert_media": bool(getattr(msg, "invert_media", False)),
        "link_preview": isinstance(msg.media, T.MessageMediaWebPage),
        "reply_markup": tl_to_plain(msg.reply_markup) if msg.reply_markup else None,
        "media": await snapshot_media(client, db, account_id, msg.media, issues),
    }
    custom = [e.document_id for e in (msg.entities or [])
              if isinstance(e, T.MessageEntityCustomEmoji)]
    if custom:
        data["custom_emoji_ids"] = custom
    return data


async def snapshot_all(client, db, account_id: int) -> tuple[list, list]:
    """Return ``(shortcuts, issues)`` describing every quick reply in order."""
    issues: list[str] = []
    out = []
    for order, shortcut in enumerate(await list_shortcuts(client)):
        messages = await shortcut_messages(client, shortcut.shortcut_id)
        snapshots = []
        for msg in messages:
            local: list[str] = []
            snapshots.append(await snapshot_message(client, db, account_id, msg, local))
            issues.extend(f"/{shortcut.shortcut} message {msg.id}: {item}" for item in local)
        out.append({
            "shortcut_id": shortcut.shortcut_id,
            "name": shortcut.shortcut,
            "command": "/" + shortcut.shortcut,
            "order": order,
            "count": shortcut.count,
            "messages": snapshots,
        })
        if len(messages) != shortcut.count:
            issues.append(f"/{shortcut.shortcut}: Telegram reported {shortcut.count} "
                          f"message(s) but returned {len(messages)}")
    return out, issues


def comparable(shortcut_messages_data: list) -> list:
    """Text, formatting and media-type signature used to detect 'already correct'."""
    sig = []
    for message in shortcut_messages_data:
        media = message.get("media") or {}
        sig.append((
            message.get("text") or "",
            tuple(sorted((e.get("_type"), e.get("offset"), e.get("length"))
                         for e in message.get("entities") or [])),
            media.get("kind"),
        ))
    return sig


def live_signature(messages: list) -> list:
    sig = []
    for msg in messages:
        media = msg.media
        kind = None
        if isinstance(media, T.MessageMediaPhoto):
            kind = "photo"
        elif isinstance(media, T.MessageMediaDocument):
            kind = "document"
        elif isinstance(media, T.MessageMediaWebPage):
            kind = "webpage"
        elif isinstance(media, T.MessageMediaVenue):
            kind = "venue"
        elif isinstance(media, T.MessageMediaGeo):
            kind = "geo"
        elif isinstance(media, T.MessageMediaContact):
            kind = "contact"
        elif isinstance(media, T.MessageMediaDice):
            kind = "dice"
        elif media is not None:
            kind = "unsupported"
        sig.append((
            msg.message or "",
            tuple(sorted((type(e).__name__, e.offset, e.length) for e in msg.entities or [])),
            kind,
        ))
    return sig


# ---------------------------------------------------------------------------
# Recreate (restore)
# ---------------------------------------------------------------------------
async def _send_entities(client, entities, notes: list) -> list:
    """Convert stored entities into sendable ones (mention-name needs InputUser)."""
    out = []
    for entity in deserialize_entities(entities):
        if isinstance(entity, T.MessageEntityMentionName):
            try:
                user = await client.get_input_entity(entity.user_id)
                out.append(T.InputMessageEntityMentionName(
                    offset=entity.offset, length=entity.length, user_id=user))
            except Exception:  # noqa: BLE001
                notes.append(f"mention of user {entity.user_id} dropped (user not reachable)")
            continue
        out.append(entity)
    return out


async def build_input_media(client, db, media: dict | None, notes: list):
    if not media:
        return None
    kind = media.get("kind")
    if kind in ("webpage", None):
        return None
    if kind == "geo":
        return T.InputMediaGeoPoint(geo_point=T.InputGeoPoint(lat=media["lat"], long=media["long"]))
    if kind == "venue":
        return T.InputMediaVenue(
            geo_point=T.InputGeoPoint(lat=media["lat"], long=media["long"]),
            title=media.get("title") or "", address=media.get("address") or "",
            provider=media.get("provider") or "", venue_id=media.get("venue_id") or "",
            venue_type=media.get("venue_type") or "")
    if kind == "contact":
        return T.InputMediaContact(
            phone_number=media.get("phone_number") or "", first_name=media.get("first_name") or "",
            last_name=media.get("last_name") or "", vcard=media.get("vcard") or "")
    if kind == "dice":
        return T.InputMediaDice(emoticon=media.get("emoticon") or "\U0001F3B2")
    if kind not in ("photo", "document"):
        raise ValueError(f"media type {media.get('type') or kind} cannot be recreated")
    if not media.get("stored"):
        raise ValueError(media.get("reason") or "media bytes were not stored in the backup")
    data = await db.get_blob(MEDIA_BUCKET, media["blob_id"])
    if kind == "photo":
        uploaded = await client.upload_file(data, file_name="photo.jpg")
        return T.InputMediaUploadedPhoto(file=uploaded, spoiler=media.get("spoiler") or None)
    attributes = []
    for attribute in tl_from_plain(media.get("attributes") or []):
        if isinstance(attribute, T.DocumentAttributeSticker):
            attribute.stickerset = T.InputStickerSetEmpty()
        attributes.append(attribute)
    uploaded = await client.upload_file(data, file_name=media.get("file_name") or "file")
    return T.InputMediaUploadedDocument(
        file=uploaded, mime_type=media.get("mime_type") or "application/octet-stream",
        attributes=attributes, spoiler=media.get("spoiler") or None)


def _groups(messages: list):
    """Consecutive messages sharing a grouped_id form one album."""
    key = lambda m: m.get("grouped_id") or f"single-{m.get('id')}"  # noqa: E731
    for _gid, items in groupby(messages, key=key):
        yield list(items)


async def recreate_shortcut(client, db, name: str, messages: list) -> list[str]:
    """Send ``messages`` into quick reply ``name``. Returns notes; raises on failure."""
    notes: list[str] = []
    shortcut = T.InputQuickReplyShortcut(shortcut=name)
    for group in _groups(messages):
        if any(m.get("reply_markup") for m in group):
            notes.append("inline buttons cannot be attached by a user account; text kept")
        if len(group) > 1:
            singles = []
            for message in group:
                media = await build_input_media(client, db, message.get("media"), notes)
                uploaded = await client(UploadMediaRequest(peer=T.InputPeerSelf(), media=media))
                entities = await _send_entities(client, message.get("entities"), notes)
                singles.append(T.InputSingleMedia(
                    media=tl_utils.get_input_media(uploaded), random_id=_rid(),
                    message=message.get("text") or "", entities=entities or None))
            await client(SendMultiMediaRequest(
                peer=T.InputPeerSelf(), multi_media=singles, quick_reply_shortcut=shortcut,
                invert_media=group[0].get("invert_media") or None))
            continue
        message = group[0]
        entities = await _send_entities(client, message.get("entities"), notes)
        media = await build_input_media(client, db, message.get("media"), notes)
        last_error = None
        for attempt in entity_fallbacks(entities):
            try:
                if media is None:
                    await client(SendMessageRequest(
                        peer=T.InputPeerSelf(), message=message.get("text") or "",
                        entities=attempt or None, no_webpage=not message.get("link_preview"),
                        invert_media=message.get("invert_media") or None,
                        quick_reply_shortcut=shortcut, random_id=_rid()))
                else:
                    await client(SendMediaRequest(
                        peer=T.InputPeerSelf(), media=media, message=message.get("text") or "",
                        entities=attempt or None, invert_media=message.get("invert_media") or None,
                        quick_reply_shortcut=shortcut, random_id=_rid()))
                if len(attempt) != len(entities):
                    notes.append(f"{len(entities) - len(attempt)} formatting/custom-emoji "
                                 "entity(ies) rejected by Telegram and dropped")
                last_error = None
                break
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not is_entity_error(error):
                    break
        if last_error is not None:
            raise last_error
    return notes


async def delete_shortcut(client, shortcut_id: int) -> None:
    await client(DeleteQuickReplyShortcutRequest(shortcut_id=shortcut_id))


async def reorder(client, names_in_order: list[str]) -> bool:
    by_name = {q.shortcut: q.shortcut_id for q in await list_shortcuts(client)}
    order = [by_name[n] for n in names_in_order if n in by_name]
    remaining = [sid for name, sid in by_name.items() if name not in names_in_order]
    if not order:
        return False
    await client(ReorderQuickRepliesRequest(order=order + remaining))
    return True
