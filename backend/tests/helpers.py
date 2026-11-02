import asyncio
import json
import time
from contextlib import ExitStack, contextmanager

import pytest

from app.game.manager import manager


# ------------------------- WebSocket (TestClient) helpers

def recv(ws) -> dict:
    return json.loads(ws.receive_text())


def drain_until(ws, predicate, limit: int = 50) -> dict:
    """Reads messages until one matches `predicate`, discarding the rest."""
    for _ in range(limit):
        msg = recv(ws)
        if predicate(msg):
            return msg
    pytest.fail("expected message never arrived")


def room_with_status(status: str):
    return lambda m: m["type"] == "room" and m["status"] == status


def chat(text: str) -> str:
    return json.dumps({"type": "message", "sender": "user", "message": text})


@contextmanager
def party(client, *player_ids: str):
    """Opens a room hosted by player_ids[0] and joins the rest, consuming the
    join/room broadcasts so every socket starts with an empty inbox. Yields
    (room_id, {player_id: ws})."""
    with ExitStack() as stack:
        host = player_ids[0]
        host_ws = stack.enter_context(client.websocket_connect(f"/ws/party/create?player_id={host}"))
        assert recv(host_ws)["type"] == "message"
        room_id = recv(host_ws)["room"]
        sockets = {host: host_ws}
        for player_id in player_ids[1:]:
            sockets[player_id] = stack.enter_context(client.websocket_connect(f"/ws/party/{room_id}/{player_id}"))
            for ws in sockets.values():
                assert recv(ws)["type"] == "message"
                assert recv(ws)["type"] == "room"
        yield room_id, sockets


# ------------------------- ConnectionManager unit-test helpers

class FakeWebSocket:
    """Just enough of starlette's WebSocket for ConnectionManager."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self):
        pass

    async def send_text(self, data: str):
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000):
        self.closed = True

    def of_type(self, payload_type: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == payload_type]


async def make_room(*player_ids: str) -> tuple[str, dict[str, FakeWebSocket]]:
    """Creates a room hosted by player_ids[0] with the rest joined, with
    every fake socket's inbox cleared."""
    sockets = {pid: FakeWebSocket() for pid in player_ids}
    room = await manager.create_party(sockets[player_ids[0]], player_ids[0])
    for pid in player_ids[1:]:
        assert await manager.connect(sockets[pid], room, pid) is None
    for ws in sockets.values():
        ws.sent.clear()
    return room, sockets


async def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while not condition():
        if time.perf_counter() > deadline:
            pytest.fail("condition never became true")
        await asyncio.sleep(0.005)
