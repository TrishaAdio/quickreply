"""Payment logger logic: amounts, order ids, templates, receipt rendering.

Ported from ChannelGuard's quickreply.py *without* the keyword/admin-bot
reservation bridge: ``/add <amount> <name>`` records a payment, sends the
private receipt and (when replying to a proof image) posts it to the payment
channel. There is no ``{link}`` token any more.
"""
from __future__ import annotations

import copy
import random
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from telethon.tl.types import MessageEntityBold

from qr.database.settings_store import DEFAULT_CHANNEL, DEFAULT_DONE
from qr.services.entities import (
    deserialize_entities,
    substitute,
    substitute_spans,
    u16,
)

_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal("1000000000000000")
# Unambiguous alphabet (no 0/O/1/I) so ids read back easily from screenshots.
ORDER_ALPHABET = "ACDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKENS = ("{amount}", "{name}", "{orderid}", "{rioshare}", "{marco}", "{total}", "{todaytotal}")


def parse_amount(raw) -> Decimal:
    """Parse one positive, finite INR amount and round it to paise."""
    cleaned = str(raw).replace(",", "").replace("\u20b9", "").strip()
    cleaned = re.sub(r"\s*INR\s*$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError("invalid amount") from error
    if not value.is_finite() or value <= 0 or value > _MAX_AMOUNT:
        raise ValueError("amount must be positive and finite")
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def money(value) -> Decimal:
    value = Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    if not value.is_finite():
        raise ValueError("non-finite money value")
    return value


def payment_total(payments) -> Decimal:
    return sum((money(p["amount"]) for p in payments), Decimal("0.00"))


def fmt_inr(value) -> str:
    """Rupee sign + Indian digit grouping (1,23,456.50)."""
    value = money(value)
    negative = value < 0
    value = abs(value)
    whole = int(value)
    paise = int((value - Decimal(whole)) * 100)
    digits = str(whole)
    if len(digits) > 3:
        last3, rest = digits[-3:], digits[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped = ",".join(groups) + "," + last3
    else:
        grouped = digits
    out = "\u20b9" + grouped + (f".{paise:02d}" if paise else "")
    return ("-" if negative else "") + out


def generate_order_id(prefix: str, length: int, existing: set[str]) -> str:
    for _ in range(50):
        candidate = prefix + "".join(random.choices(ORDER_ALPHABET, k=length))
        if candidate not in existing:
            return candidate
    return prefix + "".join(random.choices(ORDER_ALPHABET, k=length + 4))


def pay_mapping(amount, name: str, cfg: dict, today: list, order_id: str = "",
                include_current: bool = False) -> dict:
    amount = money(amount)
    today_total = payment_total(today)
    today_count = len(today)
    if include_current:
        today_total += amount
        today_count += 1
    base = today_total if cfg.get("share_base", "today") == "today" else amount
    rio = base * Decimal(str(cfg.get("rio_pct", 55.0))) / Decimal("100")
    marco = base * Decimal(str(cfg.get("marco_pct", 45.0))) / Decimal("100")
    return {
        "{amount}": fmt_inr(amount),
        "{name}": name or "",
        "{orderid}": order_id or "",
        "{rioshare}": fmt_inr(rio),
        "{marco}": fmt_inr(marco),
        "{total}": str(today_count),
        "{todaytotal}": fmt_inr(today_total),
        # Removed with the keyword/bot feature; old templates must not leak it.
        "{link}": "",
    }


_TOKEN_ALIAS_RE = re.compile(
    r"[\{\uff5b\ufe5b\u2774]"
    r"([^{}\uff5b\uff5d\ufe5b\ufe5c\u2774\u2775]{1,40})"
    r"[\}\uff5d\ufe5c\u2775]",
    re.IGNORECASE,
)
_TOKEN_ALIASES = {token.strip("{}"): token for token in TOKENS}


def canonicalize_tokens(text: str, entities):
    """Normalise full-width braces, case, spaces and joiners in tokens."""
    aliases = {}
    for match in _TOKEN_ALIAS_RE.finditer(text or ""):
        key = unicodedata.normalize("NFKC", match.group(1))
        key = re.sub(r"[\s_\-\u200b-\u200f\u2060\ufeff]+", "", key).casefold()
        canonical = _TOKEN_ALIASES.get(key)
        if canonical:
            aliases[match.group(0)] = canonical
    if not aliases:
        return text or "", [copy.copy(e) for e in (entities or [])]
    return substitute(text, entities, aliases)


_AMOUNT = r"(?P<value>(?:₹\s*|INR\s*)?[+-]?\d[\d,]*(?:\.\d+)?)"
_LEGACY_FIELDS = (
    (re.compile(r"(?im)\bpayment\s+of\s*" + _AMOUNT), "{amount}"),
    (re.compile(r"(?im)^\s*amount\s*:\s*" + _AMOUNT), "{amount}"),
    (re.compile(r"(?im)^\s*marco(?:'s)?\s+share\s*:\s*" + _AMOUNT), "{marco}"),
    (re.compile(r"(?im)^\s*rio(?:'s)?\s+share\s*:\s*" + _AMOUNT), "{rioshare}"),
    (re.compile(r"(?im)^\s*payment\s+count\s*:\s*#?\s*(?P<value>\d+)"), "{total}"),
    (re.compile(r"(?im)^\s*total\s*:\s*" + _AMOUNT), "{todaytotal}"),
    (re.compile(r"(?im)^\s*order\s+id\s*:\s*(?P<value>[A-Za-z][A-Za-z0-9_-]{3,})"), "{orderid}"),
)


def canonicalize_legacy_fields(text: str, entities):
    """Turn values copied from an old rendered receipt back into tokens."""
    replacements = []
    for pattern, token in _LEGACY_FIELDS:
        for match in pattern.finditer(text or ""):
            replacements.append((*match.span("value"), token))
    return substitute_spans(text, entities, replacements)


def prepare_template(text: str, entities):
    text, entities = canonicalize_tokens(text, entities)
    return canonicalize_legacy_fields(text, entities)


def ensure_payment_metadata(text: str, amount, order_id: str) -> str:
    text = text or ""
    missing = []
    if order_id and order_id not in text:
        missing.append(f"Order ID: {order_id}")
    rendered = fmt_inr(amount)
    if rendered not in text:
        missing.append(f"Amount: {rendered}")
    if missing:
        text += ("\n\n" if text else "") + "\n".join(missing)
    return text


def render(kind: str, cfg: dict, amount, name: str, order_id: str, today: list,
           include_current: bool = False):
    """Render the done/channel template; damaged templates fall back to defaults."""
    try:
        text = cfg.get(f"{kind}_template") or (DEFAULT_DONE if kind == "done" else DEFAULT_CHANNEL)
        entities = deserialize_entities(cfg.get(f"{kind}_entities"))
        text, entities = prepare_template(text, entities)
        mapping = pay_mapping(amount, name, cfg, today, order_id, include_current)
        text, entities = substitute(text, entities, mapping)
        # Required values are substituted again at the delivery boundary.
        text, entities = canonicalize_tokens(text, entities)
        text, entities = substitute(text, entities, {
            "{amount}": fmt_inr(amount), "{orderid}": order_id})
    except Exception:  # noqa: BLE001
        base = DEFAULT_DONE if kind == "done" else DEFAULT_CHANNEL
        mapping = pay_mapping(amount, name, cfg, today, order_id, include_current)
        text, entities = substitute(base, [], mapping)
    return ensure_payment_metadata(text, amount, order_id), list(entities or [])


def required_payment_text(cfg: dict, amount, name: str, order_id: str, today: list,
                          include_current: bool) -> str:
    """Plain complete metadata used when a receipt exceeds Telegram limits."""
    mapping = pay_mapping(amount, name, cfg, today, order_id, include_current)
    return "\n".join([
        f"Order ID: {order_id}",
        f"Amount: {mapping['{amount}']}",
        f"Name: {name}",
        f"Rio: {mapping['{rioshare}']}",
        f"Marco: {mapping['{marco}']}",
        f"Payments today: {mapping['{total}']}",
        f"Collected today: {mapping['{todaytotal}']}",
    ])


PRIVATE_INVITE_RE = re.compile(
    r"https?://(?:www\.)?t\.me/(?:joinchat/|\+)[^\s<>()]+", re.IGNORECASE)


def sanitize_channel_invites(text: str, entities):
    """Never copy private invite URLs into the (public) payment channel."""
    matches = list(dict.fromkeys(PRIVATE_INVITE_RE.findall(text or "")))
    if matches:
        text, entities = substitute(text, entities, {m: "[private link]" for m in matches})
    safe = [e for e in (entities or [])
            if not PRIVATE_INVITE_RE.search(str(getattr(e, "url", "") or ""))]
    return text, safe


FAKE_LABEL = "FAKE PAYMENT"


def fake_caption(text: str, entities):
    """Prefix a payment post with a bold FAKE PAYMENT marker (<=1024 units)."""
    prefix = f"{FAKE_LABEL}\n\n"
    text = text or ""
    if text.startswith(prefix) or text == FAKE_LABEL:
        return text, list(entities or [])
    shift = u16(prefix)
    shifted = [copy.copy(e) for e in (entities or [])]
    for entity in shifted:
        entity.offset += shift
    shifted.insert(0, MessageEntityBold(offset=0, length=u16(FAKE_LABEL)))
    new_text = prefix + text
    while u16(new_text) > 1024:
        new_text = new_text[:-1]
    final = u16(new_text)
    kept = []
    for entity in shifted:
        if entity.offset >= final:
            continue
        entity.length = min(entity.length, final - entity.offset)
        if entity.length > 0:
            kept.append(entity)
    return new_text, kept


def is_fake_text(text: str) -> bool:
    return (text or "").startswith(FAKE_LABEL + "\n\n") or (text or "") == FAKE_LABEL


def post_retry_delay(attempts: int) -> float:
    return float(min(300, max(5, 5 * (2 ** min(max(attempts - 1, 0), 6)))))
