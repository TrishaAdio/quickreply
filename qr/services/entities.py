"""Text + Telegram entity helpers (ported from ChannelGuard's quickreply.py).

Telegram counts entity offsets in UTF-16 code units; every helper here keeps
bold/links/premium custom emoji attached to the right span when text changes.
"""
from __future__ import annotations

import base64
import copy
import re
from datetime import datetime

from telethon.tl import types as tl_types
from telethon.tl.tlobject import TLObject

# The link the guard sends us (full https invite link).
LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)
# The link inside a saved post (may or may not include the scheme).
FIND_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)[\w-]+", re.IGNORECASE)


def u16(text: str) -> int:
    """Length in UTF-16 code units (how Telegram counts entity offsets)."""
    return len((text or "").encode("utf-16-le")) // 2


def swap_link(text: str, entities, new_link: str):
    """Point an invite link at ``new_link`` (hyperlink entity and raw text).

    Returns ``(new_text, entities)`` or ``None`` when nothing changed.
    """
    entities = [copy.copy(e) for e in (entities or [])]
    changed = False
    for entity in entities:
        url = getattr(entity, "url", None)
        if url and FIND_LINK_RE.search(url) and url != new_link:
            entity.url = new_link
            changed = True

    new_text = text or ""
    match = FIND_LINK_RE.search(new_text)
    if match and match.group(0) != new_link:
        old = match.group(0)
        position = match.start()
        offset = u16(new_text[:position])
        old_len, new_len = u16(old), u16(new_link)
        delta = new_len - old_len
        r_start, r_end = offset, offset + old_len
        for entity in entities:
            start, end = entity.offset, entity.offset + entity.length
            if end <= r_start:
                pass
            elif start >= r_end:
                entity.offset += delta
            else:
                entity.length = max(0, entity.length + delta)
        new_text = new_text[:position] + new_link + new_text[position + len(old):]
        changed = True
    return (new_text, entities) if changed else None


def substitute(text: str, entities, mapping: dict):
    """Replace ``{tokens}`` while keeping every entity on the right span."""
    text = text or ""
    entities = [copy.copy(e) for e in (entities or [])]
    if not mapping:
        return text, entities
    pattern = re.compile("|".join(
        re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    changes, parts, last = [], [], 0
    for match in pattern.finditer(text):
        value = str(mapping[match.group(0)])
        start = u16(text[:match.start()])
        old = u16(match.group(0))
        changes.append((start, start + old, u16(value) - old))
        parts.append(text[last:match.start()])
        parts.append(value)
        last = match.end()
    parts.append(text[last:])
    new_text = "".join(parts)
    for entity in entities:
        e_start, e_end = entity.offset, entity.offset + entity.length
        off_delta = len_delta = 0
        for r_start, r_end, delta in changes:
            if r_end <= e_start:
                off_delta += delta
            elif r_start >= e_end:
                pass
            else:
                len_delta += delta
        entity.offset += off_delta
        entity.length = max(0, entity.length + len_delta)
    return new_text, entities


def substitute_spans(text: str, entities, replacements: list[tuple]):
    """Replace non-overlapping ``(start, end, value)`` character spans."""
    text = text or ""
    entities = [copy.copy(e) for e in (entities or [])]
    accepted, cursor = [], 0
    for start, end, value in sorted(replacements, key=lambda r: (r[0], r[1])):
        if start < cursor or end <= start:
            continue
        accepted.append((start, end, str(value)))
        cursor = end
    if not accepted:
        return text, entities
    parts, cursor, changes = [], 0, []
    for start, end, value in accepted:
        parts.extend((text[cursor:start], value))
        start_u16 = u16(text[:start])
        old_u16 = u16(text[start:end])
        changes.append((start_u16, start_u16 + old_u16, u16(value) - old_u16))
        cursor = end
    parts.append(text[cursor:])
    for entity in entities:
        e_start, e_end = entity.offset, entity.offset + entity.length
        off_delta = len_delta = 0
        for r_start, r_end, delta in changes:
            if r_end <= e_start:
                off_delta += delta
            elif r_start < e_end:
                len_delta += delta
        entity.offset += off_delta
        entity.length = max(0, entity.length + len_delta)
    return "".join(parts), entities


# ---------------------------------------------------------------------------
# Entity (de)serialisation and generic TL <-> BSON-safe conversion
# ---------------------------------------------------------------------------
def serialize_entities(entities) -> list[dict]:
    out = []
    for entity in entities or []:
        data = {k: v for k, v in entity.to_dict().items() if k != "_"}
        data["_type"] = type(entity).__name__
        out.append(data)
    return out


def deserialize_entities(dicts) -> list:
    out = []
    for data in dicts or []:
        cls = getattr(tl_types, data.get("_type", ""), None)
        if cls is None:
            continue
        try:
            out.append(cls(**{k: v for k, v in data.items() if k != "_type"}))
        except Exception:  # noqa: BLE001
            continue
    return out


def tl_to_plain(value):
    """Convert TL objects to BSON-safe nested dicts tagged with ``_``."""
    if isinstance(value, TLObject):
        value = value.to_dict()
    if isinstance(value, dict):
        return {str(k): tl_to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tl_to_plain(v) for v in value]
    if isinstance(value, bytes):
        return {"$b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, int) and not -(2 ** 63) <= value < 2 ** 63:
        return {"$bigint": str(value)}
    return value


def tl_from_plain(value):
    """Inverse of :func:`tl_to_plain` for objects in ``telethon.tl.types``."""
    if isinstance(value, list):
        return [tl_from_plain(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"$b64"}:
            return base64.b64decode(value["$b64"])
        if set(value) == {"$bigint"}:
            return int(value["$bigint"])
        name = value.get("_")
        kwargs = {k: tl_from_plain(v) for k, v in value.items() if k != "_"}
        if name:
            cls = getattr(tl_types, name, None)
            if cls is None:
                raise ValueError(f"unknown TL type {name}")
            return cls(**kwargs)
        return kwargs
    if isinstance(value, datetime):
        return value
    return value


def entity_fallbacks(entities):
    """Full entities, then custom-emoji-free entities, then plain text."""
    entities = list(entities or [])
    tries = [entities]
    no_custom = [e for e in entities if type(e).__name__ != "MessageEntityCustomEmoji"]
    if len(no_custom) != len(entities):
        tries.append(no_custom)
    if entities:
        tries.append([])
    return tries


def is_entity_error(error: Exception) -> bool:
    detail = f"{type(error).__name__}: {error}".upper()
    markers = ("ENTITY", "CUSTOM_EMOJI", "CUSTOM EMOJI", "EMOTICON",
               "PREMIUM_ACCOUNT_REQUIRED", "PREMIUM ACCOUNT REQUIRED")
    return any(marker in detail for marker in markers)


def fit_media_caption(text: str, entities, preserve="", limit: int = 1024):
    """Fit a caption to Telegram's UTF-16 limit, keeping required values."""
    text = text or ""
    entities = [copy.copy(e) for e in (entities or [])]
    if u16(text) <= limit:
        return text, entities, False

    def prefix_length(max_units: int) -> int:
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if u16(text[:middle]) <= max_units:
                low = middle
            else:
                high = middle - 1
        return low

    if isinstance(preserve, str):
        required = [preserve] if preserve else []
    else:
        required = [str(value) for value in (preserve or []) if value]
    prefix = ""
    keep = prefix_length(limit)
    positions = [(value, text.find(value)) for value in required]
    clipped_required = [(v, p) for v, p in positions if p < 0 or p + len(v) > keep]
    if clipped_required:
        prefix = " | ".join(required) + "\n"
        keep = prefix_length(max(0, limit - u16(prefix)))
        duplicates = [p for _v, p in positions if 0 <= p < keep]
        if duplicates:
            keep = min(duplicates)
    clipped = prefix + text[:keep]
    shift = u16(prefix)
    original_limit = u16(clipped) - shift
    kept = []
    for entity in entities:
        if entity.offset >= original_limit:
            continue
        entity.length = min(entity.length, original_limit - entity.offset)
        if entity.length <= 0:
            continue
        entity.offset += shift
        kept.append(entity)
    return clipped, kept, True


def split_plain_text(text: str, *, header: str = "", limit: int = 4000) -> list[str]:
    """Split at line boundaries, repeating ``header`` on continuation chunks."""
    chunks: list[str] = []
    current = ""
    continued_prefix = (header + "\n") if header else ""
    available = max(1, limit - u16(continued_prefix))

    def split_units(value: str, max_units: int) -> list[str]:
        pieces, piece, units = [], "", 0
        for char in value:
            char_units = 2 if ord(char) > 0xFFFF else 1
            if units + char_units > max_units:
                if piece:
                    pieces.append(piece)
                piece, units = char, char_units
            else:
                piece += char
                units += char_units
        if piece or not pieces:
            pieces.append(piece)
        return pieces

    for line in (text or "").splitlines() or [""]:
        candidate = line if not current else current + "\n" + line
        if u16(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        pieces = split_units(line, available)
        for piece in pieces[:-1]:
            chunks.append(continued_prefix + piece)
        current = continued_prefix + pieces[-1]
    if current:
        chunks.append(current)
    return chunks or [header or ""]
