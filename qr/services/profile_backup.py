"""``.save`` - back up the account profile, Business setup and quick replies.

Every field gets an explicit status in ``fields``:

* ``saved``        retrieved and stored
* ``empty``        retrieved; the account has no value for it
* ``unsupported``  Telegram/Telethon offers no way to retrieve it
* ``failed``       retrieval or storage failed (the backup becomes *partial*)
"""
from __future__ import annotations

import hashlib
import json

from telethon.tl import types as T
from telethon.tl.functions.account import GetBusinessChatLinksRequest
from telethon.tl.functions.photos import GetUserPhotosRequest
from telethon.tl.functions.users import GetFullUserRequest

from qr import ui
from qr.database import backups as repo
from qr.database.mongo import MEDIA_BUCKET, SCHEMA_VERSION, utcnow
from qr.services import quick_replies
from qr.services.entities import serialize_entities, tl_to_plain

MAX_PROFILE_PHOTOS = 50

# Settings that describe *how QuickReply behaves* and are portable.
PORTABLE_SETTINGS = (
    "greet_new", "online_minutes", "shortcut", "relay_link", "swap_greeting",
    "done_template", "done_entities", "channel_template", "channel_entities",
    "rio_pct", "marco_pct", "share_base", "order_prefix", "order_id_length",
    "archive_enabled", "post_caption",
)
# Settings that point at chats; restored only if the target can resolve them.
CHAT_SETTINGS = ("payment_channel", "archive_channel", "publish_channel", "link_source")

UNSUPPORTED = {
    "saved_locations": "Telegram exposes no API for saved locations; only the "
                       "Business location is retrievable (backed up separately).",
    "profile_decorations": "Collectible gifts/profile decorations are not available "
                           "in the Telegram API layer used by Telethon 1.36.",
    "stories": "Stories are out of scope for QuickReply backups.",
    "privacy_settings": "Privacy rules reference account-specific user lists and "
                        "are not part of QuickReply backups.",
    "phone_number": "The phone number is tied to the SIM/account and cannot be restored.",
}

_VOLATILE_KEYS = {"blob_id", "video_blob_id", "doc_ref", "date", "verified_at"}


def _strip_volatile(value):
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def fingerprint(document: dict) -> str:
    payload = {key: document.get(key) for key in ("profile", "business", "quick_replies", "app_settings")}
    canonical = json.dumps(_strip_volatile(payload), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Fields:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def mark(self, name: str, status: str, reason: str | None = None, **extra) -> None:
        entry = {"status": status}
        if reason:
            entry["reason"] = reason[:500]
        entry.update(extra)
        self.items[name] = entry

    def failed(self) -> list[str]:
        return [k for k, v in self.items.items() if v["status"] == "failed"]


def _largest_static(photo: T.Photo):
    def size_of(s):
        if isinstance(s, T.PhotoSizeProgressive):
            return max(s.sizes or [0])
        return getattr(s, "size", 0) or 0
    candidates = [s for s in photo.sizes or []
                  if isinstance(s, (T.PhotoSize, T.PhotoSizeProgressive))]
    return max(candidates, key=size_of) if candidates else None


async def _snapshot_photo(client, db, account_id: int, photo, *, kind: str) -> dict:
    """Store the static image (and animated video, if any) of a profile photo."""
    entry = {"photo_id": photo.id, "date": photo.date, "has_video": bool(photo.video_sizes)}
    static = _largest_static(photo)
    data = await client.download_media(photo, file=bytes, thumb=static.type if static else None)
    if not data:
        raise ValueError("photo download returned no data")
    blob_id, digest = await db.put_blob(
        MEDIA_BUCKET, data, f"{kind}.jpg", {"account_id": account_id, "kind": kind})
    entry.update({"blob_id": blob_id, "sha256": digest, "size": len(data)})
    videos = [v for v in (photo.video_sizes or []) if isinstance(v, T.VideoSize)]
    if videos:
        video = max(videos, key=lambda v: v.size or 0)
        clip = await client.download_media(photo, file=bytes, thumb=video)
        if clip:
            vid, vsha = await db.put_blob(
                MEDIA_BUCKET, clip, f"{kind}.mp4", {"account_id": account_id, "kind": kind + "_video"})
            entry.update({"video_blob_id": vid, "video_sha256": vsha,
                          "video_start_ts": video.video_start_ts})
    return entry


async def collect(app, account_id: int | None = None) -> dict:
    """Read everything retrievable from the logged-in account."""
    client, db = app.client, app.db
    account_id = int(account_id or app.self_id)
    fields = _Fields()
    document: dict = {
        "account_id": account_id,
        "schema_version": SCHEMA_VERSION,
        "created_at": utcnow(),
        "profile": {},
        "business": {},
        "quick_replies": [],
        "app_settings": {},
        "issues": [],
    }
    profile = document["profile"]
    business = document["business"]

    me = await client.get_me()
    full = await client(GetFullUserRequest(T.InputUserSelf()))
    user_full = full.full_user
    profile["premium"] = bool(getattr(me, "premium", False))

    # ---- identity ---------------------------------------------------------
    profile["first_name"] = me.first_name or ""
    profile["last_name"] = me.last_name or ""
    fields.mark("first_name", "saved" if profile["first_name"] else "empty")
    fields.mark("last_name", "saved" if profile["last_name"] else "empty")
    profile["username"] = me.username or ""
    profile["usernames"] = [
        {"username": u.username, "active": bool(u.active), "editable": bool(u.editable)}
        for u in (me.usernames or [])
    ]
    fields.mark("username", "saved" if (profile["username"] or profile["usernames"]) else "empty")
    profile["about"] = user_full.about or ""
    fields.mark("about", "saved" if profile["about"] else "empty")
    birthday = getattr(user_full, "birthday", None)
    profile["birthday"] = ({"day": birthday.day, "month": birthday.month, "year": birthday.year}
                           if birthday else None)
    fields.mark("birthday", "saved" if birthday else "empty")
    profile["personal_channel_id"] = getattr(user_full, "personal_channel_id", None)
    fields.mark("personal_channel", "saved" if profile["personal_channel_id"] else "empty")

    # ---- colours, emoji status -------------------------------------------
    for key, value in (("color", me.color), ("profile_color", me.profile_color)):
        profile[key] = ({"color": value.color, "background_emoji_id": value.background_emoji_id}
                        if isinstance(value, T.PeerColor) else None)
        fields.mark(key, "saved" if profile[key] else "empty")
    status = me.emoji_status
    if isinstance(status, (T.EmojiStatus, T.EmojiStatusUntil)):
        profile["emoji_status"] = {"document_id": status.document_id,
                                   "until": getattr(status, "until", None)}
        fields.mark("emoji_status", "saved")
    else:
        profile["emoji_status"] = None
        fields.mark("emoji_status", "empty")

    # ---- profile photos ---------------------------------------------------
    try:
        result = await client(GetUserPhotosRequest(
            user_id=T.InputUserSelf(), offset=0, max_id=0, limit=MAX_PROFILE_PHOTOS))
        photos = [p for p in (result.photos or []) if isinstance(p, T.Photo)]
        current_id = getattr(getattr(user_full, "profile_photo", None), "id", None)
        stored = []
        for photo in photos:
            entry = await _snapshot_photo(client, db, account_id, photo, kind="profile_photo")
            entry["is_current"] = photo.id == current_id
            stored.append(entry)
        profile["photos"] = stored
        total = getattr(result, "count", len(photos))
        if total > len(photos):
            fields.mark("profile_photos", "saved",
                        f"only the newest {len(photos)} of {total} photos are kept", count=len(stored))
        else:
            fields.mark("profile_photos", "saved" if stored else "empty", count=len(stored))
    except Exception as error:  # noqa: BLE001
        profile["photos"] = []
        fields.mark("profile_photos", "failed", f"{type(error).__name__}: {error}")
    fallback = getattr(user_full, "fallback_photo", None)
    if isinstance(fallback, T.Photo):
        try:
            profile["fallback_photo"] = await _snapshot_photo(
                client, db, account_id, fallback, kind="fallback_photo")
            fields.mark("fallback_photo", "saved")
        except Exception as error:  # noqa: BLE001
            fields.mark("fallback_photo", "failed", f"{type(error).__name__}: {error}")
    else:
        profile["fallback_photo"] = None
        fields.mark("fallback_photo", "empty")

    # ---- quick replies ----------------------------------------------------
    shortcut_names: dict[int, str] = {}
    try:
        shortcuts, issues = await quick_replies.snapshot_all(client, db, account_id)
        document["quick_replies"] = shortcuts
        document["issues"].extend(issues)
        shortcut_names = {s["shortcut_id"]: s["name"] for s in shortcuts}
        messages = sum(len(s["messages"]) for s in shortcuts)
        not_stored = sum(1 for s in shortcuts for m in s["messages"]
                         if (m.get("media") or {}).get("stored") is False)
        if not_stored:
            fields.mark("quick_replies", "failed",
                        f"{not_stored} media item(s) could not be stored", count=len(shortcuts),
                        messages=messages)
        else:
            fields.mark("quick_replies", "saved" if shortcuts else "empty",
                        count=len(shortcuts), messages=messages)
    except Exception as error:  # noqa: BLE001
        reason = f"{type(error).__name__}: {error}"
        if "PREMIUM" in reason.upper():
            fields.mark("quick_replies", "unsupported", "quick replies require Telegram Premium")
        else:
            fields.mark("quick_replies", "failed", reason)

    # ---- business -----------------------------------------------------------
    hours = getattr(user_full, "business_work_hours", None)
    business["work_hours"] = tl_to_plain(hours) if hours else None
    fields.mark("business_work_hours", "saved" if hours else "empty")
    location = getattr(user_full, "business_location", None)
    if location:
        geo = location.geo_point if isinstance(location.geo_point, T.GeoPoint) else None
        business["location"] = {"address": location.address,
                                "lat": geo.lat if geo else None, "long": geo.long if geo else None}
        fields.mark("business_location", "saved")
    else:
        business["location"] = None
        fields.mark("business_location", "empty")
    for key, attr in (("greeting", "business_greeting_message"), ("away", "business_away_message")):
        value = getattr(user_full, attr, None)
        if value is None:
            business[key] = None
            fields.mark(f"business_{key}", "empty")
            continue
        entry = tl_to_plain(value)
        entry["shortcut_name"] = shortcut_names.get(value.shortcut_id)
        business[key] = entry
        if entry["shortcut_name"] is None:
            fields.mark(f"business_{key}", "failed",
                        "its quick reply could not be backed up, so it cannot be recreated")
        else:
            fields.mark(f"business_{key}", "saved")
    intro = getattr(user_full, "business_intro", None)
    if intro:
        entry = {"title": intro.title, "description": intro.description, "sticker": None}
        if isinstance(intro.sticker, T.Document):
            entry["sticker"] = {"id": intro.sticker.id, "access_hash": intro.sticker.access_hash}
        business["intro"] = entry
        fields.mark("business_intro", "saved")
    else:
        business["intro"] = None
        fields.mark("business_intro", "empty")
    try:
        links = await client(GetBusinessChatLinksRequest())
        business["chat_links"] = [
            {"title": link.title, "message": link.message,
             "entities": serialize_entities(link.entities)}
            for link in (links.links or [])
        ]
        fields.mark("business_chat_links", "saved" if business["chat_links"] else "empty",
                    count=len(business["chat_links"]))
    except Exception as error:  # noqa: BLE001
        business["chat_links"] = []
        reason = f"{type(error).__name__}: {error}"
        fields.mark("business_chat_links",
                    "unsupported" if "PREMIUM" in reason.upper() else "failed", reason)

    # ---- QuickReply's own configuration -----------------------------------
    cfg = await app.settings.get(account_id)
    document["app_settings"] = {key: cfg.get(key) for key in PORTABLE_SETTINGS + CHAT_SETTINGS}
    fields.mark("quickreply_settings", "saved")
    greeting = cfg.get("greeting")
    if greeting:
        try:
            message = await client.get_messages(greeting["chat_id"], ids=greeting["message_id"])
            if message is None:
                raise ValueError("the /set greeting post no longer exists")
            issues: list[str] = []
            document["app_settings"]["greeting_message"] = await quick_replies.snapshot_message(
                client, db, account_id, message, issues)
            document["issues"].extend(f"greeting: {i}" for i in issues)
            fields.mark("greeting_post", "saved")
        except Exception as error:  # noqa: BLE001
            fields.mark("greeting_post", "failed", f"{type(error).__name__}: {error}")
    else:
        fields.mark("greeting_post", "empty")

    for name, reason in UNSUPPORTED.items():
        fields.mark(name, "unsupported", reason)
    document["fields"] = fields.items
    return document


def media_refs(document: dict) -> list[tuple]:
    """Every ``(blob_id, sha256)`` the backup depends on."""
    refs = []
    profile = document.get("profile") or {}
    for photo in list(profile.get("photos") or []) + [profile.get("fallback_photo") or {}]:
        if photo.get("blob_id"):
            refs.append((photo["blob_id"], photo.get("sha256")))
        if photo.get("video_blob_id"):
            refs.append((photo["video_blob_id"], photo.get("video_sha256")))
    messages = [m for s in document.get("quick_replies") or [] for m in s.get("messages") or []]
    greeting = (document.get("app_settings") or {}).get("greeting_message")
    if greeting:
        messages.append(greeting)
    for message in messages:
        media = message.get("media") or {}
        if media.get("blob_id"):
            refs.append((media["blob_id"], media.get("sha256")))
    return refs


def summarize(document: dict) -> str:
    fields = document.get("fields") or {}
    profile = document.get("profile") or {}
    saved = sorted(k for k, v in fields.items() if v["status"] == "saved")
    empty = sorted(k for k, v in fields.items() if v["status"] == "empty")
    failed = {k: v.get("reason", "") for k, v in fields.items() if v["status"] == "failed"}
    unsupported = sorted(k for k, v in fields.items() if v["status"] == "unsupported")
    shortcuts = document.get("quick_replies") or []
    lines = [
        f"Account: {profile.get('first_name', '')} {profile.get('last_name', '')}".rstrip()
        + f" ({document.get('account_id')})",
        f"Quick replies: {len(shortcuts)} shortcut(s), "
        f"{sum(len(s['messages']) for s in shortcuts)} message(s)",
        f"Profile photos: {len(profile.get('photos') or [])}",
        "Saved: " + (", ".join(saved) or "-"),
        "Empty on this account: " + (", ".join(empty) or "-"),
        "Not retrievable: " + (", ".join(unsupported) or "-"),
    ]
    if failed:
        lines.append("FAILED: " + "; ".join(f"{k} ({v})" for k, v in failed.items()))
    return "\n".join(lines)


async def create_backup(app, *, kind: str = "manual") -> dict:
    """Collect, store, verify. Returns ``{"backup_id", "status", "summary", "unchanged"}``."""
    document = await collect(app)
    document["kind"] = kind
    document["fingerprint"] = fingerprint(document)
    fields = document["fields"]

    if kind == "manual":
        duplicate = await repo.find_duplicate(app.db, app.self_id, document["fingerprint"])
        if duplicate is not None:
            refs = media_refs(duplicate)
            if all([await app.db.verify_blob(MEDIA_BUCKET, b, s) for b, s in refs]):
                await repo.touch(app.db, duplicate["_id"])
                return {"backup_id": str(duplicate["_id"]), "status": "complete",
                        "unchanged": True, "summary": summarize(duplicate)}

    document["status"] = "verifying"
    document["summary"] = summarize(document)
    backup_id = await repo.insert(app.db, document)

    # Verify the stored document and every referenced media object.
    stored = await repo.by_id(app.db, str(backup_id))
    problems = []
    if stored is None or stored.get("fingerprint") != document["fingerprint"]:
        problems.append("stored document could not be read back")
    bad_media = 0
    for blob_id, sha in media_refs(document):
        if not await app.db.verify_blob(MEDIA_BUCKET, blob_id, sha):
            bad_media += 1
    if bad_media:
        problems.append(f"{bad_media} media object(s) failed verification")
        fields["media_verification"] = {"status": "failed", "reason": problems[-1]}
    failed = [k for k, v in fields.items() if v["status"] == "failed"]
    status = "complete" if not failed and not problems else "partial"
    summary = summarize(document) + ("\n" + "; ".join(problems) if problems else "")
    await repo.set_status(app.db, backup_id, status, fields=fields, summary=summary)
    await app.db.record_audit(app.self_id, "backup", backup_id=str(backup_id), kind=kind,
                              status=status, failed=failed)
    ui.info(f"Backup {backup_id} stored ({status}).")
    return {"backup_id": str(backup_id), "status": status, "unchanged": False, "summary": summary}
