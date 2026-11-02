import asyncio
import json
import time
from types import SimpleNamespace

import anyio
import pytest

from app.game import engine
from app.game.manager import finalize_race, manager
from app.schemas.payloads import *
from tests.helpers import chat, drain_until, make_room, party, recv, room_with_status, wait_for

pytestmark = pytest.mark.anyio

START = StartPayload(type="start", text="hi there friend")


def progress(cursor: int, correct_chars: int, completed_words: int = 0) -> ProgressPayload:
    return ProgressPayload(type="progress", cursor=cursor,
                           completed_words=completed_words, correct_chars=correct_chars)


async def start_race(room: str, host: str = "p1", payload: StartPayload = START) -> asyncio.Task:
    """Starts a race in the background and returns once it's active. The
    returned task is run_game's overall race timer."""
    task = asyncio.create_task(manager.handle_host_start(room, host, payload))
    await wait_for(lambda: manager.rooms[room]["status"] == "active")
    return task


# ------------------------- countdown -> start (over the socket)

def test_start_runs_countdown_then_goes_active(client):
    with party(client, "p1", "p2") as (room_id, ws):
        ws["p1"].send_text(json.dumps({"type": "start", "text": "to be or not", "quote_source": "Hamlet"}))
        for player in ("p1", "p2"):
            countdown_room = recv(ws[player])
            assert countdown_room["type"] == "room" and countdown_room["status"] == "countdown"
            assert countdown_room["text"] == "to be or not"
            assert countdown_room["quote_source"] == "Hamlet"
            assert [recv(ws[player]) for _ in range(3)] == [
                {"type": "countdown", "value": v} for v in (3, 2, 1)]
            assert recv(ws[player])["status"] == "active"
        assert manager.rooms[room_id]["start_time"] is not None


def test_progress_broadcast_to_every_player(client):
    with party(client, "p1", "p2") as (_, ws):
        ws["p1"].send_text(START.model_dump_json())
        for player in ("p1", "p2"):
            drain_until(ws[player], room_with_status("active"))
        ws["p1"].send_text(progress(cursor=10, correct_chars=9, completed_words=2).model_dump_json())
        for player in ("p1", "p2"):
            p1 = recv(ws[player])["players"]["p1"]
            assert (p1["cursor"], p1["correct_chars"], p1["completed_words"]) == (10, 9, 2)
            assert p1["accuracy"] == 90


def test_progress_before_race_starts_is_ignored(client):
    with party(client, "p1") as (room_id, ws):
        ws["p1"].send_text(progress(cursor=10, correct_chars=10).model_dump_json())
        ws["p1"].send_text(chat("marker"))
        assert recv(ws["p1"])["message"] == "marker"  # no room update in between
        assert manager.rooms[room_id]["players"]["p1"]["cursor"] == 0


def test_grace_period_ends_race_for_stragglers(client, timing):
    timing(GRACE_PERIOD_SECONDS=0.05)  # race timer itself stays at 60 s
    with party(client, "p1", "p2") as (room_id, ws):
        ws["p1"].send_text(START.model_dump_json())
        drain_until(ws["p1"], room_with_status("active"))
        ws["p1"].send_text(json.dumps({"type": "finish"}))
        end = drain_until(ws["p1"], lambda m: m["type"] == "end")
        assert [(e["player_id"], e["finished"]) for e in end["leaderboard"]] == [("p1", True), ("p2", False)]
        assert recv(ws["p1"])["status"] == "waiting"
        assert manager.rooms[room_id]["status"] == "waiting"


# ------------------------- server-side WPM / accuracy

async def test_wpm_from_correct_chars_over_elapsed_minutes():
    room, sockets = await make_room("p1")
    manager.rooms[room]["start_time"] = time.perf_counter() - 60  # one minute in
    await manager.handle_progress(room, progress(cursor=260, correct_chars=250), "p1")
    player = manager.rooms[room]["players"]["p1"]
    assert player["wpm"] == 50  # (250 chars / 5) per minute
    assert player["accuracy"] == 96  # round(250 / 260 * 100)
    assert sockets["p1"].of_type("room")[-1]["players"]["p1"]["wpm"] == 50


async def test_wpm_held_at_zero_in_first_second():
    room, _ = await make_room("p1")
    manager.rooms[room]["start_time"] = time.perf_counter() - 0.5
    await manager.handle_progress(room, progress(cursor=20, correct_chars=20), "p1")
    assert manager.rooms[room]["players"]["p1"]["wpm"] == 0


async def test_accuracy_zero_when_nothing_typed():
    room, _ = await make_room("p1")
    manager.rooms[room]["start_time"] = time.perf_counter() - 10
    await manager.handle_progress(room, progress(cursor=0, correct_chars=0), "p1")
    assert manager.rooms[room]["players"]["p1"]["accuracy"] == 0


# ------------------------- ranking

def set_stats(room: str, stats: dict[str, tuple[float, bool, float]]) -> None:
    for pid, (wpm, finished, accuracy) in stats.items():
        manager.rooms[room]["players"][pid].update(wpm=wpm, finished=finished, accuracy=accuracy)


@pytest.mark.parametrize("stats, expected_order", [
    # higher wpm wins even though the slower player finished and the faster didn't
    ({"p1": (60, True, 99), "p2": (80, False, 90)}, ["p2", "p1"]),
    # same wpm: finishing beats partial progress, regardless of accuracy
    ({"p1": (70, False, 100), "p2": (70, True, 80)}, ["p2", "p1"]),
    # same wpm and finished: accuracy breaks the tie
    ({"p1": (70, True, 90), "p2": (70, True, 95)}, ["p2", "p1"]),
    ({"p1": (40, True, 90), "p2": (90, True, 90), "p3": (65, False, 90)}, ["p2", "p3", "p1"]),
], ids=["wpm-first", "finished-tiebreak", "accuracy-tiebreak", "three-players"])
async def test_leaderboard_ranked_by_wpm_finished_accuracy(stats, expected_order):
    room, sockets = await make_room(*stats)
    manager.rooms[room]["status"] = "active"
    set_stats(room, stats)
    await finalize_race(room)

    (end,) = sockets["p1"].of_type("end")
    assert [e["player_id"] for e in end["leaderboard"]] == expected_order
    assert [e["rank"] for e in end["leaderboard"]] == list(range(1, len(stats) + 1))
    for entry in end["leaderboard"]:
        wpm, finished, accuracy = stats[entry["player_id"]]
        assert (entry["wpm"], entry["finished"], entry["accuracy"]) == (wpm, finished, accuracy)


async def test_finalize_returns_room_to_lobby_and_broadcasts_end_then_room():
    room, sockets = await make_room("p1", "p2")
    manager.rooms[room]["status"] = "active"
    await finalize_race(room)
    assert manager.rooms[room]["status"] == "waiting"
    for ws in sockets.values():
        assert [m["type"] for m in ws.sent] == ["end", "room"]
        assert ws.sent[1]["status"] == "waiting"


async def test_finalize_is_idempotent():
    room, sockets = await make_room("p1")
    manager.rooms[room]["status"] = "active"
    await finalize_race(room)
    await finalize_race(room)
    assert len(sockets["p1"].of_type("end")) == 1


async def test_finalize_on_deleted_room_is_noop():
    await finalize_race("GONE00")


# ------------------------- finishing / grace period

async def test_finish_ignored_outside_active_race():
    room, sockets = await make_room("p1", "p2")
    await manager.handle_player_finish(room, "p1")
    assert manager.rooms[room]["players"]["p1"]["finished"] is False
    assert manager.rooms[room]["finish_order"] == []
    assert manager.rooms[room]["grace_task"] is None


async def test_finish_from_unknown_player_ignored():
    room, _ = await make_room("p1", "p2")
    manager.rooms[room]["status"] = "active"
    await manager.handle_player_finish(room, "ghost")
    assert manager.rooms[room]["finish_order"] == []


async def test_finish_for_deleted_room_is_noop():
    await manager.handle_player_finish("GONE00", "p1")


async def test_first_finish_starts_grace_period_once():
    room, sockets = await make_room("p1", "p2", "p3")
    manager.rooms[room]["status"] = "active"
    await manager.handle_player_finish(room, "p1")
    grace = manager.rooms[room]["grace_task"]
    assert grace is not None and not grace.done()
    await manager.handle_player_finish(room, "p2")
    assert manager.rooms[room]["grace_task"] is grace  # not restarted by later finishers
    assert manager.rooms[room]["finish_order"] == ["p1", "p2"]
    assert manager.rooms[room]["status"] == "active"
    assert sockets["p1"].of_type("end") == []


async def test_everyone_finishing_ends_race_and_cancels_grace(timing):
    timing(GRACE_PERIOD_SECONDS=0.05)
    room, sockets = await make_room("p1", "p2")
    manager.rooms[room]["status"] = "active"
    await manager.handle_player_finish(room, "p1")
    grace = manager.rooms[room]["grace_task"]
    await manager.handle_player_finish(room, "p2")

    assert manager.rooms[room]["status"] == "waiting"
    assert manager.rooms[room]["grace_task"] is None
    await asyncio.sleep(0.1)  # past where the grace period would have fired
    assert grace.cancelled()
    assert len(sockets["p1"].of_type("end")) == 1


async def test_grace_period_finalizes_with_stragglers_unfinished(timing):
    timing(GRACE_PERIOD_SECONDS=0.05)
    room, sockets = await make_room("p1", "p2")
    manager.rooms[room]["status"] = "active"
    await manager.handle_player_finish(room, "p1")
    assert manager.rooms[room]["status"] == "active"
    await wait_for(lambda: manager.rooms[room]["status"] == "waiting")
    (end,) = sockets["p2"].of_type("end")
    assert {e["player_id"]: e["finished"] for e in end["leaderboard"]} == {"p1": True, "p2": False}


# ------------------------- run_game's race timer

@pytest.fixture
def engine_sleeps(monkeypatch):
    """Makes every sleep in run_game instant and records the requested
    durations, so a full race can run without waiting out its timer."""
    requested = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        requested.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(engine, "asyncio", SimpleNamespace(sleep=sleep))
    return requested


@pytest.mark.parametrize("mode, expected_timer", [
    ("time", 45),  # the room's time_setting
    ("words", 300),  # MAX_RACE_DURATION
    ("quote", 300),
])
async def test_race_timer_length_depends_on_mode(engine_sleeps, mode, expected_timer):
    room, sockets = await make_room("p1")
    manager.rooms[room]["mode"] = mode
    manager.rooms[room]["time_setting"] = 45
    with anyio.fail_after(2):
        await manager.handle_host_start(room, "p1", START)
    assert engine_sleeps == [1, 1, 1, expected_timer]  # countdown, then race timer
    # the timer running out ends the race for whoever hasn't finished
    (end,) = sockets["p1"].of_type("end")
    assert end["leaderboard"][0]["finished"] is False
    assert manager.rooms[room]["status"] == "waiting"


async def test_second_start_during_race_ignored():
    room, sockets = await make_room("p1")
    race = await start_race(room)
    await manager.handle_host_start(room, "p1", StartPayload(type="start", text="other text"))
    assert manager.rooms[room]["text"] == START.text
    assert len(sockets["p1"].of_type("countdown")) == 3
    race.cancel()


async def test_restart_resets_finish_state():
    room, _ = await make_room("p1", "p2")
    race = await start_race(room)
    await manager.handle_player_finish(room, "p1")
    await manager.handle_player_finish(room, "p2")
    assert manager.rooms[room]["status"] == "waiting"
    race.cancel()

    race = await start_race(room)
    assert manager.rooms[room]["finish_order"] == []
    assert not any(p["finished"] for p in manager.rooms[room]["players"].values())
    race.cancel()


async def test_everyone_leaving_mid_race_deletes_room_cleanly(timing):
    timing(MAX_RACE_DURATION=0.05)
    room, sockets = await make_room("p1", "p2")
    manager.rooms[room]["mode"] = "words"
    race = await start_race(room)
    for pid, ws in sockets.items():
        await manager.disconnect(ws, room, pid)
    assert room not in manager.rooms
    with anyio.fail_after(2):
        await race  # the race timer still fires, finalize_race just no-ops


# ------------------------- known bugs

@pytest.mark.xfail(strict=True, reason=(
    "BUG: handle_host_start only resets `finished`/`finish_order` between races, "
    "so wpm/accuracy/cursor from the previous race carry over; a player who "
    "sends no progress in the next race is ranked on their old wpm"))
async def test_player_stats_reset_between_races():
    room, _ = await make_room("p1", "p2")
    race = await start_race(room)
    manager.rooms[room]["start_time"] = time.perf_counter() - 60
    await manager.handle_progress(room, progress(cursor=250, correct_chars=250), "p2")
    await finalize_race(room)
    race.cancel()

    race = await start_race(room)
    race.cancel()
    assert manager.rooms[room]["players"]["p2"]["wpm"] == 0


@pytest.mark.xfail(strict=True, reason=(
    "BUG: start_time is never cleared when a race ends, so handle_progress's "
    "'race hasn't started' guard only works before the first race; progress "
    "sent in the lobby (or during a later countdown) is applied"))
async def test_progress_between_races_ignored():
    room, _ = await make_room("p1")
    race = await start_race(room)
    await finalize_race(room)
    race.cancel()

    await manager.handle_progress(room, progress(cursor=10, correct_chars=10), "p1")
    assert manager.rooms[room]["players"]["p1"]["cursor"] == 0


@pytest.mark.xfail(strict=True, reason=(
    "BUG: when a race ends early (everyone finished / grace period), run_game's "
    "race timer keeps sleeping; if the host starts another race before it "
    "fires, the old timer's finalize_race ends the new race early (a pending "
    "grace task from a timer-ended race can do the same)"))
async def test_previous_race_timer_does_not_end_next_race(timing):
    timing(MAX_RACE_DURATION=0.4)
    room, _ = await make_room("p1", "p2")
    manager.rooms[room]["mode"] = "words"
    first = await start_race(room)  # its timer fires ~0.4 s from now
    await manager.handle_player_finish(room, "p1")
    await manager.handle_player_finish(room, "p2")  # race 1 ends early
    await asyncio.sleep(0.2)

    second = await start_race(room)  # its own timer fires ~0.6 s from race 1's start
    try:
        await asyncio.sleep(0.3)  # past race 1's timer, before race 2's
        assert manager.rooms[room]["status"] == "active"
    finally:
        first.cancel()
        second.cancel()
