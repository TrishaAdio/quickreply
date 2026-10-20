"""Start QuickReply: config -> MongoDB -> Telegram -> handlers + background loops."""
from __future__ import annotations

import asyncio
import os

from qr import BUILD, ui
from qr.app import App
from qr.config import settings as config
from qr.database.mongo import Database, DatabaseError
from qr.handlers import core, payments, router
from qr.locks import ProcessLock, account_lock_path, session_lock_path
from qr.services import archive_service

PAYMENT_RECOVERY_SECONDS = 20
ARCHIVE_CYCLE_SECONDS = 60


def ensure_sessions_dir() -> None:
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config.SESSIONS_DIR, 0o700)
    except OSError:
        pass


async def _loop(name: str, seconds: float, func) -> None:
    while True:
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            ui.warn(f"{name} failed: {type(error).__name__}: {error}")
        await asyncio.sleep(seconds)


async def run(session_name: str = config.DEFAULT_SESSION) -> None:
    from telethon import TelegramClient, events

    ui.box("QUICKREPLY", "Backup \u2022 Restore \u2022 Archives" if ui._UNICODE else "Backup - Restore - Archives")
    settings = config.load(session_name)

    db = Database(settings.mongo_url)
    ui.info(f"Connecting to MongoDB {db.redacted_url} ...")
    try:
        await db.ping()
        await db.init()
    except DatabaseError as error:
        ui.error(str(error))
        raise SystemExit(1) from None
    ui.success(f"MongoDB ready (database '{db.db_name}').")

    ensure_sessions_dir()
    session_lock = ProcessLock(session_lock_path(settings.session_path))
    if not session_lock.acquire():
        ui.error("Another QuickReply process is using this session. Stop it first.")
        raise SystemExit(1)

    client = TelegramClient(settings.session_path, settings.api_id, settings.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        ui.warn("This session is not logged in yet - starting Telegram's normal login "
                "(phone number, login code, then your 2FA password if you use one).")
        from qr.installer import login

        await login(client)
    me = await client.get_me()
    account_lock = ProcessLock(account_lock_path(me.id))
    if not account_lock.acquire():
        ui.error("QuickReply is already running for this Telegram account on this machine.")
        await client.disconnect()
        raise SystemExit(1)

    app = App(client, db, self_id=me.id, me=me)
    await app.load_settings()
    await app.register_account()
    if not app.cfg.get("dm_tracking_since"):
        from qr.database.mongo import utcnow

        await app.set(dm_tracking_since=utcnow())
    recovered = await payments.recover_payments(app, interrupted_adds=True)
    if recovered:
        ui.warn(f"Recovered {recovered} interrupted payment/channel-delivery record(s).")

    async def incoming(event):
        try:
            await core.on_incoming(app, event)
        except Exception as error:  # noqa: BLE001
            ui.error(f"incoming handler failed: {type(error).__name__}: {error}")

    async def outgoing(event):
        await router.on_outgoing(app, event)

    client.add_event_handler(incoming, events.NewMessage(incoming=True))
    client.add_event_handler(outgoing, events.NewMessage(outgoing=True))

    async def payment_cycle():
        async with app.payment_lock:
            await payments.recover_payments(app, interrupted_adds=False)

    async def archive_cycle():
        result = await archive_service.run_cycle(app)
        for key in result["delivered"]:
            ui.success(f"[archive] delivered {key}")
        for problem in result["errors"]:
            ui.warn(f"[archive] {problem}")

    tasks = [
        asyncio.create_task(_loop("payment recovery", PAYMENT_RECOVERY_SECONDS, payment_cycle)),
        asyncio.create_task(_loop("archive cycle", ARCHIVE_CYCLE_SECONDS, archive_cycle)),
    ]
    cfg = app.cfg
    ui.heading("QuickReply is running")
    ui.success(f"Logged in as {ui.bold(me.first_name or '')} (id {me.id}){' - Premium' if me.premium else ''}.")
    ui.info(f"Build {BUILD}. Send .help in Saved Messages for every command.")
    ui.info(f"Payment channel: {cfg.get('payment_channel') or ui.yellow('not set (.setchannel)')}")
    ui.info(f"Archive channel: {cfg.get('archive_channel')} "
            f"({'on' if cfg.get('archive_enabled', True) else 'off'})")
    ui.info(f"Posting channel: {cfg.get('publish_channel') or ui.yellow('not set (.setpostchannel)')}")
    if not me.premium:
        ui.warn("This account has no Telegram Premium: quick replies and Business features are unavailable.")
    print(ui.dim("Ctrl+C to stop."))
    try:
        await client.run_until_disconnected()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
        account_lock.close()
        session_lock.close()
