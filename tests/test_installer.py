"""Installer: minimal config, validation, no silent overwrites, session reuse,
idempotent re-runs and no secrets in the output."""
from __future__ import annotations

import uuid

import pytest

from fakes import FakeAccount, FakeClient, FakeChannel
from qr import installer, ui
from qr.config import settings as config
from qr.config.validation import database_name, redact_mongo_url, validate_mongo_url

SECRET_HASH = "0123456789abcdef0123456789abcdef"
SECRET_PASS = "S3cr3tPassw0rd"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "ENV_EXAMPLE_PATH", tmp_path / ".env.example")
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(installer, "GITIGNORE", tmp_path / ".gitignore")
    for key in config.REQUIRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_missing_configuration_is_detected(workdir):
    assert installer.missing_keys({}) == ["MONGO_URL", "API_ID", "API_HASH"]
    assert installer.missing_keys({"MONGO_URL": "mongodb://h/", "API_ID": "1"}) == ["API_HASH"]
    with pytest.raises(SystemExit, match="Missing configuration: MONGO_URL, API_ID, API_HASH"):
        config.load()


@pytest.mark.parametrize("url,fragment", [
    ("", "empty"),
    ("http://cluster.example.net", "must start with mongodb://"),
    ("mongodb+srv://YOUR_CONNECTION_STRING", "placeholder"),
    ("mongodb+srv://user:pw@localhost", "full host name"),
    ("mongodb+srv://u:pw@cluster0.ab.mongodb.net:27017", "must not include a port"),
    ("mongodb://user:pw@host:notaport/db", "not a valid MongoDB URI"),
    ("mongodb://host/db name", "spaces"),
])
def test_invalid_mongo_urls_give_readable_errors(url, fragment):
    problem = validate_mongo_url(url)
    assert problem and fragment in problem
    assert "pw@" not in problem


def test_valid_urls_and_database_name_rules():
    assert validate_mongo_url("mongodb+srv://u:p@cluster0.abcde.mongodb.net/shop?retryWrites=true") is None
    assert validate_mongo_url("mongodb://localhost:27017") is None
    assert database_name("mongodb+srv://u:p@cluster0.abcde.mongodb.net/shop?retryWrites=true") == "shop"
    assert database_name("mongodb://localhost:27017/") == config.DEFAULT_DB_NAME
    redacted = redact_mongo_url(f"mongodb+srv://admin:{SECRET_PASS}@cluster0.abcde.mongodb.net/shop")
    assert SECRET_PASS not in redacted and "admin" not in redacted and "cluster0" in redacted


def test_existing_env_is_not_overwritten_without_confirmation(workdir):
    env = config.ENV_PATH
    env.write_text("# keep me\nAPI_ID=111\nEXTRA=1\n", encoding="utf-8")
    asked = []
    changed = installer.write_env(env, {"API_ID": "222", "API_HASH": SECRET_HASH},
                                  confirm=lambda m: asked.append(m) or False)
    assert not changed and asked and "REPLACE API_ID" in asked[0]
    assert env.read_text() == "# keep me\nAPI_ID=111\nEXTRA=1\n"
    changed = installer.write_env(env, {"API_ID": "222", "API_HASH": SECRET_HASH}, confirm=lambda m: True)
    assert changed
    assert env.read_text() == f"# keep me\nAPI_ID=222\nEXTRA=1\nAPI_HASH={SECRET_HASH}\n"
    # identical values: nothing to do, no question asked
    assert not installer.write_env(env, {"API_ID": "222"}, confirm=lambda m: pytest.fail("asked"))


def test_repo_hygiene_steps_are_idempotent(workdir):
    assert installer.ensure_gitignore() == [".env", "sessions/", "*.session", "*.session-journal"]
    assert installer.ensure_gitignore() == []
    assert installer.ensure_env_example() is True
    assert installer.ensure_env_example() is False
    example = config.ENV_EXAMPLE_PATH.read_text()
    assert "MONGO_URL=mongodb+srv://YOUR_CONNECTION_STRING" in example
    assert [l.split("=")[0] for l in example.splitlines() if "=" in l and not l.startswith("#")] == \
        ["MONGO_URL", "API_ID", "API_HASH"]


def test_dependency_conflicts_are_detected_not_silently_replaced():
    rows = installer.dependency_status(
        [{"name": "telethon", "version": "1.36.0"}, {"name": "pymongo", "version": "4.15.5"},
         {"name": "colorama", "version": "0.4.6"}],
        version_of={"telethon": "1.36.0", "pymongo": "4.9.0"}.get)
    by_name = {r["name"]: r for r in rows}
    assert by_name["telethon"]["ok"]
    assert by_name["pymongo"]["conflict"] and not by_name["pymongo"]["ok"]
    assert by_name["colorama"]["installed"] is None


def test_requirements_are_parsed_with_platform_markers():
    names = [r["name"] for r in installer.parse_requirements()]
    assert {"telethon", "pymongo", "colorama"} <= set(names)
    assert ("tzdata" in names) == (installer.sys.platform == "win32")


async def test_existing_authorised_session_is_reused(workdir):
    account = FakeAccount(5)
    started = []

    class Factory(FakeClient):
        def __init__(self, path, api_id, api_hash):
            super().__init__(account)

        async def connect(self):
            return True

        async def is_user_authorized(self):
            return True

        async def start(self, **kwargs):
            started.append(kwargs)

    authorized, me, _client = await installer.check_session(1, SECRET_HASH, client_factory=Factory)
    assert authorized and me.id == 5 and started == []


async def test_check_mongo_reports_bad_url_and_works_with_real_server(mongo_url):
    ok, detail = await installer.check_mongo("mongodb+srv://u:p@bad")
    assert not ok and "full host name" in detail
    url = f"{mongo_url}qrinstall_{uuid.uuid4().hex[:6]}"
    ok, detail = await installer.check_mongo(url)
    assert ok, detail
    from pymongo import AsyncMongoClient
    client = AsyncMongoClient(url)
    names = await client.get_default_database().list_collection_names()
    assert "_qr_healthcheck" not in names           # test data removed
    await client.close()


async def test_full_rerun_reuses_everything_and_prints_no_secrets(workdir, mongo_url, monkeypatch, capsys):
    url = f"mongodb://admin:{SECRET_PASS}@{mongo_url.split('//', 1)[1]}qrrerun?authSource=admin"
    # The throwaway mongod has no auth, so connect with the credential-free URL
    # but keep the secret in .env to prove it is never echoed.
    real = f"{mongo_url}qrrerun"
    config.ENV_PATH.write_text(f"MONGO_URL={url}\nAPI_ID=12345\nAPI_HASH={SECRET_HASH}\n", encoding="utf-8")
    original = installer.check_mongo
    monkeypatch.setattr(installer, "check_mongo", lambda u: original(real))
    account = FakeAccount(7)

    async def fake_session(api_id, api_hash, name=config.DEFAULT_SESSION, client_factory=None):
        client = FakeClient(account, [FakeChannel(-1001000000001, "Archive", "ohthatsthepoint")])
        client.disconnect = lambda: _noop()
        return True, account.user, client

    async def _noop():
        return None

    monkeypatch.setattr(installer, "check_session", fake_session)
    monkeypatch.setattr(installer, "_deps_step", lambda report, interactive=True: report.add("Dependencies", True) or True)
    monkeypatch.setattr("qr.services.legacy_import.candidates", lambda base: [])
    from qr.database import mongo as mongo_module
    real_db = mongo_module.Database
    monkeypatch.setattr(mongo_module, "Database", lambda u, **kw: real_db(real, **kw))
    monkeypatch.setattr(ui, "ask", lambda *a, **k: pytest.fail("installer prompted on a re-run"))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("installer asked to confirm on a re-run"))
    before = config.ENV_PATH.read_text()
    for _ in range(2):
        assert await installer.run_install() == 0
    assert config.ENV_PATH.read_text() == before
    output = capsys.readouterr().out
    assert SECRET_PASS not in output and SECRET_HASH not in output
    assert "QuickReply is ready" in output
