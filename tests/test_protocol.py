"""Tests for the outer WebSocket protobuf codec (``chess_site/proto.py``)."""

from __future__ import annotations

import binascii

from chess_site import proto


def _fields(frame: bytes) -> dict[int, object]:
    result = {}
    for number, _wire, value in proto._iter_fields(frame):
        result[number] = value
    return result


def test_decode_room_frame():
    # Captured from a freshly created room: version 1, two offline seats, create timestamp.
    frame = binascii.unhexlify("0a0f100132020801320208015d07d9d56a")
    data = proto.WsData.decode(frame)
    assert data.room is not None
    room = data.room
    assert room.version == 1
    assert room.state == 0
    assert room.position == 0
    assert room.owner == 0
    assert room.create == 0x6AD5D907
    assert len(room.player_list) == 2
    assert all(player.offline for player in room.player_list)


def test_join_game_event_fields():
    frame = proto.encode_join_game(2, "😀", version=5)
    fields = _fields(frame)
    assert fields[1] == proto.CLIENT_VISITOR_JOIN_GAME
    assert fields[3] == 5
    assert fields[4] == 2
    player = proto.PlayerInfo.decode(fields[5])
    assert player.emoji == "😀".encode("utf-8")


def test_leave_seat_event_omits_payload():
    frame = proto.encode_change_seat(0, version=3)
    fields = _fields(frame)
    assert fields[1] == proto.CLIENT_PLAYER_CHANGE_SEAT
    assert fields[3] == 3
    assert 4 not in fields  # no payload == leave the seat


def test_client_event_roundtrip():
    payload = b"\x01\x02\x03"
    frame = proto.encode_client_event(
        proto.CLIENT_OWNER_START_GAME,
        version=7,
        payload=9,
        data=payload,
    )
    fields = _fields(frame)
    assert fields[1] == proto.CLIENT_OWNER_START_GAME
    assert fields[3] == 7
    assert fields[4] == 9
    assert fields[5] == payload


def test_key_zero_is_omitted_as_protobuf_default():
    # PlayerUpdateGameData is key 0, the protobuf default: the field is implicit.
    frame = proto.encode_client_event(proto.CLIENT_PLAYER_UPDATE_GAME_DATA, version=2, data=b"x")
    fields = _fields(frame)
    assert 1 not in fields
    assert fields[3] == 2


def test_server_event_and_player_info_decode():
    event = proto.ServerEvent(
        key=proto.SERVER_UPDATE_GAME_DATA,
        time=1234,
        source=0,
        payload=5,
        data=b"abc",
    )
    # Round-trip through the room encoder is not available for events, so build
    # the frame by hand through the same helpers the codec uses internally.
    frame = (
        proto._uint_field(1, event.key)
        + proto._uint_field(2, event.time)
        + proto._uint_field(3, event.source)
        + proto._uint_field(4, event.payload)
        + proto._bytes_field(5, event.data)
    )
    decoded = proto.ServerEvent.decode(frame)
    assert decoded == event

    info = proto.PlayerInfo(emoji=b"x", name="bot", source=1, state=3)
    assert proto.PlayerInfo.decode(info.encode()) == info
