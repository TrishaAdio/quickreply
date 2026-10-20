"""Application context shared by handlers and services."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from telethon.errors import MessageNotModifiedError

from qr import ui
from qr.database.mongo import utcnow
from qr.database.settings_store import SettingsStore


@dataclass
class RuntimeState:
    owner_active_until: float = 0.0
    activity_generation: int = 0
    last_link: str | None = None
    automatic_outgoing: dict = field(default_factory=dict)
    automatic_message_ids: set = field(default_factory=set)


class App:
    """Everything a handler needs: Telegram client, MongoDB, settings, locks."""

    def __init__(self, client, db, *, self_id: int = 0, me=None):
        self.client = client
        self.db = db
        self.settings = SettingsStore(db)
        self.self_id = int(self_id or 0)
        self.me = me
        self.state = RuntimeState()
        self.worker_id = uuid.uuid4().hex
        self.payment_lock = asyncio.Lock()
        self.broadcast_lock = asyncio.Lock()
        self.archive_lock = asyncio.Lock()
        self.post_lock = asyncio.Lock()
        self.restore_lock = asyncio.Lock()
        self.sleep = asyncio.sleep  # patched in tests to skip real waits

    # ---- settings -------------------------------------------------------
    @property
    def cfg(self) -> dict:
        return self.settings.cached(self.self_id)

    async def load_settings(self) -> dict:
        return await self.settings.load(self.self_id)

    async def set(self, **values) -> dict:
        return await self.settings.update(self.self_id, **values)

    # ---- account registry ---------------------------------------------
    async def register_account(self) -> None:
        me = self.me
        await self.db.accounts.update_one(
            {"_id": self.self_id},
            {"$set": {
                "first_name": getattr(me, "first_name", "") or "",
                "last_name": getattr(me, "last_name", "") or "",
                "username": getattr(me, "username", "") or "",
                "premium": bool(getattr(me, "premium", False)),
                "last_seen_at": utcnow(),
            }, "$setOnInsert": {"registered_at": utcnow()}},
            upsert=True,
        )

    # ---- presence / automatic-send markers ------------------------------
    def mark_owner_active(self) -> None:
        minutes = float(self.cfg.get("online_minutes", 2.0) or 0)
        self.state.owner_active_until = time.monotonic() + minutes * 60
        self.state.activity_generation += 1

    def expect_automatic(self, peer_id: int, texts: list[str]) -> dict:
        marker = {"until": time.monotonic() + 30, "texts": set(texts),
                  "ready": asyncio.Event()}
        self.state.automatic_outgoing[peer_id] = marker
        return marker

    def finish_automatic(self, peer_id: int, marker: dict, result) -> None:
        ids = sent_message_ids(result)
        self.state.automatic_message_ids.update((peer_id, i) for i in ids)
        marker["ready"].set()
        if self.state.automatic_outgoing.get(peer_id) is marker:
            self.state.automatic_outgoing.pop(peer_id, None)

    def cancel_automatic(self, peer_id: int, marker: dict) -> None:
        marker["ready"].set()
        if self.state.automatic_outgoing.get(peer_id) is marker:
            self.state.automatic_outgoing.pop(peer_id, None)

    async def consume_automatic(self, event) -> bool:
        """True when an outgoing update was produced by QuickReply itself."""
        peer_id = event.chat_id
        key = (peer_id, event.id)
        if key in self.state.automatic_message_ids:
            self.state.automatic_message_ids.discard(key)
            return True
        marker = self.state.automatic_outgoing.get(peer_id)
        if marker is None or (event.raw_text or "") not in marker["texts"]:
            return False
        if marker["until"] <= time.monotonic():
            self.cancel_automatic(peer_id, marker)
            return False
        try:
            await asyncio.wait_for(marker["ready"].wait(), timeout=2)
        except asyncio.TimeoutError:
            return False
        if key not in self.state.automatic_message_ids:
            return False
        self.state.automatic_message_ids.discard(key)
        return True

    # ---- replies --------------------------------------------------------
    async def ack(self, event, text: str, **kwargs) -> bool:
        """Edit the command message, else send one reply. Reports delivery."""
        try:
            await event.edit(text, **kwargs)
            return True
        except MessageNotModifiedError:
            return True
        except Exception:  # noqa: BLE001
            try:
                await event.respond(text, **kwargs)
                return True
            except Exception:  # noqa: BLE001
                return False

    async def notify(self, text: str) -> None:
        """Private note to the account's own Saved Messages."""
        try:
            await self.client.send_message("me", text)
        except Exception as error:  # noqa: BLE001
            ui.warn(f"Couldn't write to Saved Messages: {type(error).__name__}")


def sent_message_ids(result) -> set[int]:
    """Message ids from a Telethon Message / list / Updates result."""
    ids: set[int] = set()
    if isinstance(result, (list, tuple)):
        for item in result:
            ids.update(sent_message_ids(item))
        return ids
    message_id = getattr(result, "id", None)
    if isinstance(message_id, int):
        ids.add(message_id)
    for update in getattr(result, "updates", []) or []:
        message = getattr(update, "message", None)
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            ids.add(message_id)
    return ids
