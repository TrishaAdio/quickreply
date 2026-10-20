"""Payment logger commands (work in any chat, outgoing only).

/add <amount> [name]           reply to the payment screenshot
/setdone <template>            private receipt template (or reply to a post)
/setchannelpostofpayment <t>   payment-channel caption template
.setchannel [id|@username]     payment upload channel
/stats  /clear  /cancel        today's stats / reset / mark a post fake
.unban <id|@username>          lift a user's ban in every group you admin
"""
from __future__ import annotations

import re

from pymongo.errors import DuplicateKeyError
from telethon.errors import FloodWaitError, MessageNotModifiedError, UserNotParticipantError
from telethon.tl import types as T
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChannelParticipantsBanned, ChannelParticipantsKicked, ChatBannedRights
from telethon.utils import get_peer_id

from qr import ui
from qr.app import sent_message_ids
from qr.database.mongo import utcnow
from qr.database import screenshots as shots
from qr.services import payments as P
from qr.services.clock import today_ist, ts_day_window, ts_now
from qr.services.entities import (
    entity_fallbacks,
    fit_media_caption,
    is_entity_error,
    serialize_entities,
    split_plain_text,
    u16,
)
from qr.services.telegram_utils import resolve_chat

ADD_RE = re.compile(r"^/add(?:\s+(\S+)(?:\s+([\s\S]+?))?)?\s*$", re.IGNORECASE)
SETCHANNEL_RE = re.compile(r"^[./]setchannel(?:\s+([\s\S]+))?$", re.IGNORECASE)
UNBAN_RE = re.compile(r"^[./]unban(?:\s+([\s\S]+))?$", re.IGNORECASE)
RETRYABLE_POST = ("queued", "posting", "failed", "untracked", "not_configured", None)
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def todays_payments(app) -> list:
    start, end = ts_day_window(today_ist())
    cursor = app.db.payments.find({"account_id": app.self_id, "status": "valid",
                                   "ts": {"$gte": start, "$lt": end}})
    return await cursor.to_list(None)


async def _edit_rich(event, text, entities) -> int:
    original = list(entities or [])
    last_error = None
    for attempt in entity_fallbacks(original):
        try:
            await event.edit(text, formatting_entities=attempt or None)
            return len(original) - len(attempt)
        except MessageNotModifiedError:
            return len(original) - len(attempt)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if not is_entity_error(error):
                raise
    if last_error is not None:
        raise last_error
    return 0


async def deliver_receipt(app, event, text, entities, amount, name, order_id, today) -> bool:
    if u16(text) <= 4096:
        try:
            dropped = await _edit_rich(event, text, entities)
            if dropped:
                ui.warn(f"Receipt: dropped {dropped} unsupported entity(ies).")
            return True
        except Exception as error:  # noqa: BLE001
            ui.warn(f"Rich receipt edit failed; sending plain text: {type(error).__name__}")
            return await app.ack(event, text)
    fallback = P.required_payment_text(app.cfg, amount, name, order_id, today, include_current=True)
    chunks = split_plain_text(fallback, header=f"Order ID: {order_id}")
    if not await app.ack(event, chunks[0]):
        return False
    for chunk in chunks[1:]:
        try:
            await event.respond(chunk)
        except Exception:  # noqa: BLE001
            return False
    return True


def _is_image(message) -> tuple[bool, str]:
    media = getattr(message, "media", None)
    if isinstance(media, T.MessageMediaPhoto):
        return True, ".jpg"
    document = getattr(media, "document", None)
    mime = getattr(document, "mime_type", "") or ""
    if mime in IMAGE_EXT:
        return True, IMAGE_EXT[mime]
    return False, ""


async def capture_screenshot(app, reply, order_id: str) -> str | None:
    """Store the proof image for the daily archive (best effort, never blocks /add)."""
    is_image, ext = _is_image(reply)
    if not is_image:
        return None
    try:
        data = await app.client.download_media(reply, file=bytes)
        doc, _created = await shots.add(
            app.db, app.self_id, data, captured_at=reply.date,
            source_chat_id=int(reply.chat_id), source_message_id=int(reply.id),
            mime_type="image/jpeg" if ext == ".jpg" else f"image/{ext[1:]}",
            extension=ext, order_id=order_id)
        return str(doc["_id"])
    except Exception as error:  # noqa: BLE001
        ui.warn(f"Screenshot capture failed for {order_id}: {type(error).__name__}: {error}")
        return None


async def _find_channel_post(app, target: int, order_id: str) -> int | None:
    try:
        async for message in app.client.iter_messages(target, search=order_id, limit=50):
            text = str(getattr(message, "message", "") or "")
            if order_id.casefold() in text.casefold() and getattr(message, "media", None) is not None:
                return int(message.id)
    except Exception as error:  # noqa: BLE001
        ui.warn(f"Payment-channel reconciliation failed: {type(error).__name__}: {error}")
    return None


async def _update(app, payment: dict, **values) -> None:
    payment.update(values)
    await app.db.payments.update_one({"_id": payment["_id"]}, {"$set": values})


async def post_payment_to_channel(app, payment: dict, *, source_message=None,
                                  notify_failure: bool = True) -> bool:
    """Post or reconcile one committed payment's proof image."""
    if payment.get("status") != "valid":
        return False
    if payment.get("post_status") == "posted" and payment.get("post_message_id"):
        return True
    target = app.cfg.get("payment_channel")
    snapshot = payment.get("post_chat_id")
    if target is None:
        await _update(app, payment, post_status="not_configured",
                      post_error="payment channel is not configured",
                      next_post_retry=ts_now() + 60)
        if notify_failure and not payment.get("post_failure_notified"):
            await _update(app, payment, post_failure_notified=True)
            await app.notify(f"Payment {payment['order_id']} was recorded, but no payment channel "
                             "is set. Run .setchannel in the channel; delivery retries automatically.")
        return False
    order_id = payment["order_id"]
    if payment.get("post_status") in ("posting", "untracked", "failed"):
        for candidate in dict.fromkeys(c for c in (snapshot, target) if c is not None):
            existing = await _find_channel_post(app, candidate, order_id)
            if existing:
                await _update(app, payment, post_chat_id=int(candidate), post_message_id=existing,
                              post_status="posted", post_error=None, next_post_retry=None)
                return True
    if source_message is None and payment.get("source_message_id"):
        try:
            source_message = await app.client.get_messages(
                payment["source_chat_id"], ids=payment["source_message_id"])
        except Exception as error:  # noqa: BLE001
            payment["post_error"] = f"source fetch failed: {type(error).__name__}"
    attempts = int(payment.get("post_attempts") or 0) + 1
    if source_message is None or not getattr(source_message, "media", None):
        await _update(app, payment, post_attempts=attempts, post_status="failed",
                      post_error=payment.get("post_error") or "source payment image is unavailable",
                      next_post_retry=ts_now() + P.post_retry_delay(attempts))
        if notify_failure and not payment.get("post_failure_notified"):
            await _update(app, payment, post_failure_notified=True)
            await app.notify(f"Payment-channel delivery failed for {order_id}: "
                             f"{payment['post_error']}. It stays queued for retry.")
        return False

    amount = P.parse_amount(payment["amount"])
    name = payment.get("name") or ""
    text, entities = P.render("channel", app.cfg, amount, name, order_id,
                              await todays_payments(app), include_current=False)
    text, entities = P.sanitize_channel_invites(text, entities)
    text, entities, truncated = fit_media_caption(text, entities, preserve=(order_id, P.fmt_inr(amount)))
    await _update(app, payment, post_chat_id=int(target), post_status="posting",
                  post_attempts=attempts, next_post_retry=None,
                  channel_caption_truncated=bool(truncated))
    marker = app.expect_automatic(target, [text])
    result, last_error = None, None
    for attempt in entity_fallbacks(entities):
        try:
            result = await app.client.send_file(target, file=source_message.media, caption=text,
                                                formatting_entities=attempt or None)
            break
        except Exception as error:  # noqa: BLE001
            last_error = error
            if not is_entity_error(error):
                break
    if result is None:
        app.cancel_automatic(target, marker)
        existing = await _find_channel_post(app, target, order_id)
        if existing:
            await _update(app, payment, post_message_id=existing, post_status="posted",
                          post_error=None)
            return True
        error_text = f"{type(last_error).__name__}: {last_error}"
        await _update(app, payment, post_status="failed", post_error=error_text,
                      next_post_retry=ts_now() + P.post_retry_delay(attempts))
        ui.warn(f"Payment channel post failed: {error_text}")
        if notify_failure and not payment.get("post_failure_notified"):
            await _update(app, payment, post_failure_notified=True)
            await app.notify(f"Payment-channel delivery failed for {order_id}: {error_text}. "
                             "It stays queued for retry.")
        return False
    app.finish_automatic(target, marker, result)
    ids = sorted(sent_message_ids(result))
    await _update(app, payment, post_message_id=ids[0] if ids else None,
                  post_status="posted" if ids else "untracked",
                  post_error=None if ids else "Telegram returned no message id",
                  post_failure_notified=False)
    if truncated and payment.get("overflow_status") != "posted":
        overflow = P.required_payment_text(app.cfg, amount, name, order_id,
                                           await todays_payments(app), include_current=False)
        try:
            for index, chunk in enumerate(split_plain_text(overflow, limit=3800), 1):
                await app.client.send_message(
                    target, f"Payment details {index} - Order {order_id}\n{chunk}", link_preview=False)
            await _update(app, payment, overflow_status="posted")
        except Exception as error:  # noqa: BLE001
            await _update(app, payment, overflow_status="failed", overflow_error=str(error)[:300])
    return bool(ids)


async def recover_payments(app, *, interrupted_adds: bool) -> int:
    changed = 0
    if interrupted_adds:
        result = await app.db.payments.update_many(
            {"account_id": app.self_id, "status": "pending"},
            {"$set": {"status": "failed", "error": "interrupted before receipt completion"}})
        changed += result.modified_count
    cursor = app.db.payments.find({
        "account_id": app.self_id, "status": "valid", "source_message_id": {"$ne": None},
        "post_status": {"$in": list(RETRYABLE_POST)},
        "$or": [{"next_post_retry": None}, {"next_post_retry": {"$lte": ts_now()}}],
    })
    for payment in await cursor.to_list(200):
        try:
            await post_payment_to_channel(app, payment, notify_failure=False)
        except Exception as error:  # noqa: BLE001
            await _update(app, payment, post_status="failed", post_error=str(error)[:300],
                          next_post_retry=ts_now() + 30)
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def cmd_add(app, event, command_key: str) -> None:
    match = ADD_RE.match((event.raw_text or "").strip())
    amount_raw = match.group(1) if match else None
    name = (match.group(2) or "").strip() if match else ""
    if not amount_raw:
        await app.ack(event, "Usage: reply to the payment screenshot with /add <amount> <name>")
        return
    try:
        amount = P.parse_amount(amount_raw)
    except ValueError:
        await app.ack(event, f"Amount '{amount_raw}' is not a positive number.")
        return
    if command_key:
        existing = await app.db.payments.find_one({"account_id": app.self_id, "command_key": command_key})
        if existing:
            await app.ack(event, f"Payment already recorded - {existing['order_id']} ({existing['status']})")
            return
    reply = await event.get_reply_message()
    if not (reply and reply.media):
        await app.ack(event, "Reply to the payment screenshot with /add <amount> <name> to log it.")
        return

    cfg = app.cfg
    payment = None
    for _ in range(5):
        taken = set(await app.db.payments.distinct("order_id", {"account_id": app.self_id}))
        order_id = P.generate_order_id(cfg.get("order_prefix", "ANI"), int(cfg.get("order_id_length", 6)), taken)
        payment = {
            "account_id": app.self_id, "amount": format(amount, "f"), "name": name,
            "order_id": order_id, "command_key": command_key or None, "ts": ts_now(),
            "created_at": utcnow(), "status": "pending",
            "source_chat_id": int(getattr(reply, "chat_id", None) or event.chat_id),
            "source_message_id": int(reply.id),
            "post_chat_id": cfg.get("payment_channel"),
            "post_status": "queued" if cfg.get("payment_channel") is not None else "not_configured",
        }
        if not command_key:
            payment.pop("command_key")
        try:
            result = await app.db.payments.insert_one(payment)
            payment["_id"] = result.inserted_id
            break
        except DuplicateKeyError as error:
            if "command_key" in str(error):
                existing = await app.db.payments.find_one({"account_id": app.self_id, "command_key": command_key})
                await app.ack(event, f"Payment already recorded - {existing['order_id']}")
                return
            payment = None
    if payment is None:
        await app.ack(event, "Could not allocate a unique order id; try again.")
        return

    today = await todays_payments(app)
    text, entities = P.render("done", cfg, amount, name, payment["order_id"], today, include_current=True)
    if not await deliver_receipt(app, event, text, entities, amount, name, payment["order_id"], today):
        await _update(app, payment, status="failed", error="receipt could not be delivered")
        await app.ack(event, "Could not deliver the payment receipt; payment not recorded.")
        return
    await _update(app, payment, status="valid")
    screenshot_id = await capture_screenshot(app, reply, payment["order_id"])
    if screenshot_id:
        await _update(app, payment, screenshot_id=screenshot_id)
    try:
        await post_payment_to_channel(app, payment, source_message=reply)
    except Exception as error:  # noqa: BLE001
        ui.warn(f"Payment committed, channel delivery needs attention: {type(error).__name__}: {error}")
    count = len(await todays_payments(app))
    print(ui.green("[pay] ") + f"{payment['order_id']} {P.fmt_inr(amount)} {name} (#{count} today)", flush=True)


async def cmd_cancel(app, event) -> None:
    reply = await event.get_reply_message()
    if reply is None:
        await app.ack(event, "Reply to a payment post with /cancel.")
        return
    payment = await app.db.payments.find_one({
        "account_id": app.self_id, "post_chat_id": int(event.chat_id), "post_message_id": int(reply.id)})
    if payment is None:
        await app.ack(event, "That post is not linked to a recorded payment in this channel.")
        return
    if payment.get("status") == "fake":
        await app.ack(event, "Payment is already marked fake.")
        return
    await _update(app, payment, status="cancel_pending",
                  cancel_previous_status=payment.get("status", "valid"))
    text, entities = P.fake_caption(reply.message or "", reply.entities)
    try:
        await app.client.edit_message(await event.get_input_chat(), reply.id, text,
                                      formatting_entities=entities or None)
        applied = True
    except MessageNotModifiedError:
        applied = True
    except Exception as error:  # noqa: BLE001
        current = None
        try:
            current = await app.client.get_messages(await event.get_input_chat(), ids=reply.id)
        except Exception:  # noqa: BLE001
            pass
        applied = bool(current and P.is_fake_text(current.message))
        if not applied:
            await _update(app, payment, cancel_error=f"{type(error).__name__}: {error}"[:300])
            await app.ack(event, f"Payment excluded from stats, but the FAKE marker failed: "
                                 f"{type(error).__name__}. Reply /cancel again to retry.")
            return
    await _update(app, payment, status="fake", canceled_ts=ts_now(), cancel_error=None)
    day = await todays_payments(app)
    try:
        await event.delete()
    except Exception:  # noqa: BLE001
        await app.ack(event, f"Payment marked fake. Today: {P.fmt_inr(P.payment_total(day))} / {len(day)} payment(s).")


async def set_template(app, event, kind: str, command: str, label: str) -> None:
    reply = await event.get_reply_message()
    if reply is not None and (reply.raw_text or ""):
        template, entities = P.prepare_template(reply.raw_text, reply.entities)
        await app.set(**{f"{kind}_template": template, f"{kind}_entities": serialize_entities(entities)})
        await app.ack(event, f"{label} saved from that post - {len(entities)} formatting/emoji entity(ies) stored.")
        return
    match = re.match(rf"^{re.escape(command)}(?:\s+([\s\S]+))?$", event.raw_text or "", re.IGNORECASE)
    template = (match.group(1).strip() if match and match.group(1) else "")
    if not template:
        await app.ack(event, f"Send the text after {command}, or reply to a formatted post with {command}.")
        return
    template, _ = P.prepare_template(template, [])
    await app.set(**{f"{kind}_template": template, f"{kind}_entities": []})
    await app.ack(event, f"{label} saved ({len(template)} chars, plain text).")


async def cmd_setchannel(app, event) -> None:
    match = SETCHANNEL_RE.match((event.raw_text or "").strip())
    argument = (match.group(1) or "").strip() if match else ""
    if argument.casefold() in ("", "here", "this"):
        if not getattr(event, "is_channel", False):
            await app.ack(event, "Run .setchannel inside the target channel, or name it: .setchannel <id|@username>")
            return
        chat_id = event.chat_id
        title = getattr(await event.get_chat(), "title", None) or "this chat"
    else:
        try:
            chat_id, _entity, title = await resolve_chat(app.client, argument)
        except Exception as error:  # noqa: BLE001
            await app.ack(event, f"Couldn't resolve {argument}: {type(error).__name__}: {error}")
            return
    await app.set(payment_channel=int(chat_id))
    result = await app.db.payments.update_many(
        {"account_id": app.self_id, "status": "valid", "source_message_id": {"$ne": None},
         "post_status": {"$ne": "posted"}},
        {"$set": {"next_post_retry": None, "post_failure_notified": False}})
    await app.db.payments.update_many(
        {"account_id": app.self_id, "post_status": "not_configured"},
        {"$set": {"post_status": "queued", "post_error": None}})
    message = f"Payment channel set: {title} ({chat_id})"
    if result.modified_count:
        message += f"\nRe-queued {result.modified_count} pending delivery(s)."
    await app.ack(event, message)
    await recover_payments(app, interrupted_adds=False)


async def cmd_stats(app, event) -> None:
    from decimal import Decimal

    day = await todays_payments(app)
    total = P.payment_total(day)
    rio = total * Decimal(str(app.cfg.get("rio_pct", 55))) / Decimal("100")
    marco = total * Decimal(str(app.cfg.get("marco_pct", 45))) / Decimal("100")
    await app.ack(event, f"Today - {P.fmt_inr(total)} - {len(day)} payments\n"
                         f"Rio {P.fmt_inr(rio)} - Marco {P.fmt_inr(marco)}")


async def cmd_clear(app, event) -> None:
    start, end = ts_day_window(today_ist())
    result = await app.db.payments.update_many(
        {"account_id": app.self_id, "status": "valid", "ts": {"$gte": start, "$lt": end}},
        {"$set": {"status": "cleared", "cleared_ts": ts_now()}})
    await app.ack(event, f"Today cleared - {result.modified_count} payment(s) removed from stats.\n"
                         f"Today - {P.fmt_inr(0)} - 0 payments")


# ---------------------------------------------------------------------------
# .unban
# ---------------------------------------------------------------------------
UNBAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=False)


async def _resolve_unban_target(app, event, argument: str) -> tuple[int, str]:
    value = argument.split()[0] if argument.split() else ""
    if not value:
        reply = await event.get_reply_message()
        sender = getattr(reply, "sender_id", None)
        if sender is None:
            raise ValueError("Usage: .unban <user id|@username> - or reply to the user.")
        return int(sender), str(sender)
    link = re.match(r"^(?:https?://)?t\.me/([A-Za-z][\w]{3,})/?$", value, re.I)
    if link:
        value = f"@{link.group(1)}"
    if value.lstrip("-").isdigit():
        return int(value), value
    try:
        entity = await app.client.get_entity(value)
    except Exception as error:  # noqa: BLE001
        raise ValueError(f"Couldn't resolve {value}: {type(error).__name__}") from error
    if not isinstance(entity, T.User):
        raise ValueError(f"{value} is not a user.")
    return entity.id, f"@{entity.username} ({entity.id})" if entity.username else str(entity.id)


async def _ban_target(app, chat, user_id: int):
    try:
        permissions = await app.client.get_permissions(chat, user_id)
    except FloodWaitError as error:
        await app.sleep(min(int(error.seconds), 60) + 1)
        return None
    except UserNotParticipantError:
        return None
    except (AttributeError, TypeError, ValueError):
        pass
    except Exception:  # noqa: BLE001
        return None
    else:
        return user_id if getattr(permissions, "is_banned", False) else None
    for participant_filter in (ChannelParticipantsKicked(q=""), ChannelParticipantsBanned(q="")):
        try:
            async for user in app.client.iter_participants(chat, filter=participant_filter):
                if user.id == user_id:
                    return user
        except Exception:  # noqa: BLE001
            continue
    return None


async def cmd_unban(app, event) -> None:
    match = UNBAN_RE.match((event.raw_text or "").strip())
    argument = (match.group(1) or "").strip() if match else ""
    try:
        user_id, label = await _resolve_unban_target(app, event, argument)
    except ValueError as error:
        await app.ack(event, str(error))
        return
    chats = []
    async for dialog in app.client.iter_dialogs():
        if getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False):
            chats.append((dialog.entity, dialog.name or "", get_peer_id(dialog.entity)))
    unbanned, failed = [], []
    for entity, name, chat_id in chats:
        member = await _ban_target(app, entity, user_id)
        if member is None:
            continue
        try:
            await app.client(EditBannedRequest(entity, member, UNBAN_RIGHTS))
            unbanned.append(name or str(chat_id))
        except Exception as error:  # noqa: BLE001
            failed.append(f"{name or chat_id}: {type(error).__name__}")
    if not unbanned and not failed:
        lines = [f"No ban found for {label} in {len(chats)} chat(s)."]
    else:
        lines = [f"Unbanned {label} in {len(unbanned)} of {len(chats)} chat(s)."]
        if unbanned:
            lines.append("\n".join(unbanned[:20]) + (f"\n+{len(unbanned) - 20} more" if len(unbanned) > 20 else ""))
    if failed:
        lines.append("Failed:\n" + "\n".join(failed[:10]))
    await app.ack(event, "\n\n".join(lines))
