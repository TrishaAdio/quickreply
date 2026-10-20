""".setup - restore a saved configuration onto the CURRENT account.

    .setup                         list restorable accounts
    .setup <account id|backup id>  preview (no changes) + confirmation code
    .setup confirm <code>          take a pre-restore backup, then apply
    .setup cancel                  discard the pending confirmation
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta

from pymongo import ReturnDocument

from qr.database import backups as repo
from qr.database.mongo import SCHEMA_VERSION, utcnow
from qr.handlers.save import respond_long
from qr.services import profile_backup, profile_restore

CONFIRM_MINUTES = 10
_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")


async def _source_backup(app, ref: str):
    """Resolve a backup reference; the source must be a registered account."""
    if _OBJECT_ID.match(ref):
        backup = await repo.by_id(app.db, ref)
        if backup is None:
            raise ValueError(f"No backup with id {ref}.")
    elif ref.lstrip("-").isdigit():
        backup = await repo.latest(app.db, int(ref), kinds=("manual",))
        if backup is None:
            raise ValueError(f"Account {ref} has no backup. Run .save on that account first.")
    else:
        raise ValueError("Use a numeric account id or a 24-character backup id (see .backups).")
    if not await app.db.accounts.find_one({"_id": backup["account_id"]}):
        raise ValueError(f"Account {backup['account_id']} is not a registered QuickReply account "
                         "of this database; refusing to use its backup.")
    if backup.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Backup schema {backup.get('schema_version')} is not supported.")
    if backup.get("status") not in ("complete", "partial"):
        raise ValueError(f"Backup {backup['_id']} is {backup.get('status')}; it cannot be restored.")
    return backup


def _target_label(app) -> str:
    me = app.me
    name = " ".join(p for p in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if p)
    username = f" @{me.username}" if getattr(me, "username", None) else ""
    return f"{name}{username} (id {app.self_id})"


async def cmd_setup(app, event) -> None:
    parts = (event.raw_text or "").split()
    args = parts[1:]
    if not args:
        accounts = await app.db.accounts.find({}).to_list(50)
        lines = ["Restorable accounts:"]
        for account in accounts:
            latest = await repo.latest(app.db, account["_id"])
            when = f"{latest['created_at']:%Y-%m-%d %H:%M} UTC ({latest['status']})" if latest else "no backup"
            lines.append(f"{account['_id']}  {account.get('first_name', '')} "
                         f"@{account.get('username') or '-'}  latest: {when}")
        lines.append(f"\nTarget is always THIS account: {_target_label(app)}")
        lines.append("Preview: .setup <account id|backup id>")
        await respond_long(app, event, "\n".join(lines))
        return

    if args[0].lower() == "cancel":
        result = await app.db.restore_sessions.delete_many({"account_id": app.self_id, "used": False})
        await app.ack(event, f"Cancelled {result.deleted_count} pending restore(s).")
        return

    if args[0].lower() == "confirm":
        if len(args) < 2:
            await app.ack(event, "Usage: .setup confirm <code>")
            return
        await _confirm(app, event, args[1].strip().upper())
        return

    try:
        backup = await _source_backup(app, args[0])
    except ValueError as error:
        await app.ack(event, str(error))
        return
    await app.ack(event, "Comparing the backup with this account (no changes are made)...")
    report = await profile_restore.run(app, backup, dry_run=True)
    code = secrets.token_hex(3).upper()
    await app.db.restore_sessions.delete_many({"account_id": app.self_id, "used": False})
    await app.db.restore_sessions.insert_one({
        "code": code, "account_id": app.self_id, "backup_id": backup["_id"],
        "source_account_id": backup["account_id"], "used": False,
        "created_at": utcnow(), "expires_at": utcnow() + timedelta(minutes=CONFIRM_MINUTES),
    })
    changes = sum(1 for v in report.fields.values() if v["status"] == "will_restore")
    text = (f"RESTORE PREVIEW\nSource: account {backup['account_id']}, backup {backup['_id']} "
            f"({backup['created_at']:%Y-%m-%d %H:%M} UTC, {backup['status']})\n"
            f"Target: {_target_label(app)}\n"
            f"{changes} change(s) planned. A pre-restore backup of this account is taken first.\n\n"
            f"{report.text(limit=80)}\n\n"
            f"To apply to THIS account send within {CONFIRM_MINUTES} minutes:\n"
            f".setup confirm {code}")
    await respond_long(app, event, text)


async def _confirm(app, event, code: str) -> None:
    session = await app.db.restore_sessions.find_one_and_update(
        {"account_id": app.self_id, "code": code, "used": False, "expires_at": {"$gt": utcnow()}},
        {"$set": {"used": True, "used_at": utcnow()}}, return_document=ReturnDocument.AFTER)
    if session is None:
        await app.ack(event, "That confirmation code is invalid, expired or already used. "
                             "Run .setup <account|backup> again.")
        return
    backup = await repo.by_id(app.db, str(session["backup_id"]))
    if backup is None:
        await app.ack(event, "The selected backup no longer exists.")
        return
    if app.restore_lock.locked():
        await app.ack(event, "A restore is already running.")
        return
    async with app.restore_lock:
        await app.ack(event, "Taking a pre-restore backup of this account...")
        try:
            pre = await profile_backup.create_backup(app, kind="pre_restore")
        except Exception as error:  # noqa: BLE001
            await app.ack(event, f"Pre-restore backup failed ({type(error).__name__}: {error}); "
                                 "nothing was changed.")
            return
        await app.ack(event, f"Pre-restore backup {pre['backup_id']} ({pre['status']}). Restoring...")
        report = await profile_restore.run(app, backup, dry_run=False)
        status = "complete" if not report.failed else "partial"
        await app.db.restore_history.insert_one({
            "account_id": app.self_id, "source_account_id": backup["account_id"],
            "backup_id": backup["_id"], "pre_restore_backup_id": pre["backup_id"],
            "status": status, "fields": report.fields, "created_at": utcnow(),
        })
        await app.db.record_audit(app.self_id, "restore", backup_id=str(backup["_id"]),
                                  pre_restore_backup_id=pre["backup_id"], status=status,
                                  failed=report.failed)
    head = ("RESTORE COMPLETE - every supported field was restored or already correct."
            if status == "complete" else
            f"RESTORE PARTIAL - {len(report.failed)} field(s) failed (listed first).")
    await respond_long(app, event, f"{head}\nRollback: .setup {pre['backup_id']}\n\n{report.text(limit=120)}")
