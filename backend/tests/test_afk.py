import asyncio
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.game.manager import finalize_race, manager
from tests.helpers import make_room, party, recv, wait_for

pytestmark = pytest.mark.anyio

TIMED_OUT = {"type": "message", "sender": "server", "message": "Lobby timed out due to inactivity."}


@pytest.fixture
def fast_watchdog(timing):
    timing(AFK_LOBBY_CHECK_INTERVAL_SECONDS=0.01, AFK_LOBBY_TIMEOUT_SECONDS=0.05)


async def test_idle_lobby_closed_and_deleted(fast_watchdog):
    room, sockets = await make_room("p1", "p2")
    await wait_for(lambda: room not in manager.rooms)
    for ws in sockets.values():
        assert ws.sent == [TIMED_OUT]
        assert ws.closed


async def test_activity_keeps_lobby_open(timing):
    timing(AFK_LOBBY_CHECK_INTERVAL_SECONDS=0.01, AFK_LOBBY_TIMEOUT_SECONDS=0.15)
    room, sockets = await make_room("p1")
    for _ in range(10):  # 0.3 s total, twice the timeout, but never idle that long
        await asyncio.sleep(0.03)
        manager.rooms[room]["last_activity"] = time.perf_counter()
    assert room in manager.rooms
    assert sockets["p1"].sent == []
    # and once activity stops, it times out after all
    await wait_for(lambda: room not in manager.rooms)


@pytest.mark.parametrize("status", ["countdown", "active"])
async def test_watchdog_leaves_racing_rooms_alone(fast_watchdog, status):
    room, sockets = await make_room("p1")
    manager.rooms[room]["status"] = status
    await asyncio.sleep(0.1)  # double the timeout
    assert room in manager.rooms
    assert sockets["p1"].sent == []


async def test_watchdog_exits_when_room_deleted(timing):
    timing(AFK_LOBBY_CHECK_INTERVAL_SECONDS=0.01)
    room, sockets = await make_room("p1")
    watchdog = manager.rooms[room]["afk_task"]
    await manager.disconnect(sockets["p1"], room, "p1")
    await wait_for(watchdog.done)
    assert not watchdog.cancelled()  # returned on its own


def test_idle_lobby_over_socket_gets_message_then_closed(client, fast_watchdog):
    with party(client, "p1") as (room_id, ws):
        assert recv(ws["p1"]) == TIMED_OUT
        with pytest.raises(WebSocketDisconnect):
            ws["p1"].receive_text()
        assert room_id not in manager.rooms


@pytest.mark.xfail(strict=True, reason=(
    "BUG: _afk_lobby_watchdog returns (instead of continuing to poll) as soon "
    "as it sees a non-waiting status, so once a room has raced it's never "
    "AFK-closed again, even after it sits idle back in the lobby"))
async def test_lobby_idle_after_a_race_is_closed(fast_watchdog):
    room, _ = await make_room("p1")
    manager.rooms[room]["status"] = "active"
    await asyncio.sleep(0.05)  # let the watchdog see the race
    await finalize_race(room)  # back to "waiting"

    await asyncio.sleep(0.2)  # four times the timeout
    assert room not in manager.rooms
