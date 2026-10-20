"""One-time import from an old ChannelGuard checkout (read-only on that side).

Reuses what the old ``quickreply.py`` already had, so the operator is not
asked for it again:

* ``API_ID`` / ``API_HASH`` from its ``.env``
* the authorised ``quickreply.session`` (copied, never moved)
* ``data/pay.json`` templates, payment channel and payment log
* ``data/greeting.json`` / ``data/greeted.json``
* legacy tuning keys (SHORTCUT, RIO_PCT, ...) -> per-account MongoDB settings

The keyword / admin-bot reservation data (invite links, bridge requests) is
deliberately not imported: that feature does not exist in QuickReply.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from pymongo.errors import DuplicateKeyError

from qr.config.settings import SESSIONS_DIR, read_env_file
from qr.database.mongo import utcnow
from qr.services.entities import deserialize_entities, serialize_entities, substitute

LEGACY_SESSION = "quickreply.session"
_BOOL_FALSE = ("0", "false", "no", "")


def looks_like_legacy(path: Path) -> bool:
    """A ChannelGuard checkout (never QuickReply itself, which has ``qr/``)."""
    path = Path(path)
    if not (path / "quickreply.py").is_file() or (path / "qr").is_dir():
        return False
    signature = any((path / name).exists() for name in ("guard.py", "linkstore.py", "runtime_lock.py"))
    has_data = (path / "data" / "pay.json").exists() or (path / LEGACY_SESSION).exists()
    return signature or has_data


def candidates(base: Path) -> list[Path]:
    """ChannelGuard checkouts near QuickReply: its parents (QuickReply cloned
    *inside* ChannelGuard) and their sibling folders."""
    found = set()
    roots = []
    starts = [Path(base).resolve()]
    try:
        starts.append(Path.cwd().resolve())
    except OSError:  # working directory was deleted
        pass
    for start in starts:
        roots.extend([start, *list(start.parents)[:2]])
    for root in dict.fromkeys(roots):
        if looks_like_legacy(root):
            found.add(root)
        try:
            for child in root.iterdir():
                if child.is_dir() and looks_like_legacy(child):
                    found.add(child)
        except OSError:
            continue
    return sorted(found)


def summary(path: Path) -> dict:
    """What an import would bring over (read-only)."""
    path = Path(path)
    pay = _read_json(path / "data" / "pay.json", {})
    payments = pay.get("payments") if isinstance(pay, dict) else []
    payments = [p for p in payments if isinstance(p, dict)] if isinstance(payments, list) else []
    greeted = _read_json(path / "data" / "greeted.json", [])
    env = read_env_file(path / ".env")
    return {
        "path": str(path),
        "credentials": bool(env.get("API_ID") and env.get("API_HASH")),
        "session": (path / LEGACY_SESSION).exists(),
        "payments": len(payments),
        "payments_valid": sum(1 for p in payments if p.get("status", "valid") == "valid"),
        "templates": [k for k in ("done", "channel") if isinstance(pay, dict) and pay.get(f"{k}_template")],
        "payment_channel": (pay.get("post_channel") if isinstance(pay, dict) else None) or env.get("PAYMENT_CHANNEL"),
        "greeting": (path / "data" / "greeting.json").exists(),
        "greeted": len(greeted) if isinstance(greeted, list) else 0,
    }


def probe_lock(lock_path: Path) -> tuple[bool, str]:
    """``(held, holder)`` for another process's lock file, without writing to it.

    ``holder`` is the ``pid=... cwd=...`` line ChannelGuard writes into its locks.
    """
    import os

    lock_path = Path(lock_path)
    if not lock_path.exists():
        return False, ""
    try:
        handle = open(lock_path, "rb")
    except OSError:
        return False, ""
    try:
        holder = handle.read(300).replace(b"\0", b"").decode("utf-8", "replace").strip()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBRLCK, 1)
            except OSError:
                return True, holder
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False, ""
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True, holder
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False, ""
    finally:
        handle.close()


def legacy_holder(path: Path) -> tuple[bool, str]:
    """Is the old quickreply.py running from this ChannelGuard folder?"""
    return probe_lock(Path(path) / "data" / "quickreply.lock")


def legacy_running(path: Path) -> bool:
    return legacy_holder(path)[0]


def legacy_account_holder(account_id: int) -> tuple[bool, str]:
    """Is an old ChannelGuard quickreply.py running for this Telegram account
    (from any folder)? It keeps this host-wide lock while it runs."""
    import tempfile

    return probe_lock(Path(tempfile.gettempdir()) / f"channelguard-quickreply-account-{int(account_id)}.lock")


def find_running_legacy(base: Path, account_id: int | None = None) -> str | None:
    """Description of a running old quickreply.py, or None."""
    if account_id is not None:
        held, holder = legacy_account_holder(account_id)
        if held:
            return holder or "unknown process"
    for path in candidates(base):
        held, holder = legacy_holder(path)
        if held:
            return holder or f"process using {path}"
    return None


def stop_hint(holder: str) -> str:
    match = re.search(r"pid=(\d+)", holder or "")
    if match:
        return f"Stop it with:  kill {match.group(1)}   (it runs as {holder})"
    return "Find it with:  ps aux | grep quickreply.py   then stop it with kill <PID>"


def legacy_credentials(path: Path) -> dict:
    values = read_env_file(Path(path) / ".env")
    return {key: values[key] for key in ("API_ID", "API_HASH") if values.get(key)}


def copy_session(path: Path, session_name: str = "quickreply") -> str:
    """Copy the legacy session if we do not already have one. Returns a status."""
    source = Path(path) / LEGACY_SESSION
    target = SESSIONS_DIR / f"{session_name}.session"
    if not source.exists():
        return "no legacy session file"
    if target.exists():
        return "kept existing QuickReply session"
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return f"copied {source.name} -> sessions/{target.name}"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def settings_from_legacy(path: Path) -> tuple[dict, str]:
    env = read_env_file(Path(path) / ".env")
    out: dict = {}
    if env.get("SHORTCUT"):
        out["shortcut"] = env["SHORTCUT"].strip().lstrip("/")
    for key, target in (("GREET_NEW", "greet_new"), ("SWAP_GREETING", "swap_greeting"),
                        ("USERBOT_RELAY_LINK", "relay_link")):
        if key in env:
            out[target] = env[key].strip().lower() not in _BOOL_FALSE
    for key, target, default in (("RIO_PCT", "rio_pct", 55.0), ("MARCO_PCT", "marco_pct", 45.0),
                                 ("ONLINE_MINUTES", "online_minutes", 2.0)):
        if env.get(key):
            out[target] = _float(env[key], default)
    if env.get("SHARE_BASE", "").strip().lower() in ("today", "transaction"):
        out["share_base"] = env["SHARE_BASE"].strip().lower()
    if env.get("ORDER_PREFIX"):
        out["order_prefix"] = env["ORDER_PREFIX"].strip().upper()[:8]
    if _int_or_none(env.get("ORDER_ID_LENGTH")):
        out["order_id_length"] = max(3, min(16, int(env["ORDER_ID_LENGTH"])))
    channel = _int_or_none(env.get("PAYMENT_CHANNEL"))
    if channel is not None:
        out["payment_channel"] = channel
    return out, env.get("LINK_SOURCE", "").strip()


async def import_legacy(app, path: Path, *, client=None) -> list[str]:
    """Import into the account ``app.self_id``. Safe to run repeatedly."""
    path = Path(path)
    report = []
    values, link_source = settings_from_legacy(path)

    pay = _read_json(path / "data" / "pay.json", {})
    if isinstance(pay, dict):
        for kind in ("done", "channel"):
            if isinstance(pay.get(f"{kind}_template"), str):
                text = pay[f"{kind}_template"]
                entities = deserialize_entities(pay.get(f"{kind}_entities") or [])
                if "{link}" in text:
                    # Keyword/bot links are gone; drop the token, keep formatting aligned.
                    text, entities = substitute(text, entities, {"{link}": ""})
                    report.append(f"{kind} template: removed the {{link}} token (keyword links no longer exist)")
                values[f"{kind}_template"] = text
                values[f"{kind}_entities"] = serialize_entities(entities)
        channel = _int_or_none(pay.get("post_channel"))
        if channel is not None:
            values["payment_channel"] = channel
    greeting = _read_json(path / "data" / "greeting.json", None)
    if isinstance(greeting, dict) and "chat_id" in greeting and "message_id" in greeting:
        values["greeting"] = {"chat_id": int(greeting["chat_id"]), "message_id": int(greeting["message_id"])}
    if link_source and client is not None:
        try:
            ref = int(link_source) if link_source.lstrip("-").isdigit() else link_source
            values["link_source"] = int((await client.get_entity(ref)).id)
        except Exception:  # noqa: BLE001
            report.append(f"LINK_SOURCE {link_source!r} could not be resolved; set it with .linksource")
    if values:
        await app.set(**values)
        report.append("settings imported: " + ", ".join(sorted(values)))

    greeted = _read_json(path / "data" / "greeted.json", [])
    added = 0
    for user_id in greeted if isinstance(greeted, list) else []:
        uid = _int_or_none(user_id)
        if uid is None:
            continue
        result = await app.db.greeted.update_one(
            {"account_id": app.self_id, "user_id": uid},
            {"$setOnInsert": {"account_id": app.self_id, "user_id": uid}}, upsert=True)
        added += 1 if result.upserted_id else 0
    if greeted:
        report.append(f"greeted users: {added} new of {len(greeted)}")

    payments = pay.get("payments") if isinstance(pay, dict) else []
    imported = skipped = 0
    for raw in payments if isinstance(payments, list) else []:
        if not isinstance(raw, dict) or not raw.get("order_id"):
            skipped += 1
            continue
        doc = {
            "account_id": app.self_id,
            "order_id": str(raw["order_id"]),
            "amount": str(raw.get("amount", "0")),
            "name": str(raw.get("name") or ""),
            "ts": _float(raw.get("ts"), 0.0),
            "status": str(raw.get("status") or "valid"),
            "source_chat_id": _int_or_none(raw.get("source_chat_id")),
            "source_message_id": _int_or_none(raw.get("source_message_id")),
            "post_chat_id": _int_or_none(raw.get("post_chat_id")),
            "post_message_id": _int_or_none(raw.get("post_message_id")),
            # Never auto-post historical receipts after an import.
            "post_status": "posted" if raw.get("post_message_id") else "imported_unposted",
            "imported_from": "channelguard",
            "created_at": utcnow(),
        }
        try:
            await app.db.payments.insert_one(doc)
            imported += 1
        except DuplicateKeyError:
            skipped += 1
    if payments:
        report.append(f"payments: {imported} imported, {skipped} already present/skipped")
    return report
