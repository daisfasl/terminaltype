import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.game import engine
from app.game import manager as manager_module
from app.game.manager import manager


@pytest.fixture
def anyio_backend():
    # async unit tests run through anyio's pytest plugin (anyio ships with
    # starlette), so no pytest-asyncio needed
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_rooms():
    # `manager` is a module-level singleton, so room state would otherwise
    # leak from one test into the next
    manager.rooms.clear()
    yield
    for room in manager.rooms.values():
        for key in ("afk_task", "grace_task"):
            task = room.get(key)
            if task is not None and not task.done():
                try:
                    task.cancel()
                except RuntimeError:
                    pass  # its event loop (a closed TestClient) is already gone
    manager.rooms.clear()


@pytest.fixture(autouse=True)
def instant_countdown(monkeypatch):
    # run_game's countdown is three hard-coded asyncio.sleep(1) calls. Make
    # those instant and pass every other duration (the race timer) through
    # untouched, so tests that care about the race timer should pick a
    # duration other than exactly 1 second.
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        await real_sleep(0 if seconds == 1 else seconds)

    monkeypatch.setattr(engine, "asyncio", SimpleNamespace(sleep=sleep))


@pytest.fixture
def timing(monkeypatch):
    """Overrides the timing constants used by the manager, e.g.
    `timing(GRACE_PERIOD_SECONDS=0.05)`. They're imported by name into
    app.game.manager, so patching app.game.constants would have no effect."""
    def set_values(**values):
        for name, value in values.items():
            monkeypatch.setattr(manager_module, name, value)
    return set_values


@pytest.fixture
def client():
    # one TestClient (one event loop) shared by every socket in a test, like
    # the single uvicorn process the backend runs as in production
    with TestClient(app) as test_client:
        yield test_client
