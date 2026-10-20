""".graph / .tgraph - DM traffic + income per hour as a neon glow card.

    .graph                      today (live so far)
    .graph yesterday|YYYY-MM-DD another day
    .graph scan [day]           backfill that day's DMs from Telegram history first
    .tgraph                     test render with synthetic data (nothing stored)
"""
from __future__ import annotations

import asyncio
import io

from qr import ui
from qr.services import dm_stats
from qr.services.clock import utcnow


def _account_label(app) -> str:
    me = app.me
    name = (getattr(me, "first_name", "") or "").strip()
    username = getattr(me, "username", None)
    return f"{name} @{username}".strip() if username else name


async def _render(stats, label: str) -> bytes:
    try:
        from qr.services import graph_render
    except ImportError:
        raise RuntimeError("Pillow is not installed - run: python setup.py (or pip install -r requirements.txt)") from None
    # Rendering takes ~1s of CPU; keep the Telegram event loop responsive.
    return await asyncio.to_thread(graph_render.render, stats, account=label,
                                   generated=utcnow())


async def _send(app, event, stats) -> None:
    try:
        image = await _render(stats, _account_label(app))
    except RuntimeError as error:
        await app.ack(event, str(error))
        return
    payload = io.BytesIO(image)
    payload.name = f"dm_graph_{stats.date}{'_test' if stats.test else ''}.jpg"
    await app.client.send_file("me", file=payload, caption=dm_stats.summary_text(stats))
    await app.ack(event, ("Test graph sent (synthetic data, nothing stored)." if stats.test
                          else f"Graph for {stats.date} sent."))


async def cmd_graph(app, event) -> None:
    args = (event.raw_text or "").split()[1:]
    scan = bool(args) and args[0].lower() == "scan"
    if scan:
        args = args[1:]
    try:
        day = dm_stats.resolve_day(args[0] if args else None)
    except ValueError as error:
        await app.ack(event, f"{error}. Usage: .graph [today|yesterday|YYYY-MM-DD] or .graph scan [day]")
        return
    if scan:
        await app.ack(event, f"Scanning your private chats for DMs on {day} (IST)...")

        async def progress(chats, messages):
            await app.ack(event, f"Scanning {day}... {chats} chat(s), {messages} DM(s) so far")

        try:
            result = await dm_stats.scan_day(app, day, progress)
        except Exception as error:  # noqa: BLE001
            await app.ack(event, f"Scan failed: {type(error).__name__}: {error}")
            return
        ui.info(f"[graph] scan {day}: {result}")
        await app.ack(event, f"Scan done: {result['chats']} chat(s), {result['messages']} DM(s), "
                             f"{result['added']} new. Rendering...")
    elif not app.cfg.get("dm_tracking", True):
        await app.ack(event, "DM tracking is off (.config dm_tracking on). Rendering stored data...")
    else:
        await app.ack(event, f"Rendering graph for {day}...")
    stats = await dm_stats.day_stats(app, day)
    await _send(app, event, stats)


async def cmd_tgraph(app, event) -> None:
    await app.ack(event, "Rendering a test graph with synthetic data...")
    await _send(app, event, dm_stats.synthetic(now=utcnow()))
