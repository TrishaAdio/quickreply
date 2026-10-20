"""Preserved QuickReply behaviour: greeting/away replies, /demo link relay,
the private-chat ``L`` clear-and-block, ``/broadcast`` and status commands."""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.messages import (
    DeleteHistoryRequest,
    GetQuickReplyMessagesRequest,
    SendQuickReplyMessagesRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputUserSelf, MessageMediaWebPage, UserStatusOnline

from qr import BUILD, ui
from qr.config.settings import IST
from qr.services import dm_stats, quick_replies
from qr.services.entities import LINK_RE, swap_link

BROADCAST_RE = re.compile(
    r"^/broadcast\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s+(\d{1,2})\s+([A-Za-z]{3,9})(?:\s+(\d{4}))?\s+(.+?)\s*$",
    re.IGNORECASE,
)
MONTHS = {
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3, "MARCH": 3, "APR": 4, "APRIL": 4,
    "MAY": 5, "JUN": 6, "JUNE": 6, "JUL": 7, "JULY": 7, "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "OCT": 10, "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}


# ---------------------------------------------------------------------------
# presence + greeting
# ---------------------------------------------------------------------------
async def owner_is_online(app) -> bool:
    """Online only on strong evidence (recent manual send or explicit online)."""
    if time.monotonic() < app.state.owner_active_until:
        return True
    try:
        me = await app.client.get_me()
        status = getattr(me, "status", None)
        if isinstance(status, UserStatusOnline):
            return status.expires.timestamp() > time.time()
        return False
    except Exception:  # noqa: BLE001
        return False


async def away_still_allowed(app, generation: int) -> bool:
    if not app.cfg.get("greet_new", True) or generation != app.state.activity_generation:
        return False
    if await owner_is_online(app):
        return False
    return (app.cfg.get("greet_new", True) and generation == app.state.activity_generation
            and time.monotonic() >= app.state.owner_active_until)


async def copy_to(app, peer, src):
    text = src.message or ""
    entities = src.entities or None
    media = getattr(src, "media", None)
    if media and not isinstance(media, MessageMediaWebPage):
        return await app.client.send_file(peer, file=media, caption=text, formatting_entities=entities)
    return await app.client.send_message(peer, text, formatting_entities=entities,
                                         link_preview=isinstance(media, MessageMediaWebPage))


async def send_greeting(app, event, generation: int) -> str:
    """'greeting', 'away', 'suppressed' or 'none'."""
    peer = await event.get_input_sender()
    user_id = event.sender_id
    greeting = app.cfg.get("greeting")
    if greeting:
        try:
            src = await app.client.get_messages(greeting["chat_id"], ids=greeting["message_id"])
        except Exception:  # noqa: BLE001
            src = None
        if src is not None:
            if not await away_still_allowed(app, generation):
                return "suppressed"
            marker = app.expect_automatic(user_id, [src.message or ""])
            try:
                result = await copy_to(app, peer, src)
            except Exception:
                app.cancel_automatic(user_id, marker)
                raise
            app.finish_automatic(user_id, marker, result)
            return "greeting"
    full = await app.client(GetFullUserRequest(InputUserSelf()))
    away = getattr(full.full_user, "business_away_message", None)
    shortcut_id = getattr(away, "shortcut_id", None) if away else None
    if shortcut_id:
        messages = await app.client(GetQuickReplyMessagesRequest(shortcut_id=shortcut_id, hash=0, id=None))
        msgs = list(getattr(messages, "messages", []) or [])
        ids = [m.id for m in msgs]
        if ids:
            if not await away_still_allowed(app, generation):
                return "suppressed"
            marker = app.expect_automatic(user_id, [getattr(m, "message", "") or "" for m in msgs])
            try:
                result = await app.client(SendQuickReplyMessagesRequest(
                    peer=peer, shortcut_id=shortcut_id, id=ids,
                    random_id=[random.randrange(-(2 ** 63), 2 ** 63) for _ in ids]))
            except Exception:
                app.cancel_automatic(user_id, marker)
                raise
            app.finish_automatic(user_id, marker, result)
            return "away"
    return "none"


async def is_new_user(app, event) -> bool:
    try:
        previous = await app.client.get_messages(event.sender_id, limit=2)
        return len(previous) <= 1
    except Exception:  # noqa: BLE001
        return True


async def already_greeted(app, user_id: int) -> bool:
    return await app.db.greeted.find_one({"account_id": app.self_id, "user_id": int(user_id)}) is not None


async def mark_greeted(app, user_id: int) -> None:
    await app.db.greeted.update_one(
        {"account_id": app.self_id, "user_id": int(user_id)},
        {"$setOnInsert": {"account_id": app.self_id, "user_id": int(user_id)}}, upsert=True)


# ---------------------------------------------------------------------------
# /demo link relay
# ---------------------------------------------------------------------------
async def swap_in_greeting(app, new_link: str) -> str:
    greeting = app.cfg.get("greeting")
    if not greeting:
        return "no greeting set"
    try:
        src = await app.client.get_messages(greeting["chat_id"], ids=greeting["message_id"])
    except Exception as error:  # noqa: BLE001
        return f"fetch error: {type(error).__name__}"
    if src is None:
        return "post not found (re-set with /set)"
    swapped = swap_link(src.message or "", src.entities, new_link)
    if swapped is None:
        return "no invite link in the post"
    text, entities = swapped
    try:
        await app.client.edit_message(greeting["chat_id"], greeting["message_id"], text,
                                      formatting_entities=entities or None)
        return "updated"
    except MessageNotModifiedError:
        return "unchanged"
    except Exception as error:  # noqa: BLE001
        return f"edit error: {type(error).__name__}: {error}"


async def handle_link(app, link: str) -> None:
    if link == app.state.last_link:
        return
    app.state.last_link = link
    cfg = app.cfg
    if not cfg.get("relay_link", True):
        print(ui.dim("[link] seen but the demo link is frozen (.config relay_link off)"), flush=True)
        return
    status = await quick_replies.update_link(app.client, cfg.get("shortcut", "demo"), link)
    print(ui.green(f"[/{cfg.get('shortcut', 'demo')}] ") + f"{ui.bold(link)} ({status})", flush=True)
    if cfg.get("swap_greeting"):
        print(ui.green("[greeting] ") + f"{await swap_in_greeting(app, link)}", flush=True)


async def on_incoming(app, event) -> None:
    """Incoming private messages: trusted link relay, then first-contact greeting.

    Incoming messages can never run commands.
    """
    if not event.is_private:
        return
    try:
        await dm_stats.record_dm(app, event)   # .graph analytics (no message text stored)
    except Exception as error:  # noqa: BLE001 - analytics must never break replies
        ui.warn(f"DM tracking failed: {type(error).__name__}")
    sender = event.sender_id
    source = app.cfg.get("link_source")
    match = LINK_RE.search(event.raw_text or "")
    if match and source and sender == source:
        try:
            await handle_link(app, match.group(0))
        except Exception as error:  # noqa: BLE001
            ui.error(f"link update failed: {type(error).__name__}: {error}")
            if "PREMIUM" in str(error).upper():
                ui.warn("Business quick replies require Telegram Premium.")
        return
    if not app.cfg.get("greet_new", True):
        return
    if not sender or sender in (app.self_id, source) or await already_greeted(app, sender):
        return
    if not await is_new_user(app, event):
        await mark_greeted(app, sender)
        return
    generation = app.state.activity_generation
    if await owner_is_online(app):
        return
    try:
        status = await send_greeting(app, event, generation)
        if status in ("greeting", "away"):
            await mark_greeted(app, sender)
            print(ui.green("[greet] ") + f"sent {status} to {sender}", flush=True)
        elif status == "none":
            ui.warn(f"No greeting set (reply to a post with /set). Skipped {sender}.")
    except Exception as error:  # noqa: BLE001
        ui.error(f"greet failed for {sender}: {type(error).__name__}: {error}")


# ---------------------------------------------------------------------------
# L = clear + block
# ---------------------------------------------------------------------------
async def clear_and_block(app, event) -> None:
    peer = await event.get_input_chat()
    errors = []
    try:
        await app.client(DeleteHistoryRequest(peer=peer, max_id=0, just_clear=False, revoke=True))
    except Exception as error:  # noqa: BLE001
        errors.append(f"clear failed ({type(error).__name__})")
    try:
        await app.client(BlockRequest(id=peer))
    except Exception as error:  # noqa: BLE001
        errors.append(f"block failed ({type(error).__name__})")
    if errors:
        raise RuntimeError("; ".join(errors))
    print(ui.green("[blocked] ") + f"cleared private chat with {event.chat_id}", flush=True)


# ---------------------------------------------------------------------------
# /broadcast
# ---------------------------------------------------------------------------
def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_broadcast(text: str, command_date: datetime):
    match = BROADCAST_RE.match(text.strip())
    if not match:
        raise ValueError("Format: /broadcast 9:30 AM 18 JUL THANKS FOR\n"
                         "Optional year: /broadcast 9:30 AM 18 JUL 2026 THANKS FOR")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError("Invalid time. Use 12-hour IST time, for example 9:30 AM.")
    month = MONTHS.get(match.group(5).upper())
    if month is None:
        raise ValueError(f"Invalid month: {match.group(5)}")
    end_utc = _as_utc(command_date)
    end_ist = end_utc.astimezone(IST)
    year = int(match.group(6)) if match.group(6) else end_ist.year
    hour_24 = hour % 12 + (12 if match.group(3).upper() == "PM" else 0)
    try:
        start_ist = datetime(year, month, int(match.group(4)), hour_24, minute, tzinfo=IST)
    except ValueError as error:
        raise ValueError(f"Invalid date: {error}") from error
    if start_ist > end_ist:
        raise ValueError("Start time is in the future (IST). Add the intended year if needed.")
    return start_ist.astimezone(timezone.utc), end_utc, match.group(7).strip()


async def find_broadcast_targets(app, start_utc, end_utc, keyword: str) -> dict:
    targets = {}
    needle = keyword.casefold()
    async for message in app.client.iter_messages(None, search=keyword,
                                                  offset_date=end_utc + timedelta(seconds=1)):
        date = getattr(message, "date", None)
        if date is None:
            continue
        date = _as_utc(date)
        if date < start_utc:
            break
        if date > end_utc or not getattr(message, "out", False):
            continue
        if getattr(message, "sender_id", None) != app.self_id:
            continue
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None or chat_id == app.self_id or chat_id in targets:
            continue
        if needle not in (getattr(message, "raw_text", "") or "").casefold():
            continue
        try:
            peer = await message.get_input_chat() or await app.client.get_input_entity(message.peer_id)
        except Exception:  # noqa: BLE001
            continue
        targets[chat_id] = peer
    return targets


async def run_broadcast(app, event, src, start_utc, end_utc, keyword: str) -> None:
    progress = await event.reply(
        f'Scanning for "{keyword}" from {start_utc.astimezone(IST):%d %b %Y, %I:%M %p} IST to '
        f'{end_utc.astimezone(IST):%d %b %Y, %I:%M %p} IST...')
    try:
        targets = await find_broadcast_targets(app, start_utc, end_utc, keyword)
    except Exception as error:  # noqa: BLE001
        await progress.edit(f"Broadcast search failed: {type(error).__name__}: {error}")
        return
    if not targets:
        await progress.edit(f'No chats found where you sent "{keyword}" in that IST time window.')
        return
    sent, failures = 0, []
    await progress.edit(f"Found {len(targets)} matching chat(s). Broadcasting...")
    for chat_id, peer in targets.items():
        for attempt in (1, 2):
            marker = app.expect_automatic(chat_id, [src.message or ""])
            try:
                result = await copy_to(app, peer, src)
                app.finish_automatic(chat_id, marker, result)
                sent += 1
                break
            except FloodWaitError as error:
                app.cancel_automatic(chat_id, marker)
                if attempt == 2:
                    failures.append((chat_id, "FloodWait"))
                    break
                await app.sleep(int(error.seconds) + 1)
            except Exception as error:  # noqa: BLE001
                app.cancel_automatic(chat_id, marker)
                failures.append((chat_id, type(error).__name__))
                break
        await app.sleep(0.35)
    result = (f"Broadcast complete.\nKeyword: {keyword}\nMatched chats: {len(targets)}\n"
              f"Sent: {sent}\nFailed: {len(failures)}")
    if failures:
        result += "\nFailures: " + ", ".join(f"{c} ({n})" for c, n in failures[:8])
    await progress.edit(result)


# ---------------------------------------------------------------------------
# status / help
# ---------------------------------------------------------------------------
def status_text(app) -> str:
    cfg = app.cfg
    return (f"QuickReply is running ({BUILD}).\n"
            f"Payment channel: {cfg.get('payment_channel') or 'NOT SET'}\n"
            f"Archive channel: {cfg.get('archive_channel') or 'NOT SET'}"
            f" ({'on' if cfg.get('archive_enabled', True) else 'off'})\n"
            f"Posting channel: {cfg.get('publish_channel') or 'NOT SET'}")


HELP_TEXT = (
    "<b>Payment logger</b> (any chat)\n"
    "<code>/add &lt;amount&gt; &lt;name&gt;</code> - reply to the payment screenshot: private receipt, "
    "payment-channel post, and the screenshot joins today's archive\n"
    "<code>/setdone &lt;template&gt;</code> - private receipt (reply to a post to keep formatting)\n"
    "<code>/setchannelpostofpayment &lt;template&gt;</code> - payment-channel caption\n"
    "<code>.setchannel [id|@username]</code> - payment upload channel\n"
    "<code>/stats</code> - <code>/clear</code> - <code>/cancel</code> (reply to a payment post: mark fake)\n"
    "<code>.unban &lt;id|@username&gt;</code> - lift a ban in every group you admin\n"
    "Tokens: {amount} {name} {orderid} {rioshare} {marco} {total} {todaytotal}\n\n"
    "<b>Backup / restore</b> (Saved Messages)\n"
    "<code>.save</code> - back up profile, Business setup and quick replies to MongoDB\n"
    "<code>.backups</code> - list saved backups\n"
    "<code>.setup &lt;account id|backup id&gt;</code> - preview restoring onto THIS account; "
    "then <code>.setup confirm &lt;code&gt;</code>\n\n"
    "<b>Screenshot archives</b> (Saved Messages)\n"
    "<code>.archive</code> status - <code>.archive run</code> seal finished days now - "
    "<code>.setarchive &lt;@channel&gt;</code> (default @ohthatsthepoint)\n"
    "<code>.post</code> reply to a ZIP - <code>.postbatch</code> reply to the first ZIP of a series, "
    "or <code>.postbatch &lt;N&gt;</code> - <code>.postnow [YYYY-MM-DD]</code> publish pending archives\n"
    "<code>.setpostchannel [id|@username]</code> - <code>.postcaption &lt;text&gt;</code> "
    "({date} {pretty_date} {part} {parts} {count})\n\n"
    "<b>DM analytics</b> (Saved Messages)\n"
    "<code>.graph</code> today's DMs + INR per hour (peak hours) as a glow card - "
    "<code>.graph yesterday|YYYY-MM-DD</code> - <code>.graph scan [day]</code> backfill from chat history - "
    "<code>.tgraph</code> test card with synthetic data\n\n"
    "<b>Greeting</b> (Saved Messages)\n"
    "<code>/set</code> reply to a post - <code>/unset</code> - <code>/show</code> - <code>/away on|off|status</code>\n"
    "<b>Broadcast</b> <code>/broadcast 9:30 AM 18 JUL THANKS FOR</code> (reply to a post)\n"
    "<b>Link relay</b> <code>.linksource &lt;@user|id|off&gt;</code>\n"
    "<b>Settings</b> <code>.config</code> - <code>.config &lt;key&gt; &lt;value&gt;</code>\n"
    "<b>Block</b> <code>L</code> in a private chat - clear both sides and block\n"
    "<code>.ping</code> - status"
)
