"""Interactive installer logic (used by ``setup.py``).

Kept import-light: only the standard library and :mod:`qr.ui` are needed at
import time, so it runs before ``requirements.txt`` is installed.
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from qr import ui
from qr.config import settings as config
from qr.config.validation import (
    mask_secret,
    redact_mongo_url,
    scrub,
    validate_api_hash,
    validate_api_id,
    validate_mongo_url,
)

MIN_PYTHON = (3, 10)
REQUIREMENTS = config.BASE_DIR / "requirements.txt"
GITIGNORE = config.BASE_DIR / ".gitignore"
GITIGNORE_REQUIRED = (".env", "sessions/", "*.session", "*.session-journal")
ENV_EXAMPLE = (
    "# QuickReply needs exactly three values. Copy to .env (python setup.py does it for you).\n"
    "# MongoDB connection string. The database name in the URI path is used; without one\n"
    "# QuickReply uses the database 'quickreply'. Collections/indexes are created automatically.\n"
    "MONGO_URL=mongodb+srv://YOUR_CONNECTION_STRING\n"
    "# Telegram application credentials from https://my.telegram.org -> API development tools.\n"
    "API_ID=YOUR_TELEGRAM_API_ID\n"
    "API_HASH=YOUR_TELEGRAM_API_HASH\n"
)
VALIDATORS = {"MONGO_URL": validate_mongo_url, "API_ID": validate_api_id, "API_HASH": validate_api_hash}


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
def check_python(version=sys.version_info) -> tuple[bool, str]:
    ok = tuple(version[:2]) >= MIN_PYTHON
    return ok, f"Python {version[0]}.{version[1]}.{version[2]}" + (
        "" if ok else f" - QuickReply needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")


def os_name() -> str:
    return f"{platform.system() or 'unknown'} {platform.release()}".strip()


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==\s*([\w.\-]+))?\s*(?:;\s*(.+))?$")


def parse_requirements(path: Path | None = None) -> list[dict]:
    path = path or REQUIREMENTS
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQ_RE.match(line)
        if not match:
            continue
        marker = match.group(3)
        if marker and "sys_platform" in marker:
            want = re.search(r"sys_platform\s*==\s*['\"](\w+)['\"]", marker)
            if want and want.group(1) != sys.platform:
                continue
        out.append({"name": match.group(1), "version": match.group(2)})
    return out


def dependency_status(requirements: list[dict], version_of=None) -> list[dict]:
    version_of = version_of or _installed_version
    rows = []
    for req in requirements:
        installed = version_of(req["name"])
        rows.append({
            **req, "installed": installed,
            "ok": installed is not None and (req["version"] is None or installed == req["version"]),
            "conflict": installed is not None and req["version"] is not None and installed != req["version"],
        })
    return rows


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def install_dependencies(runner=subprocess.run) -> tuple[bool, str]:
    command = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    try:
        result = runner(command, capture_output=True, text=True)
    except OSError as error:
        return False, f"could not run pip: {error}"
    tail = "\n".join(((result.stdout or "") + (result.stderr or "")).strip().splitlines()[-12:])
    return result.returncode == 0, tail


# ---------------------------------------------------------------------------
# configuration files
# ---------------------------------------------------------------------------
def missing_keys(values: dict) -> list[str]:
    return [key for key in config.REQUIRED_KEYS if not values.get(key)]


def invalid_keys(values: dict) -> dict[str, str]:
    problems = {}
    for key, check in VALIDATORS.items():
        if values.get(key):
            problem = check(values[key])
            if problem:
                problems[key] = problem
    return problems


def write_env(path: Path, updates: dict, *, confirm) -> bool:
    """Merge ``updates`` into ``path``. An existing file is only changed after
    ``confirm(message)`` returns True. Returns True when the file was written."""
    updates = {k: str(v) for k, v in updates.items() if v is not None}
    if not updates:
        return False
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if path.exists():
        current = config.read_env_file(path)
        changing = [k for k in updates if k in current and current[k] != updates[k]]
        adding = [k for k in updates if k not in current]
        if not changing and not adding:
            return False
        parts = []
        if adding:
            parts.append("add " + ", ".join(adding))
        if changing:
            parts.append("REPLACE " + ", ".join(changing))
        if not confirm(f"Update existing .env ({'; '.join(parts)})?"):
            return False
    out, seen = [], set()
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            if key in seen:
                continue
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in updates.items() if k not in seen)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)
    return True


def ensure_env_example(path: Path | None = None) -> bool:
    path = path or config.ENV_EXAMPLE_PATH
    if path.exists():
        return False
    path.write_text(ENV_EXAMPLE, encoding="utf-8")
    return True


def ensure_gitignore(path: Path | None = None) -> list[str]:
    path = path or GITIGNORE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [entry for entry in GITIGNORE_REQUIRED if entry not in lines]
    if missing:
        with path.open("a", encoding="utf-8") as handle:
            if lines and lines[-1].strip():
                handle.write("\n")
            handle.write("# QuickReply secrets\n" + "\n".join(missing) + "\n")
    return missing


def session_file(name: str = config.DEFAULT_SESSION) -> Path:
    return config.SESSIONS_DIR / f"{name}.session"


# ---------------------------------------------------------------------------
# MongoDB + Telegram checks
# ---------------------------------------------------------------------------
async def check_mongo(url: str) -> tuple[bool, str]:
    problem = validate_mongo_url(url)
    if problem:
        return False, problem
    from qr.database.mongo import Database, DatabaseError

    db = Database(url, server_timeout_ms=10000)
    try:
        await db.ping()
        await db.init()
        await db.healthcheck_roundtrip()
        return True, f"connected to {db.redacted_url}; indexes ready; write/read test passed"
    except DatabaseError as error:
        return False, str(error)
    except Exception as error:  # noqa: BLE001
        return False, scrub(f"{type(error).__name__}: {error}", [url])
    finally:
        await db.close()


def _restrict(path: Path, mode: int) -> None:
    """Owner-only permissions for secrets (no-op where unsupported, e.g. Windows ACLs)."""
    try:
        if path.exists():
            os.chmod(path, mode)
    except OSError:
        pass


async def check_session(api_id: int, api_hash: str, name: str = config.DEFAULT_SESSION, client_factory=None):
    """Return ``(authorized, me, client)`` without prompting. Caller disconnects."""
    if client_factory is None:
        from telethon import TelegramClient as client_factory  # noqa: N813
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _restrict(config.SESSIONS_DIR, 0o700)
    client = client_factory(str(config.SESSIONS_DIR / name), int(api_id), api_hash)
    await client.connect()
    _restrict(config.SESSIONS_DIR / f"{name}.session", 0o600)
    if await client.is_user_authorized():
        return True, await client.get_me(), client
    return False, None, client


async def login(client) -> object:
    """Telegram's normal login. Codes/passwords are passed straight to Telegram
    and never stored or printed."""
    phone = ui.ask("Phone number with country code (e.g. +919876543210)")
    await client.start(
        phone=lambda: phone,
        code_callback=lambda: ui.ask("Login code Telegram sent you", secret=True),
        password=lambda: ui.ask("Two-step verification password", secret=True),
    )
    return await client.get_me()


async def check_destination(client, ref) -> tuple[bool, str]:
    from qr.services.telegram_utils import can_post_media, resolve_chat

    try:
        _chat_id, entity, title = await resolve_chat(client, ref)
    except Exception as error:  # noqa: BLE001
        return False, f"{ref}: not accessible ({type(error).__name__})"
    ok, why = await can_post_media(client, entity)
    return ok, f"{title}: {why}"


# ---------------------------------------------------------------------------
# interactive flow
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.rows: list[tuple[str, bool | None, str]] = []

    def add(self, label: str, ok: bool | None, detail: str = "") -> None:
        self.rows.append((label, ok, detail))

    def show(self) -> None:
        ui.heading("Completion report")
        for label, ok, detail in self.rows:
            ui.status_line(label, ok, detail)


def _deps_step(report: Report, interactive: bool = True) -> bool:
    try:
        rows = dependency_status(parse_requirements())
    except OSError as error:
        ui.error(f"requirements.txt unreadable: {error}")
        report.add("Dependencies", False, "requirements.txt missing")
        return False
    for row in rows:
        if row["ok"]:
            ui.success(f"{row['name']} {row['installed']}")
        elif row["conflict"]:
            ui.warn(f"{row['name']} {row['installed']} installed, QuickReply pins {row['version']}")
        else:
            ui.warn(f"{row['name']} missing")
    if all(r["ok"] for r in rows):
        report.add("Dependencies", True, "all installed")
        return True
    conflicts = [r for r in rows if r["conflict"]]
    if conflicts and interactive and not ui.confirm(
            "Installing will REPLACE the versions above that differ. Continue?", default=False):
        report.add("Dependencies", None, "version conflicts left unchanged (consider a virtualenv)")
        return all(r["installed"] for r in rows)
    if interactive and not conflicts and not ui.confirm("Install missing packages now with pip?", default=True):
        report.add("Dependencies", False, "not installed")
        return False
    ui.info("Running pip install -r requirements.txt ...")
    ok, output = install_dependencies()
    if ok:
        ui.success("Dependencies installed.")
        ui.enable_colors()
        report.add("Dependencies", True, "installed")
    else:
        ui.error("pip failed:\n" + output)
        report.add("Dependencies", False, "pip failed - see output above")
    return ok


def _collect_config(report: Report, legacy: Path | None) -> dict | None:
    values = config.read_env_file(config.ENV_PATH)
    if config.ENV_PATH.exists():
        ui.info(".env found - existing values are kept unless you choose to change them.")
    for key, problem in invalid_keys(values).items():
        ui.warn(f"{key} in .env is invalid: {problem}")
    updates: dict[str, str] = {}
    if legacy is not None:
        from qr.services.legacy_import import legacy_credentials

        creds = legacy_credentials(legacy)
        needed = {k: v for k, v in creds.items() if not values.get(k) and not VALIDATORS[k](v)}
        if needed and ui.confirm(f"Reuse {', '.join(needed)} from {legacy}/.env?", default=True):
            updates.update(needed)
    for key in config.REQUIRED_KEYS:
        current = updates.get(key) or values.get(key)
        bad = current and VALIDATORS[key](current)
        if current and not bad:
            ui.success(f"{key}: {redact_mongo_url(current) if key == 'MONGO_URL' else ('set' if key == 'API_ID' else mask_secret(current))}")
            continue
        if key == "MONGO_URL":
            ui.info("Paste your MongoDB connection string (input hidden). Only this one value is needed.")
            updates[key] = ui.ask("MONGO_URL", secret=True, validator=validate_mongo_url)
        elif key == "API_ID":
            ui.info("API_ID / API_HASH come from https://my.telegram.org -> API development tools.")
            updates[key] = ui.ask("API_ID", validator=validate_api_id)
        else:
            updates[key] = ui.ask("API_HASH (input hidden)", secret=True, validator=validate_api_hash)
    if updates:
        written = write_env(config.ENV_PATH, updates, confirm=lambda m: ui.confirm(m, default=False))
        if written:
            ui.success("Saved .env (permissions restricted to your user where supported).")
        elif config.ENV_PATH.exists():
            ui.warn(".env left unchanged; the new values are used for this run only.")
    merged = {**values, **updates}
    still = missing_keys(merged) + list(invalid_keys(merged))
    report.add("Configuration", not still, "MONGO_URL, API_ID, API_HASH valid" if not still
               else "problems: " + ", ".join(still))
    return merged if not still else None


async def _telegram_step(report: Report, values: dict, session: str, legacy: Path | None):
    if legacy is not None:
        from qr.services.legacy_import import copy_session

        ui.info(copy_session(legacy, session))
    existed = session_file(session).exists()
    try:
        authorized, me, client = await check_session(values["API_ID"], values["API_HASH"], session)
    except Exception as error:  # noqa: BLE001
        report.add("Telegram", False, scrub(f"{type(error).__name__}: {error}", [values["API_HASH"]]))
        return None
    if authorized:
        ui.success(f"Existing session reused: {me.first_name} (id {me.id}).")
    else:
        ui.warn("No authorised session yet." if not existed else "Session file exists but is not logged in.")
        if not ui.confirm("Log in to Telegram now?", default=True):
            await client.disconnect()
            report.add("Telegram", None, "not logged in - run setup again or start quickreply.py")
            return None
        try:
            me = await login(client)
        except Exception as error:  # noqa: BLE001
            await client.disconnect()
            report.add("Telegram", False, f"login failed ({type(error).__name__})")
            return None
        ui.success(f"Logged in as {me.first_name} (id {me.id}).")
    try:
        os.chmod(session_file(session), 0o600)
    except OSError:
        pass
    report.add("Telegram", True, f"{me.first_name} (id {me.id})" + (" - Premium" if me.premium else
                                                                    " - no Premium: quick replies unavailable"))
    return client, me


async def run_install(session: str = config.DEFAULT_SESSION) -> int:
    report = Report()
    total = 7
    ui.step(1, total, "Environment check")
    ok, detail = check_python()
    (ui.success if ok else ui.error)(detail)
    ui.info(f"Operating system: {os_name()}")
    ui.info(".env: " + ("found" if config.ENV_PATH.exists() else "not found"))
    ui.info(f"Telegram session: {'found' if session_file(session).exists() else 'not found'} "
            f"(sessions/{session}.session)")
    report.add("Python", ok, detail)
    if not ok:
        report.show()
        return 1
    legacy = None
    try:
        from qr.services.legacy_import import candidates

        found = candidates(config.BASE_DIR)
    except Exception:  # noqa: BLE001
        found = []
    if found:
        ui.info("Found an old ChannelGuard checkout: " + str(found[0]))
        if ui.confirm("Reuse its API credentials, session and QuickReply data?", default=True):
            legacy = found[0]

    ui.step(2, total, "Minimal configuration")
    values = _collect_config(report, legacy)

    ui.step(3, total, "Dependencies")
    deps_ok = _deps_step(report)

    ui.step(4, total, "MongoDB")
    mongo_ok = False
    if values and deps_ok:
        mongo_ok, detail = await check_mongo(values["MONGO_URL"])
        (ui.success if mongo_ok else ui.error)(detail)
        report.add("MongoDB", mongo_ok, detail if mongo_ok else "connection failed - " + detail[:160])
    else:
        report.add("MongoDB", None, "skipped (configuration or dependencies incomplete)")

    ui.step(5, total, "Telegram")
    telegram = None
    if values and deps_ok:
        telegram = await _telegram_step(report, values, session, legacy)
    else:
        report.add("Telegram", None, "skipped")
    if telegram is not None:
        client, me = telegram
        try:
            if mongo_ok:
                from qr.app import App
                from qr.database.mongo import Database

                db = Database(values["MONGO_URL"])
                app = App(client, db, self_id=me.id, me=me)
                await app.load_settings()
                await app.register_account()
                ok, detail = await check_destination(client, app.cfg.get("archive_channel"))
                report.add("Archive channel", ok if ok else None, detail + ("" if ok else
                           " - delivery waits until this account can post there (.setarchive to change)"))
                if app.cfg.get("publish_channel"):
                    ok, detail = await check_destination(client, app.cfg.get("publish_channel"))
                    report.add("Posting channel", ok if ok else None, detail)
                else:
                    report.add("Posting channel", None, "not set yet - use .setpostchannel in Telegram")
                if legacy is not None:
                    from qr.services.legacy_import import import_legacy

                    for line in await import_legacy(app, legacy, client=client):
                        ui.info("import: " + line)
                    report.add("Legacy import", True, str(legacy))
                await db.close()
        finally:
            await client.disconnect()

    ui.step(6, total, "Repository hygiene")
    added = ensure_gitignore()
    created = ensure_env_example()
    ui.success(".gitignore protects .env and session files" + (f" (added {', '.join(added)})" if added else ""))
    ui.success(".env.example " + ("created" if created else "present (placeholders only)"))
    report.add("Repository", True, ".env and sessions/ are git-ignored")

    ui.step(7, total, "Summary")
    report.show()
    ready = all(ok is True for label, ok, _ in report.rows if label in ("Configuration", "Dependencies", "MongoDB", "Telegram"))
    print()
    if ready:
        ui.success("QuickReply is ready. Start it with:  " + ui.bold("python quickreply.py"))
    else:
        ui.warn("Some steps need attention (see above). Re-running setup.py is safe.")
    return 0 if ready else 1


def _pick_legacy(path_arg: str | None):
    from qr.services.legacy_import import candidates, looks_like_legacy

    if path_arg:
        path = Path(path_arg).expanduser().resolve()
        if not looks_like_legacy(path):
            ui.error(f"{path} does not look like a ChannelGuard folder (needs quickreply.py and "
                     "data/pay.json, quickreply.session or guard.py).")
            return None
        return path
    found = candidates(config.BASE_DIR)
    if len(found) == 1:
        return found[0]
    if found:
        ui.info("Several ChannelGuard folders were found:")
        ui.menu([str(item) for item in found])
        return found[ui.choose("Which one", len(found)) - 1]
    ui.warn("No ChannelGuard folder found next to or above QuickReply.")
    raw = ui.ask("Path to your ChannelGuard folder (e.g. ~/channelguard)", allow_empty=True)
    return _pick_legacy(raw) if raw else None


async def run_import(path_arg: str | None = None, session: str = config.DEFAULT_SESSION) -> int:
    """Copy ChannelGuard's QuickReply data into MongoDB (ChannelGuard is only read)."""
    from qr.locks import ProcessLock, account_lock_path, session_lock_path
    from qr.services import legacy_import

    ui.heading("Import ChannelGuard data")
    lines: list[str] = []
    legacy = _pick_legacy(path_arg)
    if legacy is None:
        return 1
    info = legacy_import.summary(legacy)
    ui.info(f"Source: {legacy}")
    ui.status_line("API_ID / API_HASH", info["credentials"] or None, "found" if info["credentials"] else "not found")
    ui.status_line("Telegram session", info["session"] or None, "quickreply.session" if info["session"] else "not found")
    ui.status_line("Payments", True if info["payments"] else None,
                   f"{info['payments']} record(s), {info['payments_valid']} valid")
    ui.status_line("Templates", True if info["templates"] else None, ", ".join(info["templates"]) or "defaults")
    ui.status_line("Payment channel", True if info["payment_channel"] else None,
                   str(info["payment_channel"] or "not set"))
    ui.status_line("Greeting", True if info["greeting"] else None,
                   f"set, {info['greeted']} user(s) already greeted" if info["greeting"] else
                   f"not set, {info['greeted']} user(s) already greeted")
    ui.info("ChannelGuard is only read - nothing there is changed or deleted.")

    held, holder = legacy_import.legacy_holder(legacy)
    if held:
        ui.error("The old ChannelGuard quickreply.py is still running. Stop it first, then run this again.")
        ui.info(legacy_import.stop_hint(holder))
        ui.info("If it comes back after kill, it runs as a service: check  systemctl list-units | grep -i quick")
        return 1

    report = Report()
    values = _collect_config(report, legacy)
    if values is None:
        ui.error("Configuration is incomplete; nothing was imported.")
        return 1
    ui.info(legacy_import.copy_session(legacy, session))

    session_lock = ProcessLock(session_lock_path(str(config.SESSIONS_DIR / session)))
    if not session_lock.acquire():
        ui.error("QuickReply is running with this session. Stop it (Ctrl+C) and run the import again.")
        return 1
    try:
        mongo_ok, detail = await check_mongo(values["MONGO_URL"])
        if not mongo_ok:
            ui.error(detail)
            return 1
        authorized, me, client = await check_session(values["API_ID"], values["API_HASH"], session)
        try:
            if not authorized:
                ui.warn("This session is not logged in.")
                if not ui.confirm("Log in to Telegram now?", default=True):
                    return 1
                me = await login(client)
            account_lock = ProcessLock(account_lock_path(me.id))
            if not account_lock.acquire():
                ui.error("QuickReply is already running for this account. Stop it and run the import again.")
                return 1
            try:
                name = " ".join(p for p in (me.first_name, me.last_name) if p)
                who = f"{name} @{me.username}" if me.username else name
                if not ui.confirm(f"Import into Telegram account {who} (id {me.id})?", default=True):
                    ui.warn("Cancelled - nothing was imported.")
                    return 1
                from qr.app import App
                from qr.database.mongo import Database

                db = Database(values["MONGO_URL"])
                try:
                    await db.init()
                    app = App(client, db, self_id=me.id, me=me)
                    await app.load_settings()
                    await app.register_account()
                    lines = await legacy_import.import_legacy(app, legacy, client=client)
                    await db.record_audit(me.id, "legacy_import", source=str(legacy), result=lines)
                finally:
                    await db.close()
            finally:
                account_lock.close()
        finally:
            await client.disconnect()
    finally:
        session_lock.close()

    ui.heading("Imported")
    for line in lines or ["nothing new to import"]:
        ui.success(line)
    ui.info("Safe to run again: records that already exist are skipped.")
    ui.info("Start QuickReply with:  " + ui.bold("python quickreply.py") + "  then send /stats to check today's numbers.")
    return 0


def run_menu() -> int:
    ui.box("QUICKREPLY CONTROL PANEL", "Backup \u2022 Restore \u2022 Archives" if ui._UNICODE else "Backup - Restore - Archives")
    options = ["Install / Configure", "Check Dependencies", "Test MongoDB Connection",
               "Check Telegram Session", "Import ChannelGuard data", "Run QuickReply", "Exit"]
    while True:
        print()
        ui.menu(options)
        choice = ui.choose("Choose an option", len(options))
        if choice == 1:
            asyncio.run(run_install())
        elif choice == 2:
            _deps_step(Report())
        elif choice == 3:
            values = config.current_values()
            if not values.get("MONGO_URL"):
                ui.error("MONGO_URL is not configured - choose Install / Configure first.")
                continue
            ok, detail = asyncio.run(check_mongo(values["MONGO_URL"]))
            (ui.success if ok else ui.error)(detail)
        elif choice == 4:
            values = config.current_values()
            if missing_keys(values):
                ui.error("API_ID / API_HASH missing - choose Install / Configure first.")
                continue

            async def probe(values=values):
                authorized, me, client = await check_session(values["API_ID"], values["API_HASH"])
                await client.disconnect()
                return authorized, me
            try:
                authorized, me = asyncio.run(probe())
                if authorized:
                    ui.success(f"Telegram session authorized: {me.first_name} (id {me.id})")
                else:
                    ui.warn("Telegram session is not logged in - choose Install / Configure.")
            except Exception as error:  # noqa: BLE001
                ui.error(scrub(f"Telegram check failed: {type(error).__name__}: {error}",
                               [values.get("API_HASH")]))
        elif choice == 5:
            asyncio.run(run_import())
        elif choice == 6:
            from quickreply import main as run_main

            return run_main([])
        else:
            return 0
