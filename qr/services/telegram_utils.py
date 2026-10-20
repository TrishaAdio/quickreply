"""Telegram helpers: flood-wait handling, chat resolution, permission checks."""
from __future__ import annotations

import re

from telethon.errors import FloodWaitError
from telethon.tl import types as tl_types
from telethon.utils import get_peer_id

MAX_FLOOD_WAIT = 300


class FloodDeferred(RuntimeError):
    """Telegram asked us to wait longer than we are willing to block."""

    def __init__(self, seconds: int):
        super().__init__(f"Telegram flood wait of {seconds}s")
        self.seconds = int(seconds)


async def flood_safe(app, factory, *, attempts: int = 3, max_wait: int = MAX_FLOOD_WAIT):
    """Run ``await factory()``; sleep through short FloodWaits and retry.

    Waits longer than ``max_wait`` raise :class:`FloodDeferred` so the caller
    can persist a retry time instead of blocking the event loop for hours.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except FloodWaitError as error:
            seconds = int(getattr(error, "seconds", 0) or 0)
            if seconds > max_wait or attempt == attempts:
                raise FloodDeferred(seconds) from error
            await app.sleep(seconds + 1)
    raise RuntimeError("unreachable")


def normalize_chat_ref(value):
    """Accept @username, numeric id, t.me/name, t.me/c/<id> links."""
    value = str(value or "").strip().split()[0] if str(value or "").strip() else ""
    private = re.match(r"^(?:https?://)?t\.me/c/(\d+)(?:/\d+)?/?$", value, re.I)
    public = re.match(r"^(?:https?://)?t\.me/([A-Za-z][\w]{3,})/?$", value, re.I)
    if private:
        return int(f"-100{private.group(1)}")
    if public:
        return f"@{public.group(1)}"
    body = value[1:] if value.startswith("-") else value
    if body.isdigit():
        return int(value)
    return value


async def resolve_chat(client, value):
    """Return ``(chat_id, entity, title)`` for a channel or group reference."""
    ref = normalize_chat_ref(value)
    if ref in ("", None):
        raise ValueError("Name a channel: @username, numeric id or t.me link.")
    entity = await client.get_entity(ref)
    if isinstance(entity, tl_types.User):
        raise ValueError(f"{value} is a user, not a channel or group.")
    chat_id = get_peer_id(entity)
    return chat_id, entity, getattr(entity, "title", None) or str(value)


async def can_post_media(client, entity) -> tuple[bool, str]:
    """Best-effort check that this account may post media in ``entity``.

    Nothing is sent. Broadcast channels need creator/admin with post rights;
    groups need membership without a media restriction.
    """
    try:
        permissions = await client.get_permissions(entity, "me")
    except Exception as error:  # noqa: BLE001
        return False, f"cannot read permissions ({type(error).__name__}: {error})"
    if permissions is None:
        return False, "not a member"
    if getattr(permissions, "is_banned", False):
        return False, "this account is banned/restricted there"
    broadcast = bool(getattr(entity, "broadcast", False))
    if broadcast:
        if getattr(permissions, "is_creator", False):
            return True, "creator"
        if getattr(permissions, "is_admin", False) and getattr(permissions, "post_messages", False):
            return True, "admin with post rights"
        return False, "needs admin rights with 'Post messages'"
    if getattr(permissions, "is_creator", False) or getattr(permissions, "is_admin", False):
        return True, "admin"
    defaults = getattr(entity, "default_banned_rights", None)
    if defaults is not None and (getattr(defaults, "send_media", False)
                                 or getattr(defaults, "send_photos", False)
                                 or getattr(defaults, "send_docs", False)):
        return False, "members may not send media/files there"
    return True, "member"
