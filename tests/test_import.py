"""`python setup.py import`: find ChannelGuard (also when QuickReply is cloned
inside it), refuse while the old bot runs, import once, never touch ChannelGuard."""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from fakes import FakeAccount, FakeClient
from qr import installer, ui
from qr.config import settings as config
from qr.services import legacy_import
from test_commands import _legacy_dir

SECRET_HASH = "0123456789abcdef0123456789abcdef"


def tree_digest(root, skip=None):
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if skip is not None and skip in path.parents:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def make_quickreply(path):
    (path / "qr").mkdir(parents=True)
    (path / "quickreply.py").write_text("# new")
    return path


def test_finds_channelguard_when_quickreply_is_inside_it(tmp_path, monkeypatch):
    channelguard = _legacy_dir(tmp_path)                 # ~/channelguard
    nested = make_quickreply(channelguard / "quickreply")   # ~/channelguard/quickreply
    monkeypatch.chdir(nested)
    assert legacy_import.candidates(nested) == [channelguard]


def test_finds_sibling_channelguard_and_never_quickreply_itself(tmp_path, monkeypatch):
    channelguard = _legacy_dir(tmp_path)
    sibling = make_quickreply(tmp_path / "QuickReply")
    monkeypatch.chdir(sibling)
    assert legacy_import.candidates(sibling) == [channelguard]
    assert not legacy_import.looks_like_legacy(sibling)


def test_summary_counts_what_would_be_imported(tmp_path):
    info = legacy_import.summary(_legacy_dir(tmp_path))
    assert info["credentials"] and info["session"]
    assert info["payments"] == 3 and info["payments_valid"] == 3
    assert info["templates"] == ["done"] and info["payment_channel"] == -1001888
    assert info["greeting"] and info["greeted"] == 3


@pytest.mark.skipif(os.name == "nt", reason="flock probe")
def test_running_detection_is_read_only(tmp_path):
    import fcntl

    root = _legacy_dir(tmp_path)
    lock = root / "data" / "quickreply.lock"
    lock.write_text("pid=1 cwd=/old\n")
    assert not legacy_import.legacy_running(root)
    holder = open(lock, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # the old bot is running
    try:
        assert legacy_import.legacy_running(root)
    finally:
        holder.close()
    assert lock.read_text() == "pid=1 cwd=/old\n"            # probe never writes


@pytest.fixture
def nested(tmp_path, monkeypatch):
    channelguard = _legacy_dir(tmp_path)
    quickreply = make_quickreply(channelguard / "quickreply")
    monkeypatch.setattr(config, "BASE_DIR", quickreply)
    monkeypatch.setattr(config, "ENV_PATH", quickreply / ".env")
    monkeypatch.setattr(config, "SESSIONS_DIR", quickreply / "sessions")
    monkeypatch.setattr(legacy_import, "SESSIONS_DIR", quickreply / "sessions")
    monkeypatch.chdir(quickreply)
    for key in config.REQUIRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    return channelguard, quickreply


async def test_import_command_end_to_end(nested, mongo_url, monkeypatch, capsys):
    channelguard, quickreply = nested
    before = tree_digest(channelguard, skip=quickreply)
    url = f"{mongo_url}qrimport_{uuid.uuid4().hex[:6]}"
    (quickreply / ".env").write_text(f"MONGO_URL={url}\n", encoding="utf-8")
    account = FakeAccount(4242, first="Owner", username="owner")

    async def fake_session(api_id, api_hash, name=config.DEFAULT_SESSION, client_factory=None):
        assert (api_id, api_hash) == ("777", SECRET_HASH)       # reused from ChannelGuard's .env
        client = FakeClient(account)

        async def disconnect():
            return None

        client.disconnect = disconnect
        return True, account.user, client

    asked = []
    answers = iter([True, True, True])   # reuse creds?, add them to the existing .env?, import into account?
    monkeypatch.setattr(installer, "check_session", fake_session)
    monkeypatch.setattr(ui, "confirm", lambda message, **k: asked.append(message) or next(answers))
    monkeypatch.setattr(ui, "ask", lambda *a, **k: pytest.fail("asked for a value"))

    assert await installer.run_import() == 0
    assert "Import into Telegram account Owner Smith @owner (id 4242)?" in asked[-1]
    out = capsys.readouterr().out
    assert "payments: 2 imported" in out and "settings imported" in out
    assert SECRET_HASH not in out
    assert (quickreply / "sessions" / "quickreply.session").read_bytes().startswith(b"SQLite format 3")
    assert "API_HASH=" + SECRET_HASH in (quickreply / ".env").read_text()

    from pymongo import AsyncMongoClient
    mongo = AsyncMongoClient(url)
    db = mongo.get_default_database()
    assert await db.payments.count_documents({"account_id": 4242}) == 2
    settings = await db.settings.find_one({"_id": 4242})
    assert settings["payment_channel"] == -1001888 and settings["shortcut"] == "promo"
    assert await db.greeted.count_documents({"account_id": 4242}) == 3
    assert await db.audit.find_one({"action": "legacy_import"})

    # running it again imports nothing new
    answers = iter([True])
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: next(answers))
    assert await installer.run_import() == 0
    assert "payments: 0 imported" in capsys.readouterr().out
    assert await db.payments.count_documents({"account_id": 4242}) == 2
    await mongo.drop_database(db.name)
    await mongo.close()

    # ChannelGuard untouched (quickreply/ is QuickReply's own folder inside it)
    assert tree_digest(channelguard, skip=quickreply) == before


async def test_import_refuses_while_old_bot_runs(nested, monkeypatch, capsys):
    channelguard, _quickreply = nested
    monkeypatch.setattr(legacy_import, "legacy_running", lambda path: True)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("should stop before asking"))
    assert await installer.run_import() == 1
    assert "still running" in capsys.readouterr().out


async def test_import_rejects_a_wrong_path(nested, tmp_path, capsys):
    assert await installer.run_import(str(tmp_path / "nothing-here")) == 1
    assert "does not look like a ChannelGuard folder" in capsys.readouterr().out
