import string

import pytest
from starlette.websockets import WebSocketDisconnect

from app.game import manager as manager_module
from app.game.manager import manager
from app.schemas.payloads import *
from tests.helpers import FakeWebSocket, chat, make_room, party, recv

pytestmark = pytest.mark.anyio

SETTINGS = UpdateSettingsPayload(type="settings", mode="words", time_setting=30,
                                 word_count=50, language="spanish", quote_length="long")


# ------------------------- creating rooms

async def test_create_party_returns_six_letter_code_and_makes_creator_host():
    ws = FakeWebSocket()
    room = await manager.create_party(ws, "p1")
    assert len(room) == 6 and set(room) <= set(string.ascii_uppercase)
    state = manager.rooms[room]
    assert state["host"] == "p1"
    assert state["status"] == "waiting"
    assert state["websockets"] == [ws]
    assert state["players"]["p1"] == {"cursor": 0, "completed_words": 0, "wpm": 0,
                                      "correct_chars": 0, "accuracy": 0, "finished": False}
    assert state["afk_task"] is not None


async def test_create_party_retries_on_code_collision(monkeypatch):
    first = await manager.create_party(FakeWebSocket(), "p1")
    codes = iter([list(first), list("ZZZZZZ")])
    monkeypatch.setattr(manager_module.random, "choices", lambda *a, **k: next(codes))
    second = await manager.create_party(FakeWebSocket(), "p2")
    assert second == "ZZZZZZ"
    assert manager.rooms[first]["host"] == "p1"  # first room untouched


def test_create_over_socket_sends_join_then_room(client):
    with client.websocket_connect("/ws/party/create?player_id=p1") as ws:
        assert recv(ws) == {"type": "message", "sender": "server",
                            "message": "Player p1 has joined the party"}
        room = recv(ws)
        assert room["type"] == "room"
        assert room["host"] == "p1"
        assert room["status"] == "waiting"
        assert list(room["players"]) == ["p1"]
        assert room["room"] in manager.rooms


# ------------------------- joining

async def test_join_nonexistent_room_fails():
    error = await manager.connect(FakeWebSocket(), "NOROOM", "p1")
    assert error == ErrorPayload(type="error", message="Party ID not found...")
    assert "NOROOM" not in manager.rooms


@pytest.mark.parametrize("status", ["countdown", "active"])
async def test_join_only_while_waiting(status):
    room, _ = await make_room("p1")
    manager.rooms[room]["status"] = status
    error = await manager.connect(FakeWebSocket(), room, "p2")
    assert error.message == "Game still in progress..."
    assert list(manager.rooms[room]["players"]) == ["p1"]


async def test_duplicate_player_name_rejected():
    room, sockets = await make_room("p1")
    error = await manager.connect(FakeWebSocket(), room, "p1")
    assert isinstance(error, ErrorPayload)
    assert "already in party" in error.message
    assert manager.rooms[room]["websockets"] == [sockets["p1"]]


async def test_join_adds_player_and_bumps_activity():
    room, _ = await make_room("p1")
    manager.rooms[room]["last_activity"] = 0
    ws = FakeWebSocket()
    assert await manager.connect(ws, room, "p2") is None
    assert list(manager.rooms[room]["players"]) == ["p1", "p2"]
    assert ws in manager.rooms[room]["websockets"]
    assert manager.rooms[room]["last_activity"] > 0


def test_join_nonexistent_room_over_socket_sends_error_and_closes(client):
    with client.websocket_connect("/ws/party/NOROOM/p1") as ws:
        assert recv(ws) == {"type": "error", "message": "Party ID not found..."}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_duplicate_name_over_socket_rejected_without_touching_room(client):
    with party(client, "p1") as (room_id, ws):
        with client.websocket_connect(f"/ws/party/{room_id}/p1") as dupe:
            assert recv(dupe)["type"] == "error"
            with pytest.raises(WebSocketDisconnect):
                dupe.receive_text()
        # the original p1 got no join broadcast for the rejected socket
        ws["p1"].send_text(chat("marker"))
        assert recv(ws["p1"])["message"] == "marker"
        assert len(manager.rooms[room_id]["websockets"]) == 1


def test_join_broadcasts_to_existing_players(client):
    with party(client, "p1") as (room_id, ws):
        with client.websocket_connect(f"/ws/party/{room_id}/p2"):
            assert recv(ws["p1"])["message"] == "Player p2 has joined the party"
            room = recv(ws["p1"])
            assert list(room["players"]) == ["p1", "p2"]
            assert room["host"] == "p1"


# ------------------------- leaving

async def test_non_host_leaving_keeps_host_and_notifies_others():
    room, sockets = await make_room("p1", "p2")
    await manager.disconnect(sockets["p2"], room, "p2")
    assert list(manager.rooms[room]["players"]) == ["p1"]
    assert manager.rooms[room]["host"] == "p1"
    left, update = sockets["p1"].sent
    assert left == {"type": "message", "sender": "server", "message": "p2 has left the party :("}
    assert update["type"] == "room" and list(update["players"]) == ["p1"]
    assert sockets["p2"].sent == []  # already removed before the broadcast


async def test_host_leaving_promotes_next_joined_player():
    room, sockets = await make_room("p1", "p2", "p3")
    await manager.disconnect(sockets["p1"], room, "p1")
    assert manager.rooms[room]["host"] == "p2"
    assert sockets["p3"].of_type("room")[-1]["host"] == "p2"


async def test_last_player_leaving_deletes_room():
    room, sockets = await make_room("p1", "p2")
    await manager.disconnect(sockets["p1"], room, "p1")
    await manager.disconnect(sockets["p2"], room, "p2")
    assert room not in manager.rooms


async def test_disconnect_from_deleted_room_is_noop():
    await manager.disconnect(FakeWebSocket(), "GONE00", "p1")


def test_host_disconnect_over_socket_promotes_next_player(client):
    with party(client, "p1", "p2", "p3") as (room_id, ws):
        ws["p1"].close()
        assert recv(ws["p2"])["message"] == "p1 has left the party :("
        room = recv(ws["p2"])
        assert room["host"] == "p2"
        assert list(room["players"]) == ["p2", "p3"]


# ------------------------- host-only settings

async def test_host_settings_change_applied_and_broadcast():
    room, sockets = await make_room("p1", "p2")
    await manager.host_change_settings(room, "p1", SETTINGS)
    state = manager.rooms[room]
    assert (state["mode"], state["time_setting"], state["word_count"],
            state["language"], state["quote_length"]) == ("words", 30, 50, "spanish", "long")
    for ws in sockets.values():
        (update,) = ws.sent
        assert update["mode"] == "words" and update["word_count"] == 50
        assert update["language"] == "spanish" and update["quote_length"] == "long"


async def test_non_host_cannot_change_settings():
    room, sockets = await make_room("p1", "p2")
    await manager.host_change_settings(room, "p2", SETTINGS)
    assert manager.rooms[room]["mode"] == "time"
    assert all(ws.sent == [] for ws in sockets.values())


@pytest.mark.parametrize("status", ["countdown", "active"])
async def test_settings_locked_once_race_starts(status):
    room, sockets = await make_room("p1")
    manager.rooms[room]["status"] = status
    await manager.host_change_settings(room, "p1", SETTINGS)
    assert manager.rooms[room]["mode"] == "time"
    assert sockets["p1"].sent == []


async def test_non_host_cannot_start_race():
    room, sockets = await make_room("p1", "p2")
    await manager.handle_host_start(room, "p2", StartPayload(type="start", text="sneaky"))
    assert manager.rooms[room]["status"] == "waiting"
    assert manager.rooms[room]["text"] != "sneaky"
    assert all(ws.sent == [] for ws in sockets.values())


def test_settings_change_over_socket_reaches_every_player(client):
    with party(client, "p1", "p2") as (_, ws):
        ws["p1"].send_text(SETTINGS.model_dump_json())
        for player in ("p1", "p2"):
            room = recv(ws[player])
            assert room["type"] == "room"
            assert room["mode"] == "words" and room["time_setting"] == 30


def test_non_host_settings_over_socket_not_broadcast(client):
    with party(client, "p1", "p2") as (room_id, ws):
        ws["p2"].send_text(SETTINGS.model_dump_json())
        ws["p2"].send_text(chat("marker"))
        # no room update arrived before the marker
        assert recv(ws["p1"])["message"] == "marker"
        assert manager.rooms[room_id]["mode"] == "time"
