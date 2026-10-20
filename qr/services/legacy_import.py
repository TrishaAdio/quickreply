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
import shutil
from pathlib import Path

from pymongo.errors import DuplicateKeyError

from qr.config.settings import SESSIONS_DIR, read_env_file
from qr.database.mongo import utcnow
from qr.services.entities import deserialize_entities, serialize_entities, substitute

LEGACY_SESSION = "quickreply.session"
_BOOL_FALSE = ("0", "false", "no", "")


def looks_like_legacy(path: Path) -> bool:
    path = Path(path)
    return (path / "quickreply.py").exists() and (
        (path / ".env").exists() or (path / LEGACY_SESSION).exists() or (path / "data").exists())


def candidates(base: Path) -> list[Path]:
    found = []
    for parent in {Path(base).resolve().parent, Path.cwd().resolve().parent}:
        try:
            for child in parent.iterdir():
                if child.is_dir() and looks_like_legacy(child):
                    found.append(child)
        except OSError:
            continue
    return sorted(set(found))


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
