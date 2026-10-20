"""DM traffic + income per hour (Asia/Kolkata) for ``.graph``.

Every incoming private message from a real user is stored as one tiny
``dm_events`` row (sender id, message id, time - never the text). The row id
is ``account:user:message`` so live tracking and a history ``scan`` can never
count the same message twice. Rows expire after ``DM_RETENTION_DAYS``.

Income comes from the ``/add`` payment log: ``valid`` payments plus ones reset
by ``/clear`` (real money, only hidden from ``/stats``); fake/failed are not
counted.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from pymongo.errors import BulkWriteError, DuplicateKeyError

from qr.config.settings import IST
from qr.database.mongo import utcnow
from qr.services.clock import as_utc, day_window, ist_date, parse_day, today_ist
from qr.services.payments import money

TELEGRAM_SERVICE_ID = 777000
INCOME_STATUSES = ("valid", "cleared")
WINDOW_HOURS = 3


@dataclass
class DayStats:
    date: str
    dms: list[int] = field(default_factory=lambda: [0] * 24)
    senders: list[int] = field(default_factory=lambda: [0] * 24)
    inr: list[Decimal] = field(default_factory=lambda: [Decimal("0")] * 24)
    payments: list[int] = field(default_factory=lambda: [0] * 24)
    unique_senders: int = 0
    tracking_since: datetime | None = None
    now_hour: int | None = None      # current IST hour when the day is today
    now_minute: int = 0
    test: bool = False

    # ---- derived ---------------------------------------------------------
    @property
    def total_dms(self) -> int:
        return sum(self.dms)

    @property
    def total_inr(self) -> Decimal:
        return sum(self.inr, Decimal("0"))

    @property
    def total_payments(self) -> int:
        return sum(self.payments)

    @property
    def peak_dm_hour(self) -> int | None:
        return _argmax(self.dms)

    @property
    def peak_inr_hour(self) -> int | None:
        return _argmax(self.inr)

    def busiest_window(self, hours: int = WINDOW_HOURS) -> tuple[int, int] | None:
        """Start hour + DM count of the busiest ``hours``-long window."""
        if not self.total_dms:
            return None
        best = max(range(0, 25 - hours), key=lambda h: (sum(self.dms[h:h + hours]), -h))
        return best, sum(self.dms[best:best + hours])

    def conversion(self) -> float | None:
        """Payments per unique DM sender (rough lead -> sale ratio)."""
        if not self.unique_senders:
            return None
        return self.total_payments / self.unique_senders


def _argmax(values) -> int | None:
    if not any(values):
        return None
    return max(range(len(values)), key=lambda i: (values[i], -i))


def hour_label(hour: int) -> str:
    return f"{hour:02d}:00\u2013{(hour + 1) % 24:02d}:00"


def hour_12(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}{suffix}"


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
def _row(account_id: int, user_id: int, message_id: int, when: datetime) -> dict:
    when = as_utc(when)
    local = when.astimezone(IST)
    return {
        "_id": f"{int(account_id)}:{int(user_id)}:{int(message_id)}",
        "account_id": int(account_id),
        "user_id": int(user_id),
        "at": when,
        "date": local.date().isoformat(),
        "hour": local.hour,
    }


def countable(app, sender_id, sender=None) -> bool:
    """Customers only: not ourselves, bots, Telegram service or the link source."""
    if not sender_id or sender_id in (app.self_id, TELEGRAM_SERVICE_ID):
        return False
    if sender_id == app.cfg.get("link_source"):
        return False
    if sender is not None and (getattr(sender, "bot", False) or getattr(sender, "is_self", False)):
        return False
    return True


async def record_dm(app, event) -> bool:
    """Store one incoming private message. Returns True when newly counted."""
    if not app.cfg.get("dm_tracking", True) or not getattr(event, "is_private", False):
        return False
    sender_id = getattr(event, "sender_id", None)
    sender = None
    try:
        sender = await event.get_sender()
    except Exception:  # noqa: BLE001 - unknown entity: still a DM
        sender = None
    if not countable(app, sender_id, sender):
        return False
    when = getattr(event, "date", None) or utcnow()
    try:
        await app.db.dm_events.insert_one(_row(app.self_id, sender_id, event.id, when))
        return True
    except DuplicateKeyError:
        return False


async def scan_day(app, day: str, progress=None) -> dict:
    """Backfill ``day`` from Telegram history (DMs received while offline).

    Idempotent: messages already recorded are skipped by their id.
    """
    start, end = day_window(day)
    chats = added = seen = 0
    async for dialog in app.client.iter_dialogs():
        if not getattr(dialog, "is_user", False):
            continue
        last = getattr(dialog, "date", None)
        if last is not None and as_utc(last) < start:
            if getattr(dialog, "pinned", False):
                continue
            break  # dialogs are newest-first after the pinned ones
        entity = dialog.entity
        if not countable(app, getattr(entity, "id", None), entity):
            continue
        rows = []
        async for message in app.client.iter_messages(entity, offset_date=end):
            date = as_utc(message.date)
            if date < start:
                break
            if date >= end or getattr(message, "out", False):
                continue
            if getattr(message, "action", None) is not None:
                continue  # service message (call, screenshot notice...)
            rows.append(_row(app.self_id, entity.id, message.id, date))
        chats += 1
        seen += len(rows)
        if rows:
            try:
                result = await app.db.dm_events.insert_many(rows, ordered=False)
                added += len(result.inserted_ids)
            except BulkWriteError as error:
                added += error.details.get("nInserted", 0)
        if progress is not None and chats % 25 == 0:
            await progress(chats, seen)
        await app.sleep(0.2)
    return {"chats": chats, "messages": seen, "added": added}


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
async def day_stats(app, day: str, *, now: datetime | None = None) -> DayStats:
    day = parse_day(day).isoformat()
    stats = DayStats(date=day)
    pipeline = [
        {"$match": {"account_id": app.self_id, "date": day}},
        {"$group": {"_id": "$hour", "dms": {"$sum": 1}, "users": {"$addToSet": "$user_id"}}},
    ]
    everyone = set()
    async for row in await app.db.dm_events.aggregate(pipeline):
        hour = int(row["_id"])
        stats.dms[hour] = row["dms"]
        stats.senders[hour] = len(row["users"])
        everyone.update(row["users"])
    stats.unique_senders = len(everyone)

    start, end = day_window(day)
    cursor = app.db.payments.find(
        {"account_id": app.self_id, "status": {"$in": list(INCOME_STATUSES)},
         "ts": {"$gte": start.timestamp(), "$lt": end.timestamp()}},
        {"amount": 1, "ts": 1})
    async for payment in cursor:
        try:
            amount = money(payment["amount"])
        except Exception:  # noqa: BLE001
            continue
        hour = datetime.fromtimestamp(float(payment["ts"]), IST).hour
        stats.inr[hour] += amount
        stats.payments[hour] += 1

    since = app.cfg.get("dm_tracking_since")
    stats.tracking_since = as_utc(since) if isinstance(since, datetime) else None
    now = as_utc(now or utcnow())
    if day == ist_date(now):
        stats.now_hour = now.astimezone(IST).hour
        stats.now_minute = now.astimezone(IST).minute
    return stats


def synthetic(day: str | None = None, *, seed: int | None = None, now: datetime | None = None) -> DayStats:
    """Realistic fake day for ``.tgraph`` (nothing is read or written)."""
    rng = random.Random(seed)
    day = day or today_ist(now)
    stats = DayStats(date=day, test=True)
    # quiet night, morning ramp, lunch bump, big evening peak
    shape = [3, 2, 1, 1, 1, 2, 4, 7, 11, 14, 16, 18, 21, 19, 17, 18, 22, 27, 33, 40, 46, 42, 30, 14]
    users = 0
    for hour, base in enumerate(shape):
        dms = max(0, int(rng.gauss(base * 2.2, base * 0.45)))
        stats.dms[hour] = dms
        stats.senders[hour] = max(0, min(dms, int(dms * rng.uniform(0.35, 0.6))))
        users += stats.senders[hour]
        count = max(0, int(rng.gauss(base / 7, 0.8)))
        stats.payments[hour] = count
        stats.inr[hour] = sum((Decimal(rng.choice((199, 299, 499, 799, 999, 1499, 2499)))
                               for _ in range(count)), Decimal("0"))
    stats.unique_senders = min(users, int(sum(stats.dms) * rng.uniform(0.3, 0.42)))
    stats.tracking_since = None
    return stats


def summary_text(stats: DayStats) -> str:
    """Plain-text companion caption (fits Telegram's 1024-unit limit)."""
    from qr.services.payments import fmt_inr

    title = parse_day(stats.date).strftime("%a %d %b %Y")
    lines = [("TEST GRAPH (synthetic data) - " if stats.test else "") + f"DM traffic {title} (IST)"]
    lines.append(f"DMs: {stats.total_dms} from {stats.unique_senders} people")
    peak = stats.peak_dm_hour
    if peak is not None:
        lines.append(f"Peak DMs: {hour_label(peak)} ({stats.dms[peak]})")
        window = stats.busiest_window()
        lines.append(f"Busiest {WINDOW_HOURS}h: {hour_label(window[0])[:5]}\u2013"
                     f"{(window[0] + WINDOW_HOURS) % 24:02d}:00 ({window[1]} DMs)")
    lines.append(f"Income: {fmt_inr(stats.total_inr) if stats.total_payments else fmt_inr(0)}"
                 f" from {stats.total_payments} payment(s)")
    peak_inr = stats.peak_inr_hour
    if peak_inr is not None:
        lines.append(f"Peak income: {hour_label(peak_inr)} ({fmt_inr(stats.inr[peak_inr])})")
    if stats.tracking_since and ist_date(stats.tracking_since) == stats.date:
        lines.append(f"Tracking started {stats.tracking_since.astimezone(IST):%H:%M} IST today - "
                     "use .graph scan to backfill earlier DMs.")
    return "\n".join(lines)[:1000]


def resolve_day(argument: str | None, now: datetime | None = None) -> str:
    argument = (argument or "").strip().lower()
    today = today_ist(now)
    if argument in ("", "today"):
        return today
    if argument == "yesterday":
        return (parse_day(today) - timedelta(days=1)).isoformat()
    return parse_day(argument).isoformat()
