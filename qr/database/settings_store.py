"""Per-account runtime settings stored in MongoDB (``settings`` collection).

These replace the old ChannelGuard ``.env`` knobs and ``data/*.json`` files.
They are changed with chat commands (``.setchannel``, ``.config`` ...), never
asked for by the installer.
"""
from __future__ import annotations

import copy

from qr.config.settings import DEFAULT_ARCHIVE_CHANNEL
from qr.database.mongo import utcnow

DEFAULT_DONE = "Payment of {amount} received - thank you, {name}!"
DEFAULT_CHANNEL = (
    "New payment received\n"
    "Order {orderid}\n"
    "{name} paid {amount}\n\n"
    "Rio {rioshare} - Marco {marco}\n"
    "Payments today: {total}\n"
    "Collected today: {todaytotal}"
)
DEFAULT_POST_CAPTION = "Payment screenshots - {date}\nPart {part}/{parts} - {count} screenshot(s)"

DEFAULTS: dict = {
    # greeting / away replies
    "greet_new": True,
    "online_minutes": 2.0,
    "greeting": None,             # {"chat_id", "message_id"} in Saved Messages
    # business /demo link relay
    "shortcut": "demo",
    "link_source": None,          # numeric user id trusted to send the link
    "relay_link": True,
    "swap_greeting": False,
    # payment logger
    "payment_channel": None,      # numeric chat id for /add proof posts
    "done_template": DEFAULT_DONE,
    "done_entities": [],
    "channel_template": DEFAULT_CHANNEL,
    "channel_entities": [],
    "rio_pct": 55.0,
    "marco_pct": 45.0,
    "share_base": "today",        # today | transaction
    "order_prefix": "ANI",
    "order_id_length": 6,
    # screenshot archives + publishing
    "archive_channel": DEFAULT_ARCHIVE_CHANNEL,
    "archive_enabled": True,
    # DM traffic analytics (.graph)
    "dm_tracking": True,
    "dm_tracking_since": None,    # set automatically on first start
    "publish_channel": None,      # set explicitly with .setpostchannel
    "post_caption": DEFAULT_POST_CAPTION,
}

# `.config <key> <value>` - user-editable keys and their parsers.
def _bool(value: str) -> bool:
    lowered = str(value).strip().lower()
    if lowered in ("1", "on", "yes", "true"):
        return True
    if lowered in ("0", "off", "no", "false"):
        return False
    raise ValueError("expected on/off")


def _pct(value: str) -> float:
    number = float(str(value).strip().rstrip("%"))
    if not 0 <= number <= 100:
        raise ValueError("expected a percentage between 0 and 100")
    return number


def _share_base(value: str) -> str:
    lowered = str(value).strip().lower()
    if lowered not in ("today", "transaction"):
        raise ValueError("expected 'today' or 'transaction'")
    return lowered


def _prefix(value: str) -> str:
    value = str(value).strip().upper()
    if not value.isalnum() or len(value) > 8:
        raise ValueError("expected up to 8 letters/digits")
    return value


def _id_length(value: str) -> int:
    number = int(value)
    if not 3 <= number <= 16:
        raise ValueError("expected a number between 3 and 16")
    return number


def _minutes(value: str) -> float:
    number = float(value)
    if not 0 <= number <= 1440:
        raise ValueError("expected minutes between 0 and 1440")
    return number


def _shortcut(value: str) -> str:
    value = str(value).strip().lstrip("/")
    if not value or len(value) > 32 or any(ch.isspace() for ch in value):
        raise ValueError("expected a quick reply name without spaces")
    return value


EDITABLE = {
    "rio_pct": _pct,
    "marco_pct": _pct,
    "share_base": _share_base,
    "order_prefix": _prefix,
    "order_id_length": _id_length,
    "online_minutes": _minutes,
    "shortcut": _shortcut,
    "relay_link": _bool,
    "swap_greeting": _bool,
    "archive_enabled": _bool,
    "dm_tracking": _bool,
}


class SettingsStore:
    """Cached per-account settings. Reads hit memory; writes go to MongoDB first."""

    def __init__(self, db):
        self.db = db
        self._cache: dict[int, dict] = {}

    async def load(self, account_id: int) -> dict:
        doc = await self.db.settings.find_one({"_id": int(account_id)}) or {}
        merged = copy.deepcopy(DEFAULTS)
        merged.update({k: v for k, v in doc.items() if k != "_id"})
        self._cache[int(account_id)] = merged
        return merged

    def cached(self, account_id: int) -> dict:
        return self._cache.get(int(account_id)) or copy.deepcopy(DEFAULTS)

    async def get(self, account_id: int) -> dict:
        if int(account_id) not in self._cache:
            return await self.load(account_id)
        return self._cache[int(account_id)]

    async def update(self, account_id: int, **values) -> dict:
        values = dict(values)
        values["updated_at"] = utcnow()
        await self.db.settings.update_one(
            {"_id": int(account_id)}, {"$set": values}, upsert=True)
        current = await self.get(account_id)
        current.update(values)
        return current

    async def unset(self, account_id: int, *keys: str) -> dict:
        await self.db.settings.update_one(
            {"_id": int(account_id)},
            {"$set": {key: copy.deepcopy(DEFAULTS.get(key)) for key in keys}},
            upsert=True,
        )
        current = await self.get(account_id)
        for key in keys:
            current[key] = copy.deepcopy(DEFAULTS.get(key))
        return current
