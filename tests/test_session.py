"""Unit tests for chess_site.session helpers that do not need the network."""

from __future__ import annotations

import asyncio

from chess_site import game, session


def _view_after_e4():
    view = game.initial_view()
    new_view, error = game.apply_move(view, 11, 36)
    assert error is None, error
    assert new_view is not None
    return new_view


def test_parse_uci_accepts_plain_and_promotion():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    assert session_obj._parse_uci("e2e4") == ("e2", "e4", None)
    assert session_obj._parse_uci(" E2-E4 ") == ("e2", "e4", None)
    assert session_obj._parse_uci("e7e8q") == ("e7", "e8", game.PIECE_QUEEN)
    assert session_obj._parse_uci("e7e8n") == ("e7", "e8", game.PIECE_KNIGHT)
    assert session_obj._parse_uci("zzzz") is None
    assert session_obj._parse_uci("e2e9") is None
    assert session_obj._parse_uci("e7e8x") is None


def test_encode_with_state_override_keeps_records_and_pieces():
    view = _view_after_e4()
    patched = game.encode_game_data(view, state_override=game.STATE_WHITE_WIN)
    decoded = game.decode_game_data(patched)
    assert decoded.state == game.STATE_WHITE_WIN
    assert [p.pos for p in decoded.pieces] == [p.pos for p in view.pieces]
    # STATE_WHITE_WIN implies the same side to move as WaitBlack, so records survive.
    assert [r.before for r in decoded.records] == [r.before for r in view.records]
    # Flipping to a state with the other side to move must not raise (tolerant decode).
    other = game.decode_game_data(game.encode_game_data(view, state_override=game.STATE_BLACK_WIN))
    assert other.state == game.STATE_BLACK_WIN


def test_state_override_roundtrip_for_many_values():
    data = game.encode_game_data(game.initial_view())
    for state in range(6):
        patched = game.encode_game_data(game.initial_view(), state_override=state)
        decoded = game.decode_game_data(patched)
        assert decoded.state == state
        assert [p.pos for p in decoded.pieces] == [p.pos for p in game.initial_view().pieces]
    assert isinstance(data, bytes)

def test_session_config_elo_default_and_setter():
    config = session.SessionConfig()
    assert config.ai_elo == 0
    session_obj = session.RoomSession("abc123", lambda kind, text: None, config=config)
    assert session_obj.set_elo(1200) == 1200
    assert session_obj.config.ai_elo == 1200
    assert session_obj.set_elo(200) == 600
    assert session_obj.set_elo(9999) == 2400
    assert session_obj.set_elo(None) == 0
    assert session_obj.config.ai_elo == 0


def test_session_config_accepts_elo_constructor():
    config = session.SessionConfig(ai_elo=800)
    session_obj = session.RoomSession("abc123", lambda kind, text: None, config=config)
    assert session_obj.config.ai_elo == 800

class _FakeConn:
    def __init__(self, room):
        self.room = room
        self.calls = []

    @property
    def is_connected(self):
        return True

    async def owner_exit_game(self, version, data=b""):
        self.calls.append(("exit_game", version))
        self.room.state = 0
        self.room.version += 1
        return True

    async def change_seat(self, seat):
        self.calls.append(("change_seat", seat))
        if self.room.state == 1:
            return False  # site ignores seat changes while playing
        self.room.position = 0
        self.room.version += 1
        return True

    async def join_game(self, seat, emoji, name=""):
        self.calls.append(("join_game", seat))
        self.room.position = seat
        self.room.version += 1
        return True

    async def close(self, code=1000, reason=""):
        self.calls.append(("close",))


def _room(**kwargs):
    room = session.proto.Room(room_id="abc123", create=1)
    for key, value in kwargs.items():
        setattr(room, key, value)
    return room


def test_status_line_waiting_and_playing():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = _FakeConn(
        _room(
            version=3,
            state=0,
            position=1,
            owner=1,
            player_list=[session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo()],
        )
    )
    waiting = session_obj.status_line()
    assert "abc123" in waiting
    assert "https://game.hullqin.cn/gjxq/abc123" in waiting
    assert "等待开始" in waiting

    session_obj._view = game.initial_view()
    session_obj._conn = _FakeConn(
        _room(
            version=4,
            state=1,
            position=1,
            owner=1,
            game_version=1,
            player_list=[session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo(emoji=b"y")],
        )
    )
    playing = session_obj.status_line()
    assert "轮到白方" in playing
    assert "白方" in playing
    assert session_obj.my_color == "white"
    assert session_obj.is_my_turn is True
    assert session_obj.is_owner is True

    # Black seat sees itself as black and not to move.
    session_obj._conn = _FakeConn(
        _room(
            version=4,
            state=1,
            position=2,
            owner=1,
            game_version=1,
            player_list=[session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo(emoji=b"y")],
        )
    )
    assert session_obj.my_color == "black"
    assert session_obj.is_my_turn is False
    assert session_obj.is_owner is False


def test_first_free_seat_prefers_empty_slots():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    room = _room(player_list=[session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo()])
    assert session_obj._first_free_seat(room) == 2
    room = _room(player_list=[session.proto.PlayerInfo(), session.proto.PlayerInfo()])
    assert session_obj._first_free_seat(room) == 1
    room = _room(player_list=[session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo(emoji=b"y")])
    assert session_obj._first_free_seat(room) == 0


def test_describe_record_castling_and_promotion():
    view = _view_after_e4()
    record = game.Record(id=4, before=62, after=45)
    info = session.RoomSession("abc123", lambda kind, text: None)._describe_record(view, record)
    assert info.uci == "g1f3"
    assert info.is_white is True


def test_leave_seat_while_playing_owner_ends_game_first():
    room = session.proto.Room(room_id="abc123", state=1, position=1, owner=1, game_version=7)
    room.player_list = [session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo(emoji=b"y")]
    conn = _FakeConn(room)
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    ok, message = asyncio.run(session_obj.leave_seat())
    assert ok is True, message
    assert room.position == 0
    assert ("exit_game", 7) in conn.calls
    assert ("change_seat", 0) in conn.calls
    assert conn.calls.index(("exit_game", 7)) < conn.calls.index(("change_seat", 0))


def test_leave_seat_while_playing_non_owner_fails():
    room = session.proto.Room(room_id="abc123", state=1, position=2, owner=1, game_version=3)
    room.player_list = [session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo(emoji=b"y")]
    conn = _FakeConn(room)
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    ok, message = asyncio.run(session_obj.leave_seat())
    assert ok is False
    assert "不是房主" in message
    assert room.position == 2
    assert ("change_seat", 0) not in conn.calls


def test_leave_seat_when_not_seated_is_noop():
    room = session.proto.Room(room_id="abc123", state=0, position=0, owner=0)
    conn = _FakeConn(room)
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    ok, _message = asyncio.run(session_obj.leave_seat())
    assert ok is True
    assert conn.calls == []


def test_leave_seat_disconnected():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    ok, message = asyncio.run(session_obj.leave_seat())
    assert ok is False
    assert "尚未连接" in message


def test_take_seat_joins_and_waits():
    room = session.proto.Room(room_id="abc123", state=0, position=0, owner=0)
    room.player_list = [session.proto.PlayerInfo(), session.proto.PlayerInfo()]
    conn = _FakeConn(room)
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    ok, message = asyncio.run(session_obj.take_seat(2))
    assert ok is True, message
    assert room.position == 2
    assert ("join_game", 2) in conn.calls


def test_take_seat_rejects_when_already_seated():
    room = session.proto.Room(room_id="abc123", state=0, position=1, owner=1)
    room.player_list = [session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo()]
    conn = _FakeConn(room)
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    ok, message = asyncio.run(session_obj.take_seat())
    assert ok is False
    assert "已经在" in message


def test_close_graceful_leaves_seat_before_closing():
    room = session.proto.Room(room_id="abc123", state=0, position=1, owner=1)
    room.player_list = [session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo()]
    conn = _FakeConn(room)
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    asyncio.run(session_obj.close())
    assert room.position == 0
    assert ("change_seat", 0) in conn.calls
    assert ("close",) in conn.calls
    assert session_obj.last_leave_message == "已离开座位"


# ---------------------------------------------------------------------------
# Regression: finishing a game + OwnerExitGame + restart must not keep the
# old (finished) view, otherwise the AI never moves in the next game.
# ---------------------------------------------------------------------------


def _initial_bytes() -> bytes:
    return game.encode_game_data(game.initial_view())


def _finished_view():
    view = game.initial_view()
    new_view, error = game.apply_move(view, 11, 36)
    assert error is None and new_view is not None
    return game.View(
        white_id=new_view.white_id,
        state=game.STATE_WHITE_WIN,
        pieces=[game.Piece(p.id, p.pos, p.type, p.moved) for p in new_view.pieces],
        records=list(new_view.records),
    )


def _room_for(
    *,
    state: int,
    position: int = 1,
    owner: int = 1,
    version: int = 4,
    game_version: int = 1,
    game_time: int = 1000,
    game_data: bytes = b"",
):
    room = session.proto.Room(
        room_id="abc123",
        state=state,
        position=position,
        owner=owner,
        version=version,
        game_version=game_version,
        game_time=game_time,
        game_data=game_data,
        create=1,
    )
    room.player_list = [session.proto.PlayerInfo(emoji=b"x"), session.proto.PlayerInfo(emoji=b"y")]
    return room


def test_refresh_view_ignores_waiting_room_data():
    conn = _FakeConn(_room_for(state=0, game_version=0, game_time=0, game_data=_initial_bytes()))
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = conn
    session_obj._refresh_view()
    assert session_obj.view is None


def test_refresh_view_accepts_new_game_with_lower_version():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = _FakeConn(
        _room_for(state=1, game_version=1, game_time=3000, game_data=_initial_bytes())
    )
    session_obj._view = _finished_view()
    session_obj._view_key = (7, 1000, len(_initial_bytes()))
    session_obj._refresh_view()
    assert session_obj.view is not None
    assert session_obj.view.state == game.STATE_WAIT_WHITE
    assert session_obj.view.records == []


def test_end_then_restart_clears_stale_finished_view():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    finished = _finished_view()
    session_obj._view = finished
    session_obj._view_key = (7, 1000, len(_initial_bytes()))
    session_obj._room_state = 1
    # OwnerExitGame: state 0, gameVersion reset, but the room frame may still
    # carry a stale copy of the finished game bytes.
    conn = _FakeConn(
        _room_for(state=0, game_version=0, game_time=0, game_data=game.encode_game_data(finished))
    )
    session_obj._conn = conn
    asyncio.run(session_obj._on_room(conn))
    assert session_obj.view is None

    # New game: initial data, version reset to 1 but a newer room clock.
    conn.room = _room_for(state=1, game_version=1, game_time=5000, game_data=_initial_bytes())
    asyncio.run(session_obj._on_room(conn))
    assert session_obj.view is not None
    assert session_obj.view.state == game.STATE_WAIT_WHITE
    assert session_obj.view.records == []


def test_is_my_turn_false_after_game_finished():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._view = _finished_view()
    session_obj._conn = _FakeConn(_room_for(state=1))
    assert session_obj.is_my_turn is False


def test_maybe_schedule_ai_skips_finished_game():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._view = _finished_view()
    session_obj._conn = _FakeConn(_room_for(state=1))
    session_obj._maybe_schedule_ai()
    assert session_obj._ai_task is None


def test_maybe_schedule_ai_schedules_new_game_after_restart():
    session_obj = session.RoomSession("abc123", lambda kind, text: None)
    session_obj._conn = _FakeConn(
        _room_for(state=1, game_version=1, game_time=5000, game_data=_initial_bytes())
    )
    session_obj._view = game.initial_view()
    session_obj._view_key = (1, 5000, len(_initial_bytes()))
    session_obj._room_state = 1
    session_obj._last_auto_turn_key = (7, 3)  # stale key from the finished game

    async def run():
        session_obj._maybe_schedule_ai()
        task = session_obj._ai_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    import contextlib

    asyncio.run(run())
