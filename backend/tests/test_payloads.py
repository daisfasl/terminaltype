import json

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.payloads import *
from tests.helpers import chat, party, recv

adapter = TypeAdapter(Payload)

SETTINGS = {"type": "settings", "mode": "time", "time_setting": 60, "word_count": 25,
            "language": "english", "quote_length": "medium"}

VALID_FRAMES = [
    ({"type": "progress", "cursor": 5, "completed_words": 1, "correct_chars": 4}, ProgressPayload),
    ({"type": "start", "text": "the quick brown fox"}, StartPayload),
    ({"type": "finish"}, FinishPayload),
    (SETTINGS, UpdateSettingsPayload),
    ({"type": "room", "room": "ABCDEF", "mode": "words", "status": "waiting", "time_setting": 60,
      "word_count": 25, "language": "english", "quote_length": "short", "text": "hi",
      "quote_source": None, "host": "p1",
      "players": {"p1": {"cursor": 0, "completed_words": 0, "wpm": 0, "correct_chars": 0,
                         "accuracy": 0, "finished": False}}}, RoomPayload),
    ({"type": "countdown", "value": 3}, CountdownPayload),
    ({"type": "message", "sender": "user", "message": "gl hf"}, MessagePayload),
    ({"type": "end", "leaderboard": []}, GameEndPayload),
    ({"type": "error", "message": "nope"}, ErrorPayload),
]


def settings(**overrides) -> dict:
    return {**SETTINGS, **overrides}


# ------------------------- discriminated union

@pytest.mark.parametrize("frame, expected", VALID_FRAMES, ids=[f["type"] for f, _ in VALID_FRAMES])
def test_each_message_type_parses_to_its_model(frame, expected):
    payload = adapter.validate_python(frame)
    assert type(payload) is expected


@pytest.mark.parametrize("frame, expected", VALID_FRAMES, ids=[f["type"] for f, _ in VALID_FRAMES])
def test_server_json_round_trips(frame, expected):
    # what the server sends (model_dump_json) must parse back to the same model
    payload = adapter.validate_python(frame)
    assert adapter.validate_json(payload.model_dump_json()) == payload


def test_start_quote_source_defaults_to_none():
    payload = adapter.validate_python({"type": "start", "text": "hi"})
    assert payload.quote_source is None


@pytest.mark.parametrize("frame", [
    {"type": "join"},
    {"type": "PROGRESS", "cursor": 0, "completed_words": 0, "correct_chars": 0},
    {"cursor": 0, "completed_words": 0, "correct_chars": 0},  # no discriminator at all
    [],
    "finish",
], ids=["unknown-type", "wrong-case-type", "missing-type", "list", "string"])
def test_unknown_or_missing_type_rejected(frame):
    with pytest.raises(ValidationError):
        adapter.validate_python(frame)


@pytest.mark.parametrize("frame", [
    {"type": "progress", "cursor": 0, "completed_words": 0},
    {"type": "start"},
    {"type": "message", "sender": "user"},
    {k: v for k, v in SETTINGS.items() if k != "language"},
    {"type": "countdown"},
], ids=["progress", "start", "message", "settings", "countdown"])
def test_missing_required_field_rejected(frame):
    with pytest.raises(ValidationError):
        adapter.validate_python(frame)


@pytest.mark.parametrize("field, value, ok", [
    ("time_setting", 14, False), ("time_setting", 15, True),
    ("time_setting", 300, True), ("time_setting", 301, False),
    ("word_count", 9, False), ("word_count", 10, True),
    ("word_count", 200, True), ("word_count", 201, False),
])
def test_settings_range_bounds(field, value, ok):
    frame = settings(**{field: value})
    if ok:
        assert getattr(adapter.validate_python(frame), field) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(frame)


@pytest.mark.parametrize("value, ok", [(0, False), (1, True), (3, True), (4, False)])
def test_countdown_value_bounds(value, ok):
    frame = {"type": "countdown", "value": value}
    if ok:
        assert adapter.validate_python(frame).value == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(frame)


@pytest.mark.parametrize("frame", [
    settings(mode="zen"),
    settings(quote_length="huge"),
    {"type": "message", "sender": "admin", "message": "hi"},
], ids=["mode", "quote_length", "sender"])
def test_literal_fields_reject_unknown_values(frame):
    with pytest.raises(ValidationError):
        adapter.validate_python(frame)


# ------------------------- over the socket

def test_invalid_frame_returns_error_and_keeps_connection(client):
    with party(client, "p1") as (_, ws):
        ws["p1"].send_text(json.dumps(settings(time_setting=5)))
        error = recv(ws["p1"])
        assert error["type"] == "error"
        assert error["message"].startswith("Invalid ")
        assert "time_setting" in error["message"]

        # still connected and still processing frames
        ws["p1"].send_text(chat("still here"))
        assert recv(ws["p1"])["message"] == "still here"


def test_unknown_type_over_socket_returns_error(client):
    with party(client, "p1") as (_, ws):
        ws["p1"].send_text(json.dumps({"type": "join"}))
        error = recv(ws["p1"])
        assert error["type"] == "error"
        assert error["message"].startswith("Invalid payload")


def test_invalid_frame_error_only_goes_to_sender(client):
    with party(client, "p1", "p2") as (_, ws):
        ws["p2"].send_text(json.dumps({"type": "progress", "cursor": 1}))
        assert recv(ws["p2"])["type"] == "error"
        # p1's next message is the chat below, not p2's error
        ws["p2"].send_text(chat("marker"))
        assert recv(ws["p1"])["message"] == "marker"


def test_server_only_payload_types_from_client_are_ignored(client):
    with party(client, "p1") as (room_id, ws):
        ws["p1"].send_text(json.dumps({"type": "countdown", "value": 1}))
        ws["p1"].send_text(json.dumps({"type": "end", "leaderboard": []}))
        ws["p1"].send_text(chat("marker"))
        # neither was relayed or acted on: the next thing back is the chat
        assert recv(ws["p1"])["message"] == "marker"


def test_chat_message_relayed_to_everyone(client):
    with party(client, "p1", "p2") as (_, ws):
        ws["p2"].send_text(chat("hello"))
        for player in ("p1", "p2"):
            assert recv(ws[player]) == {"type": "message", "sender": "user", "message": "hello"}


@pytest.mark.xfail(strict=True, reason=(
    "BUG: game_loop calls json.loads outside the ValidationError handler, so a "
    "non-JSON frame raises JSONDecodeError, kills the handler without calling "
    "manager.disconnect, and leaves the player in the room as a ghost"))
def test_non_json_frame_returns_error_and_keeps_connection(client):
    with party(client, "p1") as (_, ws):
        ws["p1"].send_text("not json")
        assert recv(ws["p1"])["type"] == "error"
