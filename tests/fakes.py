"""In-memory Telegram fakes (no network). They model just enough state for
backup -> restore -> verify round trips and posting/archiving workflows."""
from __future__ import annotations

import io
import itertools
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.errors import FloodWaitError
from telethon.tl import types as T
from telethon.tl.types import users as TU

_ids = itertools.count(1000)
NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)


def new_id() -> int:
    return next(_ids)


def jpeg(tag: str | bytes, size: int = 64) -> bytes:
    tag = tag.encode() if isinstance(tag, str) else tag
    return b"\xff\xd8\xff\xe0" + tag + b"\x00" * max(0, size - len(tag)) + b"\xff\xd9"


def png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag.encode() + b"\x00" * 32


class InputFileBytes(T.InputFile):
    """InputFile that remembers the uploaded bytes (fake upload)."""

    def __init__(self, data: bytes, name: str):
        super().__init__(id=new_id(), parts=1, name=name, md5_checksum="")
        self.data = data


class FakeAccount:
    def __init__(self, user_id: int, first="Alice", last="Smith", username="alice", premium=True):
        self.media: dict[int, bytes] = {}
        self.video: dict[int, bytes] = {}
        self.user = T.User(id=user_id, is_self=True, access_hash=1, first_name=first, last_name=last,
                           username=username, premium=premium, usernames=None)
        self.full = T.UserFull(id=user_id, settings=T.PeerSettings(),
                               notify_settings=T.PeerNotifySettings(), common_chats_count=0, about="")
        self.photos: list[T.Photo] = []   # newest first
        self.shortcuts: dict[str, dict] = {}   # name -> {"id", "messages"}
        self.shortcut_order: list[str] = []
        self.chat_links: list = []
        self.saved: list = []   # Saved Messages

    # ---- builders ------------------------------------------------------
    def make_photo(self, data: bytes, video: bytes | None = None) -> T.Photo:
        pid = new_id()
        video_sizes = [T.VideoSize(type="u", w=640, h=640, size=len(video), video_start_ts=0.5)] if video else None
        photo = T.Photo(id=pid, access_hash=pid, file_reference=b"ref", date=NOW,
                        sizes=[T.PhotoSize(type="y", w=640, h=640, size=len(data))], dc_id=2,
                        video_sizes=video_sizes)
        self.media[pid] = data
        if video:
            self.video[pid] = video
        return photo

    def add_profile_photo(self, data: bytes, video: bytes | None = None) -> T.Photo:
        photo = self.make_photo(data, video)
        self.photos.insert(0, photo)
        self.full.profile_photo = photo
        return photo

    def make_document(self, data: bytes, mime="application/pdf", name="file.pdf") -> T.Document:
        did = new_id()
        self.media[did] = data
        return T.Document(id=did, access_hash=did, file_reference=b"r", date=NOW, mime_type=mime,
                          size=len(data), dc_id=2,
                          attributes=[T.DocumentAttributeFilename(file_name=name)])

    def message(self, text="", entities=None, media=None, grouped_id=None, reply_markup=None, mid=None):
        return T.Message(id=mid or new_id(), peer_id=T.PeerUser(self.user.id), date=NOW, message=text,
                         out=True, entities=entities, media=media, grouped_id=grouped_id,
                         reply_markup=reply_markup)

    def add_shortcut(self, name: str, messages: list) -> int:
        sid = new_id()
        self.shortcuts[name] = {"id": sid, "messages": list(messages)}
        self.shortcut_order.append(name)
        return sid

    def shortcut_by_id(self, sid):
        return next((n, s) for n, s in self.shortcuts.items() if s["id"] == sid)


class FakeChannel:
    def __init__(self, chat_id=-1001234567890, title="Archive", username="ohthatsthepoint",
                 can_post=True):
        raw_id = int(str(chat_id).replace("-100", "", 1))
        self.entity = T.Channel(id=raw_id, title=title, photo=T.ChatPhotoEmpty(), date=NOW,
                                broadcast=True, creator=can_post, access_hash=5, username=username)
        self.chat_id = chat_id
        self.can_post = can_post
        self.messages: list = []   # oldest first


class FakeClient:
    def __init__(self, account: FakeAccount, channels: list[FakeChannel] | None = None):
        self.account = account
        self.channels = {c.chat_id: c for c in channels or []}
        self.requests: list = []
        self.sent: list = []          # (chat_id, kwargs)
        self.fail_requests: dict[str, Exception] = {}
        self.send_failures: list = []  # exceptions to raise on successive send_file calls (None = ok)
        self.lose_reply_once = False   # send succeeds but raises afterwards (crash simulation)
        self.users: dict = {}
        self.peer_messages: dict = {}  # chat id -> messages of ordinary chats
        self.user_dialogs: list = []   # private-chat dialogs, newest first

    # ---- helpers -------------------------------------------------------
    def channel(self, ref) -> FakeChannel | None:
        for channel in self.channels.values():
            if ref in (channel.chat_id, channel.entity, channel.entity.id) or (
                    isinstance(ref, str) and ref.lstrip("@").lower() == (channel.entity.username or "").lower()):
                return channel
        return None

    async def get_me(self):
        return self.account.user

    async def get_entity(self, ref):
        channel = self.channel(ref)
        if channel:
            return channel.entity
        if ref in self.users:
            return self.users[ref]
        raise ValueError(f"Cannot find any entity corresponding to {ref!r}")

    async def get_input_entity(self, ref):
        if isinstance(ref, T.PeerChannel):
            channel = self.channel(int(f"-100{ref.channel_id}"))
            if channel is None:
                raise ValueError("channel not accessible")
            return T.InputPeerChannel(channel.entity.id, 5)
        if ref in self.users:
            return T.InputPeerUser(ref, 1)
        raise ValueError(f"Cannot find {ref!r}")

    async def get_permissions(self, entity, user=None):
        channel = self.channel(entity)
        return SimpleNamespace(is_creator=channel.can_post, is_admin=channel.can_post,
                               post_messages=channel.can_post, is_banned=False)

    async def upload_file(self, data, file_name="file"):
        return InputFileBytes(data, file_name)

    async def download_media(self, obj, file=bytes, thumb=None):
        media = getattr(obj, "media", obj)
        if isinstance(media, T.MessageMediaPhoto):
            media = media.photo
        if isinstance(media, T.MessageMediaDocument):
            media = media.document
        if isinstance(thumb, T.VideoSize):
            return self.account.video.get(media.id)
        if isinstance(media, (T.Photo, T.Document)):
            for channel in self.channels.values():
                if media.id in getattr(channel, "media", {}):
                    return channel.media[media.id]
            return self.account.media.get(media.id)
        return None

    # ---- messages ------------------------------------------------------
    def _store_uploaded(self, file, force_document=False, name=None):
        if isinstance(file, (str, bytes)) and not isinstance(file, bytes):
            data = open(file, "rb").read()
            name = name or file.rsplit("/", 1)[-1]
        elif isinstance(file, io.BytesIO):
            data = file.getvalue()
            name = getattr(file, "name", "file")
        elif isinstance(file, bytes):
            data = file
        else:  # MessageMedia object being re-sent
            return file
        if force_document or not (name or "").lower().endswith((".jpg", ".jpeg", ".png")):
            doc = self.account.make_document(data, "application/zip" if (name or "").endswith(".zip")
                                             else "application/octet-stream", name or "file")
            return T.MessageMediaDocument(document=doc)
        return T.MessageMediaPhoto(photo=self.account.make_photo(data))

    async def send_file(self, entity, file=None, caption=None, force_document=False,
                        formatting_entities=None, **kwargs):
        if self.send_failures:
            failure = self.send_failures.pop(0)
            if failure is not None:
                raise failure
        channel = self.channel(entity)
        files = file if isinstance(file, list) else [file]
        captions = caption if isinstance(caption, list) else [caption or ""] + [""] * (len(files) - 1)
        grouped = new_id() if len(files) > 1 else None
        out = []
        for index, item in enumerate(files):
            media = self._store_uploaded(item, force_document)
            msg = T.Message(id=new_id(), peer_id=T.PeerChannel(channel.entity.id) if channel else T.PeerUser(1),
                            date=datetime.now(timezone.utc), message=captions[index] or "", out=True,
                            media=media, grouped_id=grouped, entities=formatting_entities)
            if channel:
                channel.messages.append(msg)
            out.append(msg)
        self.sent.append((getattr(channel, "chat_id", entity), {"count": len(files), "caption": captions[0],
                                                                "force_document": force_document}))
        if self.lose_reply_once:
            self.lose_reply_once = False
            raise ConnectionError("connection lost after upload")
        return out if isinstance(file, list) else out[0]

    async def send_message(self, entity, text="", formatting_entities=None, link_preview=False, **kw):
        msg = self.account.message(text, entities=formatting_entities)
        if entity == "me":
            self.account.saved.append(msg)
        self.sent.append((entity, {"text": text}))
        return msg

    async def get_messages(self, entity, ids=None, limit=None):
        channel = self.channel(entity)
        pool = channel.messages if channel else self.peer_messages.get(entity, self.account.saved)
        if ids is not None:
            return next((m for m in pool if m.id == ids), None)
        return list(reversed(pool))[:limit]

    async def iter_messages(self, entity, search=None, limit=None, min_id=0, max_id=0,
                            reverse=False, offset_date=None, **kwargs):
        channel = self.channel(entity)
        if channel:
            source = channel.messages
        else:
            source = self.peer_messages.get(getattr(entity, "id", entity), self.account.saved)
        pool = list(reversed(source))
        if offset_date is not None:
            pool = [m for m in pool if m.date < offset_date]
        pool = [m for m in pool if m.id > (min_id or 0) and (not max_id or m.id < max_id)]
        if reverse:
            pool.reverse()
        count = 0
        for message in pool:
            if search:
                names = []
                document = getattr(getattr(message, "media", None), "document", None)
                if document is not None:
                    names = [getattr(a, "file_name", "") for a in document.attributes]
                if search not in (message.message or "") and search not in names:
                    continue
            yield message
            count += 1
            if limit and count >= limit:
                break

    async def edit_message(self, entity, message_id, text, formatting_entities=None, **kwargs):
        channel = self.channel(entity)
        pool = channel.messages if channel else self.account.saved
        for message in pool:
            if message.id == message_id:
                message.message = text
                message.entities = formatting_entities
                return message
        raise ValueError("MESSAGE_ID_INVALID")

    async def iter_dialogs(self):
        for dialog in self.user_dialogs:
            yield dialog
        for channel in self.channels.values():
            yield SimpleNamespace(is_group=False, is_channel=True, entity=channel.entity,
                                  name=channel.entity.title)

    def _DeleteHistoryRequest(self, request):
        return T.messages.AffectedHistory(pts=1, pts_count=1, offset=0)

    def _BlockRequest(self, request):
        return True

    def _EditBannedRequest(self, request):
        return T.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)

    # ---- raw requests --------------------------------------------------
    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(request)
        if name in self.fail_requests:
            raise self.fail_requests[name]
        handler = getattr(self, "_" + name, None)
        if handler is None:
            raise NotImplementedError(name)
        return handler(request)

    def _GetFullUserRequest(self, request):
        return TU.UserFull(full_user=self.account.full, chats=[], users=[self.account.user])

    def _GetUserPhotosRequest(self, request):
        photos = self.account.photos[: request.limit]
        return T.photos.PhotosSlice(count=len(self.account.photos), photos=photos, users=[])

    def _GetQuickRepliesRequest(self, request):
        replies = [T.QuickReply(shortcut_id=self.account.shortcuts[n]["id"], shortcut=n,
                                top_message=0, count=len(self.account.shortcuts[n]["messages"]))
                   for n in self.account.shortcut_order]
        return T.messages.QuickReplies(quick_replies=replies, messages=[], chats=[], users=[])

    def _GetQuickReplyMessagesRequest(self, request):
        _name, shortcut = self.account.shortcut_by_id(request.shortcut_id)
        return T.messages.Messages(messages=list(shortcut["messages"]), chats=[], users=[])

    def _GetBusinessChatLinksRequest(self, request):
        return T.account.BusinessChatLinks(links=list(self.account.chat_links), chats=[], users=[])

    def _UpdateProfileRequest(self, request):
        if request.about is not None and len(request.about) > 70 and not self.account.user.premium:
            raise ValueError("ABOUT_TOO_LONG")
        if request.first_name is not None:
            self.account.user.first_name = request.first_name
        if request.last_name is not None:
            self.account.user.last_name = request.last_name
        if request.about is not None:
            self.account.full.about = request.about
        return self.account.user

    def _UpdateUsernameRequest(self, request):
        if request.username in getattr(self, "occupied", set()):
            raise ValueError("USERNAME_OCCUPIED")
        self.account.user.username = request.username
        return self.account.user

    def _UpdateBirthdayRequest(self, request):
        self.account.full.birthday = request.birthday
        return True

    def _UpdateColorRequest(self, request):
        color = T.PeerColor(color=request.color, background_emoji_id=request.background_emoji_id)
        if request.for_profile:
            self.account.user.profile_color = color
        else:
            self.account.user.color = color
        return True

    def _UpdateEmojiStatusRequest(self, request):
        self.account.user.emoji_status = request.emoji_status
        return True

    def _UploadProfilePhotoRequest(self, request):
        photo = self.account.make_photo(request.file.data if request.file else b"",
                                        request.video.data if request.video else None)
        if request.video and not request.file:
            self.account.media[photo.id] = request.video.data
        if request.fallback:
            self.account.full.fallback_photo = photo
        else:
            self.account.photos.insert(0, photo)
            self.account.full.profile_photo = photo
        return T.photos.Photo(photo=photo, users=[])

    def _media_from_input(self, media):
        if isinstance(media, T.InputMediaUploadedPhoto):
            return T.MessageMediaPhoto(photo=self.account.make_photo(media.file.data))
        if isinstance(media, T.InputMediaUploadedDocument):
            doc = self.account.make_document(media.file.data, media.mime_type, "file")
            doc.attributes = media.attributes
            return T.MessageMediaDocument(document=doc)
        if isinstance(media, T.InputMediaPhoto):
            return T.MessageMediaPhoto(photo=T.Photo(id=media.id.id, access_hash=1, file_reference=b"",
                                                     date=NOW, sizes=[], dc_id=2))
        if isinstance(media, T.InputMediaGeoPoint):
            return T.MessageMediaGeo(geo=T.GeoPoint(long=media.geo_point.long, lat=media.geo_point.lat,
                                                    access_hash=0))
        raise NotImplementedError(type(media).__name__)

    def _append(self, shortcut, message):
        if shortcut.shortcut not in self.account.shortcuts:
            self.account.add_shortcut(shortcut.shortcut, [])
        self.account.shortcuts[shortcut.shortcut]["messages"].append(message)

    def _SendMessageRequest(self, request):
        if request.quick_reply_shortcut is not None:
            for entity in request.entities or []:
                if isinstance(entity, T.MessageEntityCustomEmoji) and not self.account.user.premium:
                    raise ValueError("ENTITY_CUSTOM_EMOJI_INVALID")
            self._append(request.quick_reply_shortcut,
                         self.account.message(request.message, entities=request.entities))
        return T.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)

    def _SendMediaRequest(self, request):
        self._append(request.quick_reply_shortcut,
                     self.account.message(request.message, entities=request.entities,
                                          media=self._media_from_input(request.media)))
        return T.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)

    def _UploadMediaRequest(self, request):
        return self._media_from_input(request.media)

    def _SendMultiMediaRequest(self, request):
        grouped = new_id()
        for single in request.multi_media:
            self._append(request.quick_reply_shortcut, self.account.message(
                single.message, entities=single.entities, media=self._media_from_input(single.media),
                grouped_id=grouped))
        return T.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)

    def _DeleteQuickReplyShortcutRequest(self, request):
        name, _ = self.account.shortcut_by_id(request.shortcut_id)
        del self.account.shortcuts[name]
        self.account.shortcut_order.remove(name)
        return True

    def _ReorderQuickRepliesRequest(self, request):
        by_id = {s["id"]: n for n, s in self.account.shortcuts.items()}
        self.account.shortcut_order = [by_id[i] for i in request.order]
        return True

    def _EditMessageRequest(self, request):
        _name, shortcut = self.account.shortcut_by_id(request.quick_reply_shortcut_id)
        for message in shortcut["messages"]:
            if message.id == request.id:
                message.message = request.message
                message.entities = request.entities
        return T.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)

    def _UpdateBusinessWorkHoursRequest(self, request):
        self.account.full.business_work_hours = request.business_work_hours
        return True

    def _UpdateBusinessLocationRequest(self, request):
        geo = request.geo_point
        self.account.full.business_location = T.BusinessLocation(
            address=request.address, geo_point=T.GeoPoint(long=geo.long, lat=geo.lat, access_hash=0) if geo else None)
        return True

    def _UpdateBusinessIntroRequest(self, request):
        intro = request.intro
        self.account.full.business_intro = T.BusinessIntro(title=intro.title, description=intro.description)
        return True

    def _recipients(self, value):
        return T.BusinessRecipients(existing_chats=value.existing_chats, new_chats=value.new_chats,
                                    contacts=value.contacts, non_contacts=value.non_contacts,
                                    exclude_selected=value.exclude_selected,
                                    users=[u.user_id for u in value.users or []] or None)

    def _UpdateBusinessGreetingMessageRequest(self, request):
        m = request.message
        self.account.full.business_greeting_message = T.BusinessGreetingMessage(
            shortcut_id=m.shortcut_id, recipients=self._recipients(m.recipients), no_activity_days=m.no_activity_days)
        return True

    def _UpdateBusinessAwayMessageRequest(self, request):
        m = request.message
        self.account.full.business_away_message = T.BusinessAwayMessage(
            shortcut_id=m.shortcut_id, schedule=m.schedule, recipients=self._recipients(m.recipients),
            offline_only=m.offline_only)
        return True

    def _CreateBusinessChatLinkRequest(self, request):
        link = T.BusinessChatLink(link=f"https://t.me/m/{new_id()}", message=request.link.message,
                                  views=0, entities=request.link.entities, title=request.link.title)
        self.account.chat_links.append(link)
        return link

    def _UpdatePersonalChannelRequest(self, request):
        self.account.full.personal_channel_id = request.channel.channel_id
        return True


class FakeEvent:
    """Outgoing NewMessage event."""

    def __init__(self, text, *, chat_id, msg_id=None, out=True, reply=None, is_private=None,
                 is_channel=False, sender_id=None, fwd=False, chat=None, date=NOW, sender=None):
        self.raw_text = text
        self.chat_id = chat_id
        self.id = msg_id or new_id()
        self.out = out
        self.is_private = (not is_channel) if is_private is None else is_private
        self.is_channel = is_channel
        self.is_group = False
        self.sender_id = sender_id
        self.date = date
        self._reply = reply
        self._chat = chat
        self._sender = sender
        self.message = SimpleNamespace(fwd_from=object() if fwd else None, via_bot_id=None,
                                       media=None, entities=None)
        self.edits: list[str] = []
        self.responses: list[str] = []
        self.deleted = False

    @property
    def text_out(self) -> str:
        return "\n".join(self.edits + self.responses)

    async def edit(self, text, **kwargs):
        self.edits.append(text)

    async def respond(self, text, **kwargs):
        self.responses.append(text)

    async def reply(self, text, **kwargs):
        self.responses.append(text)
        return SimpleNamespace(edit=self.edit)

    async def get_reply_message(self):
        return self._reply

    async def get_chat(self):
        return self._chat or SimpleNamespace(bot=False, title="chat")

    async def get_input_chat(self):
        return self.chat_id

    async def get_input_sender(self):
        return self.sender_id

    async def get_sender(self):
        return self._sender

    async def delete(self):
        self.deleted = True


def flood(seconds: int) -> FloodWaitError:
    error = FloodWaitError(request=None, capture=seconds)
    return error
