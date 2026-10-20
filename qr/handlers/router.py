"""Command routing and authorization.

Authorization model (kept from the original QuickReply, made explicit):

* Commands are only read from **outgoing** messages, i.e. typed by the
  logged-in account itself on one of its sessions. Incoming messages from
  anyone else - whatever their username - never run a command.
* Forwarded messages and messages sent via inline bots never run commands.
* Administrative commands (.save .setup .post .postbatch .postnow ...) are
  accepted only in the account's own Saved Messages, so reports and payment
  data never appear in customer chats.
* Slash commands typed in a private chat with a *bot* are left alone, so
  ``/start``, ``/cancel`` or ``/clear`` meant for that bot cannot trigger
  QuickReply actions.
"""
from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from qr import ui
from qr.database.mongo import utcnow
from qr.database.settings_store import EDITABLE
from qr.handlers import core, payments, post, save, setup

ADMIN_COMMANDS = {
    "save", "backups", "setup", "post", "postbatch", "postnow", "archive", "postcaption",
    "config", "linksource", "broadcast", "set", "unset", "show", "away",
}
ANYWHERE_COMMANDS = {
    "ping", "help", "add", "cancel", "setdone", "setchannelpostofpayment", "setchannel",
    "setpostchannel", "setarchive", "unban", "stats", "clear",
}
LONG_RUNNING = {"save", "setup", "post", "postbatch", "postnow", "archive", "broadcast", "unban"}
COMMAND_LEASE = 300


def command_name(text: str) -> str | None:
    text = (text or "").strip()
    if len(text) < 2 or text[0] not in "./":
        return None
    word = text.split(maxsplit=1)[0][1:].lower()
    if "@" in word:  # /cmd@SomeBot is a bot command, never ours
        return None
    return word if word in ADMIN_COMMANDS | ANYWHERE_COMMANDS else None


def is_authorized(app, event) -> bool:
    """True only for messages this account itself typed (not forwarded)."""
    message = getattr(event, "message", event)
    if not getattr(event, "out", False):
        return False
    if getattr(message, "fwd_from", None) is not None or getattr(message, "via_bot_id", None):
        return False
    sender = getattr(event, "sender_id", None)
    # In broadcast channels the post author is the channel itself.
    return sender in (None, app.self_id) or getattr(event, "is_channel", False)


def command_identity(app, event) -> str:
    chat_id = getattr(event, "chat_id", None)
    message_id = getattr(event, "id", None)
    if chat_id is None or message_id is None:
        return ""
    return f"{app.self_id}:{chat_id}:{message_id}"


async def claim_command(app, key: str) -> str | None:
    """Fence one command so duplicate updates / processes run it once."""
    now = utcnow()
    token = uuid.uuid4().hex
    try:
        await app.db.command_claims.insert_one({
            "_id": key, "status": "processing", "token": token,
            "lease_until": now + timedelta(seconds=COMMAND_LEASE), "created_at": now})
        return token
    except DuplicateKeyError:
        taken = await app.db.command_claims.find_one_and_update(
            {"_id": key, "status": "processing", "lease_until": {"$lt": now}},
            {"$set": {"token": token, "lease_until": now + timedelta(seconds=COMMAND_LEASE)}})
        return token if taken else None


async def _renew(app, key: str, token: str) -> None:
    while True:
        await asyncio.sleep(60)
        await app.db.command_claims.update_one(
            {"_id": key, "token": token},
            {"$set": {"lease_until": utcnow() + timedelta(seconds=COMMAND_LEASE)}})


async def complete_command(app, key: str, token: str) -> None:
    await app.db.command_claims.update_one(
        {"_id": key, "token": token}, {"$set": {"status": "completed", "completed_at": utcnow()}})


async def _in_bot_chat(event) -> bool:
    if not getattr(event, "is_private", False):
        return False
    try:
        chat = await event.get_chat()
    except Exception:  # noqa: BLE001
        return False
    return bool(getattr(chat, "bot", False))


async def on_outgoing(app, event) -> None:
    text = (event.raw_text or "").strip()
    name = command_name(text)
    is_l = text == "L"
    if (name or is_l) and not is_authorized(app, event):
        return
    if name and text.startswith("/") and await _in_bot_chat(event):
        name = None  # a bot's own command
    key = command_identity(app, event) if (name or is_l) else ""
    token = None
    renewer = None
    if key:
        token = await claim_command(app, key)
        if token is None:
            return
        if name in LONG_RUNNING:
            renewer = asyncio.create_task(_renew(app, key, token))
    try:
        await dispatch(app, event, name, text, key)
    except Exception as error:  # noqa: BLE001
        ui.error(f"outgoing handler failed: {type(error).__name__}: {error}")
        traceback.print_exc()
        if name or is_l:
            detail = f"{type(error).__name__}: {error}"
            await app.ack(event, "Command failed - " + (detail[:349] + "\u2026" if len(detail) > 350 else detail))
    finally:
        if renewer is not None:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        if key and token:
            try:
                await complete_command(app, key, token)
            except Exception:  # noqa: BLE001
                pass


async def dispatch(app, event, name: str | None, text: str, command_key: str) -> None:
    chat_id = event.chat_id
    if chat_id != app.self_id and await app.consume_automatic(event):
        return
    if name is None and getattr(event, "is_channel", False) and getattr(event.message, "media", None):
        return  # archive/screenshot uploads are not "owner is chatting" activity
    app.mark_owner_active()

    if name is None:
        if (event.is_private and chat_id != app.self_id and text == "L"
                and event.message.media is None and event.message.fwd_from is None
                and not event.message.entities and is_authorized(app, event)):
            try:
                await core.clear_and_block(app, event)
            except Exception as error:  # noqa: BLE001
                ui.error(f"clear/block failed for {chat_id}: {error}")
        return

    if name in ADMIN_COMMANDS and chat_id != app.self_id:
        await app.ack(event, f"Run {text.split()[0]} in your Saved Messages.")
        return

    if name == "ping":
        await app.ack(event, core.status_text(app))
    elif name == "help":
        await app.ack(event, core.HELP_TEXT, parse_mode="html")
    elif name == "add":
        async with app.payment_lock:
            await payments.cmd_add(app, event, command_key)
    elif name == "cancel":
        if not getattr(event, "is_channel", False) and not await event.get_reply_message():
            return  # not ours: /cancel in an ordinary chat
        async with app.payment_lock:
            await payments.cmd_cancel(app, event)
    elif name == "setchannelpostofpayment":
        await payments.set_template(app, event, "channel", "/setchannelpostofpayment", "Channel post caption")
    elif name == "setdone":
        await payments.set_template(app, event, "done", "/setdone", "Private-chat message")
    elif name == "setchannel":
        async with app.payment_lock:
            await payments.cmd_setchannel(app, event)
    elif name == "unban":
        await payments.cmd_unban(app, event)
    elif name == "stats":
        await payments.cmd_stats(app, event)
    elif name == "clear":
        async with app.payment_lock:
            await payments.cmd_clear(app, event)
    elif name == "setpostchannel":
        await post.cmd_setpostchannel(app, event)
    elif name == "setarchive":
        await post.cmd_setarchive(app, event)
    elif name == "save":
        await save.cmd_save(app, event)
    elif name == "backups":
        await save.cmd_backups(app, event)
    elif name == "setup":
        await setup.cmd_setup(app, event)
    elif name == "post":
        await post.cmd_post(app, event)
    elif name == "postbatch":
        await post.cmd_postbatch(app, event)
    elif name == "postnow":
        await post.cmd_postnow(app, event)
    elif name == "archive":
        await post.cmd_archive(app, event)
    elif name == "postcaption":
        await post.cmd_postcaption(app, event)
    elif name == "config":
        await cmd_config(app, event)
    elif name == "linksource":
        await cmd_linksource(app, event)
    else:
        await greeting_commands(app, event, name, text)


async def greeting_commands(app, event, name: str, text: str) -> None:
    low = text.lower()
    if name == "broadcast":
        reply = await event.get_reply_message()
        if reply is None:
            await event.reply("Reply to the message you want to send, then use:\n"
                              "/broadcast 9:30 AM 18 JUL THANKS FOR")
            return
        try:
            start, end, keyword = core.parse_broadcast("/" + text[1:], event.date)
        except ValueError as error:
            await event.reply(str(error))
            return
        if app.broadcast_lock.locked():
            await event.reply("A broadcast is already running. Wait for it to finish.")
            return
        async with app.broadcast_lock:
            await core.run_broadcast(app, event, reply, start, end, keyword)
    elif name == "set":
        reply = await event.get_reply_message()
        if reply is None:
            await event.reply("Reply to a post with /set to use it as the greeting.")
            return
        await app.set(greeting={"chat_id": int(event.chat_id), "message_id": int(reply.id)})
        note = ""
        if app.cfg.get("swap_greeting"):
            link = app.state.last_link
            if link:
                note = f"\nLink -> {link} ({await core.swap_in_greeting(app, link)})"
        await event.reply("Greeting saved. New users who DM you first get this." + note)
    elif name == "unset":
        had = bool(app.cfg.get("greeting"))
        await app.set(greeting=None)
        await event.reply("Greeting cleared." if had else "No greeting was set.")
    elif name == "away":
        arg = low.split()[1] if len(low.split()) > 1 else "status"
        if arg in ("on", "off"):
            await app.set(greet_new=arg == "on")
            app.state.activity_generation += 1
            await event.reply(f"Away messages {'enabled' if arg == 'on' else 'disabled'}.")
        else:
            await event.reply(f"Away messages are {'enabled' if app.cfg.get('greet_new', True) else 'disabled'}.")
    elif name == "show":
        greeting = "set" if app.cfg.get("greeting") else "not set"
        away = "enabled" if app.cfg.get("greet_new", True) else "disabled"
        await event.reply(f"Greeting is {greeting}. Away messages are {away}.")


async def cmd_config(app, event) -> None:
    parts = (event.raw_text or "").split(maxsplit=2)
    if len(parts) == 1:
        lines = ["Settings (.config <key> <value>):"]
        lines += [f"{key} = {app.cfg.get(key)}" for key in sorted(EDITABLE)]
        await app.ack(event, "\n".join(lines))
        return
    key = parts[1].lower()
    if key not in EDITABLE or len(parts) < 3:
        await app.ack(event, "Usage: .config <key> <value>\nKeys: " + ", ".join(sorted(EDITABLE)))
        return
    try:
        value = EDITABLE[key](parts[2])
    except (ValueError, TypeError) as error:
        await app.ack(event, f"Invalid value for {key}: {error}")
        return
    await app.set(**{key: value})
    await app.ack(event, f"{key} = {value}")


async def cmd_linksource(app, event) -> None:
    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) == 1:
        await app.ack(event, f"Link source: {app.cfg.get('link_source') or 'off'}\n"
                             "Usage: .linksource <@user|id|off> - only this account's invite "
                             f"links update /{app.cfg.get('shortcut', 'demo')}.")
        return
    value = parts[1].strip()
    if value.lower() == "off":
        await app.set(link_source=None)
        await app.ack(event, "Link relay source cleared; incoming links are ignored.")
        return
    try:
        entity = await app.client.get_entity(int(value) if value.lstrip("-").isdigit() else value)
    except Exception as error:  # noqa: BLE001
        await app.ack(event, f"Couldn't resolve {value}: {type(error).__name__}")
        return
    await app.set(link_source=int(entity.id))
    await app.ack(event, f"Link source set to {entity.id}. Only its invite links update "
                         f"/{app.cfg.get('shortcut', 'demo')}.")
