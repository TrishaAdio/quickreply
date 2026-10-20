"""``.setup`` - apply a saved backup to the account QuickReply is logged in as.

The target is always the *current* session (the account that typed the
command), never another account, so settings cannot land on an unintended
session. :func:`run` is used twice: ``dry_run=True`` builds the preview shown
before confirmation, ``dry_run=False`` applies the changes.

Every field ends in one of: ``restored``, ``already_correct``,
``unsupported``, ``unavailable`` (not in the backup) or ``failed``.
In a dry run ``restored`` is reported as ``will_restore``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from telethon.tl import types as T
from telethon.tl.functions.account import (
    CreateBusinessChatLinkRequest,
    GetBusinessChatLinksRequest,
    UpdateBirthdayRequest,
    UpdateBusinessAwayMessageRequest,
    UpdateBusinessGreetingMessageRequest,
    UpdateBusinessIntroRequest,
    UpdateBusinessLocationRequest,
    UpdateBusinessWorkHoursRequest,
    UpdateColorRequest,
    UpdateEmojiStatusRequest,
    UpdatePersonalChannelRequest,
    UpdateProfileRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.photos import GetUserPhotosRequest, UploadProfilePhotoRequest
from telethon.tl.functions.users import GetFullUserRequest

from qr.database.mongo import MEDIA_BUCKET, sha256_hex, utcnow
from qr.services import quick_replies
from qr.services.entities import deserialize_entities, tl_from_plain, tl_to_plain
from qr.services.profile_backup import CHAT_SETTINGS, PORTABLE_SETTINGS, _largest_static
from qr.services.telegram_utils import flood_safe

PREMIUM_REASON = "requires Telegram Premium on the target account"
STATUS_ORDER = ("failed", "will_restore", "restored", "already_correct", "unsupported", "unavailable")


class Report:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.fields: dict[str, dict] = {}

    def set(self, name: str, status: str, reason: str = "") -> None:
        if status == "restored" and self.dry_run:
            status = "will_restore"
        entry = {"status": status}
        if reason:
            entry["reason"] = str(reason)[:400]
        self.fields[name] = entry

    def fail(self, name: str, error) -> None:
        detail = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self.set(name, "failed", detail)

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.fields.items() if v["status"] == "failed"]

    def text(self, limit: int = 60) -> str:
        labels = {
            "will_restore": "will change", "restored": "restored",
            "already_correct": "already correct", "unsupported": "unsupported",
            "unavailable": "not in backup", "failed": "FAILED",
        }
        rows = sorted(self.fields.items(),
                      key=lambda kv: (STATUS_ORDER.index(kv[1]["status"]), kv[0]))
        lines = []
        for name, entry in rows[:limit]:
            reason = f" - {entry['reason']}" if entry.get("reason") else ""
            lines.append(f"{labels[entry['status']]}: {name}{reason}")
        if len(rows) > limit:
            lines.append(f"... +{len(rows) - limit} more")
        return "\n".join(lines)


async def _target_state(client):
    me = await client.get_me()
    full = await client(GetFullUserRequest(T.InputUserSelf()))
    return me, full.full_user


def _plain_without(value, *keys):
    plain = tl_to_plain(value) if value is not None else None
    if isinstance(plain, dict):
        for key in keys:
            plain.pop(key, None)
    return plain


async def _photo_hash(client, photo) -> str | None:
    static = _largest_static(photo)
    data = await client.download_media(photo, file=bytes, thumb=static.type if static else None)
    return sha256_hex(data) if data else None


async def _recipients(client, plain: dict | None, notes: list):
    plain = dict(plain or {})
    users = []
    for user_id in plain.get("users") or []:
        try:
            users.append(await client.get_input_entity(user_id))
        except Exception:  # noqa: BLE001
            notes.append(f"recipient {user_id} is not reachable from this account")
    return T.InputBusinessRecipients(
        existing_chats=plain.get("existing_chats"), new_chats=plain.get("new_chats"),
        contacts=plain.get("contacts"), non_contacts=plain.get("non_contacts"),
        exclude_selected=plain.get("exclude_selected"), users=users or None)


async def run(app, backup: dict, *, dry_run: bool) -> Report:
    client, db = app.client, app.db
    report = Report(dry_run)
    profile = backup.get("profile") or {}
    business = backup.get("business") or {}
    me, full = await _target_state(client)
    premium = bool(getattr(me, "premium", False))

    async def call(request):
        return await flood_safe(app, lambda: client(request))

    # 1. names + bio --------------------------------------------------------
    wanted = {"first_name": profile.get("first_name") or "",
              "last_name": profile.get("last_name") or "",
              "about": profile.get("about") or ""}
    current = {"first_name": me.first_name or "", "last_name": me.last_name or "",
               "about": full.about or ""}
    changes = {}
    for key, value in wanted.items():
        if key == "first_name" and not value:
            report.set(key, "unavailable", "backup has no first name")
        elif value == current[key]:
            report.set(key, "already_correct")
        else:
            changes[key] = value
    if changes:
        if dry_run:
            for key, value in changes.items():
                report.set(key, "restored", f"{current[key]!r} -> {value!r}")
        else:
            try:
                await call(UpdateProfileRequest(**changes))
                for key in changes:
                    report.set(key, "restored")
            except Exception as error:  # noqa: BLE001
                # Retry without the bio (ABOUT_TOO_LONG on non-Premium accounts).
                if "about" in changes and len(changes) > 1:
                    rest = {k: v for k, v in changes.items() if k != "about"}
                    try:
                        await call(UpdateProfileRequest(**rest))
                        for key in rest:
                            report.set(key, "restored")
                        report.fail("about", error)
                    except Exception as second:  # noqa: BLE001
                        for key in changes:
                            report.fail(key, second)
                else:
                    for key in changes:
                        report.fail(key, error)

    # 2. username ------------------------------------------------------------
    username = profile.get("username") or ""
    if profile.get("usernames"):
        report.set("collectible_usernames", "unsupported",
                   "additional/collectible usernames cannot be transferred by API")
    if not username:
        report.set("username", "unavailable")
    elif (me.username or "").casefold() == username.casefold():
        report.set("username", "already_correct")
    elif dry_run:
        report.set("username", "restored",
                   f"@{username} - fails if the source account still owns it")
    else:
        try:
            await call(UpdateUsernameRequest(username=username))
            report.set("username", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("username", error)

    # 3. birthday ------------------------------------------------------------
    birthday = profile.get("birthday")
    current_bd = getattr(full, "birthday", None)
    if not birthday:
        report.set("birthday", "unavailable")
    elif current_bd and (current_bd.day, current_bd.month, current_bd.year) == (
            birthday["day"], birthday["month"], birthday.get("year")):
        report.set("birthday", "already_correct")
    elif dry_run:
        report.set("birthday", "restored")
    else:
        try:
            await call(UpdateBirthdayRequest(birthday=T.Birthday(
                day=birthday["day"], month=birthday["month"], year=birthday.get("year"))))
            report.set("birthday", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("birthday", error)

    # 4. personal channel ------------------------------------------------------
    channel_id = profile.get("personal_channel_id")
    if not channel_id:
        report.set("personal_channel", "unavailable")
    elif getattr(full, "personal_channel_id", None) == channel_id:
        report.set("personal_channel", "already_correct")
    else:
        try:
            channel = await client.get_input_entity(T.PeerChannel(channel_id))
            if dry_run:
                report.set("personal_channel", "restored")
            else:
                await call(UpdatePersonalChannelRequest(channel=channel))
                report.set("personal_channel", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("personal_channel", f"channel {channel_id} not accessible from this "
                                            f"account ({type(error).__name__})")

    # 5. profile photos ------------------------------------------------------
    photos = list(profile.get("photos") or [])
    if not photos:
        report.set("profile_photos", "unavailable")
    else:
        try:
            existing = await client(GetUserPhotosRequest(
                user_id=T.InputUserSelf(), offset=0, max_id=0, limit=len(photos) + 5))
            existing_hashes = set()
            live_photos = [p for p in existing.photos or [] if isinstance(p, T.Photo)]
            existing_ids = {p.id for p in live_photos}
            for photo in live_photos:
                digest = await _photo_hash(client, photo)
                if digest:
                    existing_hashes.add(digest)
            # Telegram re-encodes uploads, so photos created by an earlier
            # restore are recognised by the photo id recorded at upload time.
            account_doc = await db.accounts.find_one({"_id": app.self_id}) or {}
            photo_map = account_doc.get("restored_photos") or {}
            for sha, photo_id in photo_map.items():
                if photo_id in existing_ids:
                    existing_hashes.add(sha)
            current_photo = next((p for p in photos if p.get("is_current")), photos[0])
            missing = [p for p in photos if p.get("sha256") not in existing_hashes]
            if not missing:
                report.set("profile_photos", "already_correct",
                           f"{len(photos)} photo(s) already present")
            elif dry_run:
                report.set("profile_photos", "restored", f"upload {len(missing)} photo(s)")
            else:
                # Oldest first so the backup's current photo ends up current.
                ordered = [p for p in reversed(photos) if p is not current_photo] + [current_photo]
                uploaded = 0
                for entry in ordered:
                    if entry.get("sha256") in existing_hashes and entry is not current_photo:
                        continue
                    data = await db.get_blob(MEDIA_BUCKET, entry["blob_id"])
                    kwargs = {"file": await client.upload_file(data, file_name="photo.jpg")}
                    if entry.get("video_blob_id"):
                        clip = await db.get_blob(MEDIA_BUCKET, entry["video_blob_id"])
                        kwargs = {"video": await client.upload_file(clip, file_name="photo.mp4"),
                                  "video_start_ts": entry.get("video_start_ts")}
                    result = await call(UploadProfilePhotoRequest(**kwargs))
                    new_photo = getattr(result, "photo", None)
                    if isinstance(new_photo, T.Photo) and entry.get("sha256"):
                        await db.accounts.update_one(
                            {"_id": app.self_id},
                            {"$set": {f"restored_photos.{entry['sha256']}": new_photo.id}})
                    uploaded += 1
                    await app.sleep(1)
                report.set("profile_photos", "restored", f"uploaded {uploaded} photo(s)")
        except Exception as error:  # noqa: BLE001
            report.fail("profile_photos", error)
    fallback = profile.get("fallback_photo")
    live_fallback = getattr(full, "fallback_photo", None)
    fallback_map = ((await db.accounts.find_one({"_id": app.self_id}) or {}).get("restored_photos") or {})
    if not fallback:
        report.set("fallback_photo", "unavailable")
    elif isinstance(live_fallback, T.Photo) and (
            fallback_map.get(fallback.get("sha256")) == live_fallback.id
            or await _photo_hash(client, live_fallback) == fallback.get("sha256")):
        report.set("fallback_photo", "already_correct")
    elif dry_run:
        report.set("fallback_photo", "restored")
    else:
        try:
            data = await db.get_blob(MEDIA_BUCKET, fallback["blob_id"])
            result = await call(UploadProfilePhotoRequest(
                fallback=True, file=await client.upload_file(data, file_name="fallback.jpg")))
            if isinstance(getattr(result, "photo", None), T.Photo) and fallback.get("sha256"):
                await db.accounts.update_one({"_id": app.self_id}, {"$set": {
                    f"restored_photos.{fallback['sha256']}": result.photo.id}})
            report.set("fallback_photo", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("fallback_photo", error)

    # 6. colours + emoji status (Premium) ---------------------------------------
    for key, for_profile, live in (("color", False, me.color), ("profile_color", True, me.profile_color)):
        value = profile.get(key)
        if not value:
            report.set(key, "unavailable")
        elif isinstance(live, T.PeerColor) and (live.color, live.background_emoji_id) == (
                value.get("color"), value.get("background_emoji_id")):
            report.set(key, "already_correct")
        elif not premium:
            report.set(key, "unsupported", PREMIUM_REASON)
        elif dry_run:
            report.set(key, "restored")
        else:
            try:
                await call(UpdateColorRequest(for_profile=for_profile or None, color=value.get("color"),
                                              background_emoji_id=value.get("background_emoji_id")))
                report.set(key, "restored")
            except Exception as error:  # noqa: BLE001
                report.fail(key, error)
    status = profile.get("emoji_status")
    live_status = me.emoji_status
    if not status:
        report.set("emoji_status", "unavailable")
    elif getattr(live_status, "document_id", None) == status.get("document_id"):
        report.set("emoji_status", "already_correct")
    elif not premium:
        report.set("emoji_status", "unsupported", PREMIUM_REASON)
    else:
        until = status.get("until")
        if isinstance(until, datetime) and until.replace(tzinfo=until.tzinfo or timezone.utc) <= utcnow():
            report.set("emoji_status", "unavailable", "the saved timed emoji status has expired")
        elif dry_run:
            report.set("emoji_status", "restored")
        else:
            try:
                value = (T.EmojiStatusUntil(document_id=status["document_id"], until=until)
                         if until else T.EmojiStatus(document_id=status["document_id"]))
                await call(UpdateEmojiStatusRequest(emoji_status=value))
                report.set("emoji_status", "restored")
            except Exception as error:  # noqa: BLE001
                report.fail("emoji_status", error)

    # 7. quick replies (Premium) ---------------------------------------------
    shortcuts = backup.get("quick_replies") or []
    if not shortcuts:
        report.set("quick_replies", "unavailable")
    elif not premium:
        report.set("quick_replies", "unsupported", PREMIUM_REASON)
    else:
        try:
            live = {q.shortcut: q for q in await quick_replies.list_shortcuts(client)}
        except Exception as error:  # noqa: BLE001
            live = None
            report.fail("quick_replies", error)
        if live is not None:
            for shortcut in shortcuts:
                field = f"quick_reply /{shortcut['name']}"
                existing = live.get(shortcut["name"])
                try:
                    if existing is not None:
                        messages = await quick_replies.shortcut_messages(client, existing.shortcut_id)
                        if quick_replies.live_signature(messages) == quick_replies.comparable(
                                shortcut["messages"]):
                            report.set(field, "already_correct")
                            continue
                    if dry_run:
                        report.set(field, "restored",
                                   "replace existing (content differs)" if existing else "create")
                        continue
                    if existing is not None:
                        await quick_replies.delete_shortcut(client, existing.shortcut_id)
                    notes = await quick_replies.recreate_shortcut(
                        client, db, shortcut["name"], shortcut["messages"])
                    report.set(field, "restored", "; ".join(dict.fromkeys(notes)))
                    await app.sleep(1)
                except Exception as error:  # noqa: BLE001
                    report.fail(field, error)
            if not dry_run:
                try:
                    await quick_replies.reorder(client, [s["name"] for s in shortcuts])
                except Exception as error:  # noqa: BLE001
                    report.fail("quick_reply_order", error)

    # 8. business profile (Premium) -------------------------------------------
    await _restore_business(app, report, business, full, premium, dry_run, call)

    # 9. QuickReply settings ------------------------------------------------
    await _restore_settings(app, report, backup.get("app_settings") or {}, dry_run)

    if not dry_run:
        await _verify(app, report, backup)
    return report


async def _restore_business(app, report, business, full, premium, dry_run, call):
    client = app.client
    keys = ("business_work_hours", "business_location", "business_intro",
            "business_greeting", "business_away", "business_chat_links")
    if not premium:
        for key in keys:
            report.set(key, "unsupported", PREMIUM_REASON)
        return

    hours = business.get("work_hours")
    if not hours:
        report.set("business_work_hours", "unavailable")
    elif _plain_without(getattr(full, "business_work_hours", None), "open_now") == \
            {k: v for k, v in hours.items() if k != "open_now"}:
        report.set("business_work_hours", "already_correct")
    elif dry_run:
        report.set("business_work_hours", "restored")
    else:
        try:
            value = tl_from_plain({k: v for k, v in hours.items() if k != "open_now"})
            await call(UpdateBusinessWorkHoursRequest(business_work_hours=value))
            report.set("business_work_hours", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("business_work_hours", error)

    location = business.get("location")
    live = getattr(full, "business_location", None)
    if not location:
        report.set("business_location", "unavailable")
    elif live and live.address == location.get("address") and (
            getattr(live.geo_point, "lat", None), getattr(live.geo_point, "long", None)) == (
            location.get("lat"), location.get("long")):
        report.set("business_location", "already_correct")
    elif dry_run:
        report.set("business_location", "restored")
    else:
        try:
            geo = (T.InputGeoPoint(lat=location["lat"], long=location["long"])
                   if location.get("lat") is not None else None)
            await call(UpdateBusinessLocationRequest(geo_point=geo, address=location.get("address")))
            report.set("business_location", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("business_location", error)

    intro = business.get("intro")
    live_intro = getattr(full, "business_intro", None)
    if not intro:
        report.set("business_intro", "unavailable")
    elif live_intro and (live_intro.title, live_intro.description) == (
            intro.get("title"), intro.get("description")):
        report.set("business_intro", "already_correct")
    elif dry_run:
        report.set("business_intro", "restored",
                   "intro sticker may not transfer" if intro.get("sticker") else "")
    else:
        note = ""
        try:
            await call(UpdateBusinessIntroRequest(intro=T.InputBusinessIntro(
                title=intro.get("title") or "", description=intro.get("description") or "")))
            if intro.get("sticker"):
                note = "intro sticker cannot be re-referenced from another account; text restored"
            report.set("business_intro", "restored", note)
        except Exception as error:  # noqa: BLE001
            report.fail("business_intro", error)

    try:
        live_shortcuts = {q.shortcut: q.shortcut_id for q in await quick_replies.list_shortcuts(client)}
    except Exception:  # noqa: BLE001
        live_shortcuts = {}
    for key, live_attr in (("greeting", "business_greeting_message"), ("away", "business_away_message")):
        field = f"business_{key}"
        saved = business.get(key)
        if not saved:
            report.set(field, "unavailable")
            continue
        name = saved.get("shortcut_name")
        live_value = getattr(full, live_attr, None)
        if live_value is not None and name and live_shortcuts.get(name) == live_value.shortcut_id \
                and _plain_without(live_value, "shortcut_id") == {
                    k: v for k, v in saved.items() if k not in ("shortcut_id", "shortcut_name")}:
            report.set(field, "already_correct")
            continue
        if dry_run:
            report.set(field, "restored", f"uses quick reply /{name}")
            continue
        shortcut_id = live_shortcuts.get(name)
        if shortcut_id is None:
            report.fail(field, f"quick reply /{name} does not exist on this account")
            continue
        notes: list[str] = []
        try:
            recipients = await _recipients(client, saved.get("recipients"), notes)
            if key == "greeting":
                message = T.InputBusinessGreetingMessage(
                    shortcut_id=shortcut_id, recipients=recipients,
                    no_activity_days=saved.get("no_activity_days") or 7)
                await call(UpdateBusinessGreetingMessageRequest(message=message))
            else:
                message = T.InputBusinessAwayMessage(
                    shortcut_id=shortcut_id, schedule=tl_from_plain(saved.get("schedule")),
                    recipients=recipients, offline_only=saved.get("offline_only"))
                await call(UpdateBusinessAwayMessageRequest(message=message))
            report.set(field, "restored", "; ".join(notes))
        except Exception as error:  # noqa: BLE001
            report.fail(field, error)

    links = business.get("chat_links") or []
    if not links:
        report.set("business_chat_links", "unavailable")
        return
    try:
        current = await client(GetBusinessChatLinksRequest())
        have = {(l.title or "", l.message or "") for l in current.links or []}
        missing = [l for l in links if (l.get("title") or "", l.get("message") or "") not in have]
        if not missing:
            report.set("business_chat_links", "already_correct")
        elif dry_run:
            report.set("business_chat_links", "restored",
                       f"create {len(missing)} link(s); URLs will differ")
        else:
            for link in missing:
                await call(CreateBusinessChatLinkRequest(link=T.InputBusinessChatLink(
                    message=link.get("message") or "",
                    entities=deserialize_entities(link.get("entities")) or None,
                    title=link.get("title") or None)))
            report.set("business_chat_links", "restored",
                       f"created {len(missing)} link(s); t.me URLs are new")
    except Exception as error:  # noqa: BLE001
        report.fail("business_chat_links", error)


async def _restore_settings(app, report, saved: dict, dry_run: bool):
    from qr.services.telegram_utils import resolve_chat

    if not saved:
        report.set("quickreply_settings", "unavailable")
        return
    current = await app.settings.get(app.self_id)
    portable = {k: saved[k] for k in PORTABLE_SETTINGS if k in saved}
    differs = {k: v for k, v in portable.items() if current.get(k) != v}
    if not differs:
        report.set("quickreply_settings", "already_correct")
    elif dry_run:
        report.set("quickreply_settings", "restored", ", ".join(sorted(differs)))
    else:
        await app.set(**differs)
        report.set("quickreply_settings", "restored", ", ".join(sorted(differs)))

    for key in CHAT_SETTINGS:
        value = saved.get(key)
        field = f"setting {key}"
        if value in (None, ""):
            report.set(field, "unavailable")
            continue
        if current.get(key) == value:
            report.set(field, "already_correct")
            continue
        try:
            if key == "link_source":
                await app.client.get_input_entity(value)
                resolved = value
            elif isinstance(value, str) and value.startswith("@"):
                await resolve_chat(app.client, value)
                resolved = value
            else:
                resolved, _entity, _title = await resolve_chat(app.client, value)
            if not dry_run:
                await app.set(**{key: resolved})
            report.set(field, "restored")
        except Exception as error:  # noqa: BLE001
            report.fail(field, f"not accessible from this account ({type(error).__name__})")

    greeting = saved.get("greeting_message")
    if not greeting:
        report.set("greeting_post", "unavailable")
    elif dry_run:
        report.set("greeting_post", "restored", "a copy is posted to Saved Messages")
    else:
        try:
            sent = await _send_copy(app, greeting)
            await app.set(greeting={"chat_id": app.self_id, "message_id": sent.id})
            report.set("greeting_post", "restored")
        except Exception as error:  # noqa: BLE001
            report.fail("greeting_post", error)


async def _send_copy(app, message: dict):
    """Post a stored message snapshot into Saved Messages."""
    notes: list[str] = []
    media = await quick_replies.build_input_media(app.client, app.db, message.get("media"), notes)
    entities = deserialize_entities(message.get("entities"))
    if media is not None:
        return await app.client.send_file("me", file=media, caption=message.get("text") or "",
                                          formatting_entities=entities or None)
    return await app.client.send_message("me", message.get("text") or "",
                                         formatting_entities=entities or None,
                                         link_preview=bool(message.get("link_preview")))


async def _verify(app, report: Report, backup: dict) -> None:
    """Re-read the account and downgrade anything that did not stick."""
    profile = backup.get("profile") or {}
    try:
        me, full = await _target_state(app.client)
    except Exception as error:  # noqa: BLE001
        report.set("verification", "failed", f"could not re-read account: {error}")
        return
    checks = {
        "first_name": (me.first_name or "") == (profile.get("first_name") or ""),
        "last_name": (me.last_name or "") == (profile.get("last_name") or ""),
        "about": (full.about or "") == (profile.get("about") or ""),
        "username": (me.username or "").casefold() == (profile.get("username") or "").casefold(),
    }
    for field, ok in checks.items():
        if report.fields.get(field, {}).get("status") == "restored" and not ok:
            report.fail(field, "Telegram accepted the change but verification shows a different value")
    try:
        live = {q.shortcut: q for q in await quick_replies.list_shortcuts(app.client)}
    except Exception:  # noqa: BLE001
        live = {}
    for shortcut in backup.get("quick_replies") or []:
        field = f"quick_reply /{shortcut['name']}"
        if report.fields.get(field, {}).get("status") != "restored":
            continue
        existing = live.get(shortcut["name"])
        if existing is None:
            report.fail(field, "shortcut missing after restore")
        elif existing.count != len(shortcut["messages"]):
            report.fail(field, f"expected {len(shortcut['messages'])} message(s), found {existing.count}")
    report.set("verification", "restored" if not report.failed else "failed",
               "account re-read after restore" + ("" if not report.failed else
                                                  f"; {len(report.failed)} field(s) failed"))
