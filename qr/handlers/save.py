""".save and .backups"""
from __future__ import annotations

from qr.database import backups as repo
from qr.services import profile_backup
from qr.services.entities import split_plain_text


async def respond_long(app, event, text: str) -> None:
    chunks = split_plain_text(text, limit=3900)
    await app.ack(event, chunks[0])
    for chunk in chunks[1:]:
        await event.respond(chunk)


async def cmd_save(app, event) -> None:
    await app.ack(event, "Saving a full backup to MongoDB... (profile photos and quick reply "
                         "media are downloaded, this can take a minute)")
    result = await profile_backup.create_backup(app, kind="manual")
    if result["unchanged"]:
        head = (f"Backup verified - nothing changed since backup {result['backup_id']}, "
                "so no duplicate was stored.")
    elif result["status"] == "complete":
        head = f"Backup {result['backup_id']} saved and verified (complete)."
    else:
        head = (f"Backup {result['backup_id']} saved but PARTIAL - some fields failed "
                "(see FAILED below).")
    await respond_long(app, event, head + "\n\n" + result["summary"])


async def cmd_backups(app, event) -> None:
    rows = await repo.recent(app.db, None, limit=15)
    if not rows:
        await app.ack(event, "No backups yet. Send .save in Saved Messages.")
        return
    lines = ["Backups (newest first):"]
    for row in rows:
        profile = row.get("profile") or {}
        who = profile.get("first_name") or ""
        if profile.get("username"):
            who += f" @{profile['username']}"
        lines.append(f"{row['_id']}  {row['created_at']:%Y-%m-%d %H:%M} UTC  account {row['account_id']} "
                     f"{who}  [{row['kind']}, {row['status']}]")
    lines.append("\nRestore onto this account: .setup <account id|backup id>")
    await respond_long(app, event, "\n".join(lines))
