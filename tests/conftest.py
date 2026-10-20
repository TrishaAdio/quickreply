"""Test fixtures.

MongoDB tests use a real server: set QR_TEST_MONGO_URL, or put ``mongod`` on
PATH / point QR_TEST_MONGOD at the binary and a throwaway instance is started.
Without either, MongoDB-backed tests are skipped (pure-logic tests still run).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeAccount, FakeChannel, FakeClient  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def mongo_url(tmp_path_factory):
    url = os.environ.get("QR_TEST_MONGO_URL")
    if url:
        yield url
        return
    binary = os.environ.get("QR_TEST_MONGOD") or shutil.which("mongod")
    if not binary:
        pytest.skip("MongoDB not available (set QR_TEST_MONGO_URL or QR_TEST_MONGOD)")
    dbpath = tmp_path_factory.mktemp("mongo")
    port = _free_port()
    process = subprocess.Popen(
        [binary, "--dbpath", str(dbpath), "--port", str(port), "--bind_ip", "127.0.0.1", "--quiet",
         "--setParameter", "ttlMonitorSleepSecs=3600"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        process.kill()
        pytest.skip("mongod did not start")
    yield f"mongodb://127.0.0.1:{port}/"
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture
async def db(mongo_url):
    from qr.database.mongo import Database

    database = Database(mongo_url, db_name=f"qrtest_{uuid.uuid4().hex[:10]}", server_timeout_ms=5000)
    await database.ping()
    await database.init()
    yield database
    await database.client.drop_database(database.db_name)
    await database.close()


@pytest.fixture
def account():
    return FakeAccount(111)


@pytest.fixture
def archive_channel():
    return FakeChannel(-1001000000001, "Archive", "ohthatsthepoint")


@pytest.fixture
def post_channel():
    return FakeChannel(-1001000000002, "Posts", "newpostchannel")


@pytest.fixture
def client(account, archive_channel, post_channel):
    return FakeClient(account, [archive_channel, post_channel])


def make_app(client, db, account):
    from qr.app import App

    app = App(client, db, self_id=account.user.id, me=account.user)
    app.slept = []

    async def fake_sleep(seconds):
        app.slept.append(seconds)

    app.sleep = fake_sleep
    return app


@pytest.fixture
async def app(client, db, account):
    application = make_app(client, db, account)
    await application.load_settings()
    await application.register_account()
    return application
