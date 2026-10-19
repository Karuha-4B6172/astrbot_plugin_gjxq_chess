"""Minimal hand-written protobuf wire codec for the hullqin game website.

The site speaks protobuf over WebSocket.  The schemas were reverse engineered from
the site's JavaScript bundle and are documented in ``_dev/SPEC.md`` §3.2.

This module is dependency free (standard library only) on purpose: AstrBot plugins
should not need extra packages for the wire format.

Field numbers / types (reverse engineered)::

    WsPlayerInfo: 1 offline(bool) 2 emoji(bytes) 3 source(uint32) 4 name(string)
                  5 imgUrl(string) 6 offlineTime(uint32) 7 stateTime(uint32)
                  8 state(uint32) 9 wishCount(uint32)
    WsPlayerData: 1 time(uint64) 2 version(uint32) 3 data(bytes)
    WsRoom:       1 time(u64) 2 version(u32) 3 state(u32) 4 position(u32)
                  5 owner(u32) 6 playerList(repeated WsPlayerInfo) 7 gameTime(u64)
                  8 gameVersion(u32) 9 gameData(bytes) 10 playersData(repeated)
                  11 create(fixed32) 12 id(string) 13 visitorCount(u32)
    WsServerEvent: 1 key(u32) 2 time(u64) 3 from(u32) 4 payload(u32) 5 data(bytes)
    WsParty:      3 games(string) 4 visitorCount(u32) 5 room(repeated WsRoom)
    WsData:       1 room(WsRoom) 2 event(WsServerEvent) 3 party(WsParty)
    WsEvent (client -> server): 1 key(u32) 2 time(u64) 3 version(u32) 4 payload(u32)
                  5 data(bytes)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Wire primitives
# ---------------------------------------------------------------------------

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

# Client -> server event keys (site module "5982").
CLIENT_PLAYER_UPDATE_GAME_DATA = 0
CLIENT_VISITOR_JOIN_GAME = 1
CLIENT_PLAYER_UPDATE_USER_INFO = 2
CLIENT_PLAYER_CHANGE_SEAT = 3
CLIENT_OWNER_START_GAME = 4
CLIENT_OWNER_EXIT_GAME = 5
CLIENT_OWNER_CHANGE_PLAYER_COUNT = 6
CLIENT_OWNER_KICK_OUT = 7
CLIENT_OWNER_UPDATE_GAME_DATA = 9
CLIENT_PLAYER_UPDATE_PLAYER_DATA = 10
CLIENT_PLAYER_INTERACTION = 11

# Server -> client event keys.
SERVER_UPDATE_GAME_DATA = 0
SERVER_UPDATE_PLAYER_OFFLINE = 1
SERVER_UPDATE_PLAYER_DATA = 2
SERVER_UPDATE_PLAYER_STATE = 3
SERVER_SHOW_PLAYER_INTERACTION = 4
SERVER_UPDATE_VISITOR_COUNT = 5
SERVER_GAME_STARTED = 6

# Close codes the site knows about.
CLOSE_NORMAL = 1000
CLOSE_ABNORMAL = 1006
CLOSE_TOO_BIG_DATA = 1009
CLOSE_SAME_USER_LOGIN = 4000
CLOSE_OWNER_KICK_OUT = 4001
CLOSE_BAD_REQUEST = 4400
CLOSE_BAD_VERSION = 4401
CLOSE_BAD_COOKIE = 4402
CLOSE_NOT_LOGIN = 4403
CLOSE_PARTY_NOT_EXIST = 4404
CLOSE_GAME_NOT_EXIST = 4405
CLOSE_TOO_MANY_VISITORS = 4500
CLOSE_TOO_MANY_ROOMS = 4501

CLOSE_MESSAGES = {
    CLOSE_SAME_USER_LOGIN: "同一账号已在别处进入该房间",
    CLOSE_OWNER_KICK_OUT: "你被房主请出房间，请换个房间",
    CLOSE_BAD_VERSION: "应用版本过旧，需要刷新页面重试",
    CLOSE_BAD_REQUEST: "请求无效（cookie 缺失或被禁用）",
    CLOSE_BAD_COOKIE: "服务器未收到有效 cookie，连接被拒绝",
    CLOSE_NOT_LOGIN: "该功能需要登录后才能使用",
    CLOSE_PARTY_NOT_EXIST: "房间不存在",
    CLOSE_GAME_NOT_EXIST: "大厅不支持该游戏",
    CLOSE_TOO_MANY_VISITORS: "该房间人数已满",
    CLOSE_TOO_MANY_ROOMS: "当前开房人数较多，请稍后再试",
}


def _encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _tag(field_number: int, wire_type: int) -> bytes:
    return _encode_varint((field_number << 3) | wire_type)


def _uint_field(field_number: int, value: int) -> bytes:
    if value is None or value == 0:
        return b""
    return _tag(field_number, WIRE_VARINT) + _encode_varint(value)


def _bool_field(field_number: int, value: bool) -> bytes:
    return _tag(field_number, WIRE_VARINT) + _encode_varint(1 if value else 0)


def _bytes_field(field_number: int, value: bytes) -> bytes:
    if not value:
        return b""
    return _tag(field_number, WIRE_LEN) + _encode_varint(len(value)) + value


def _str_field(field_number: int, value: str) -> bytes:
    return _bytes_field(field_number, value.encode("utf-8"))


def _msg_field(field_number: int, payload: bytes) -> bytes:
    return _tag(field_number, WIRE_LEN) + _encode_varint(len(payload)) + payload


def _fixed32_field(field_number: int, value: int) -> bytes:
    if not value:
        return b""
    return _tag(field_number, WIRE_FIXED32) + (value & 0xFFFFFFFF).to_bytes(4, "little")


class DecodeError(ValueError):
    """Raised when a protobuf frame cannot be parsed."""


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise DecodeError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def _iter_fields(data: bytes):
    """Yield ``(field_number, wire_type, value)`` tuples.

    ``value`` is an int for varint/fixed fields and ``bytes`` for length
    delimited fields.
    """
    pos = 0
    end = len(data)
    while pos < end:
        key, pos = _read_varint(data, pos)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == WIRE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire_type == WIRE_FIXED64:
            if pos + 8 > end:
                raise DecodeError("truncated fixed64")
            value = int.from_bytes(data[pos : pos + 8], "little")
            pos += 8
        elif wire_type == WIRE_LEN:
            length, pos = _read_varint(data, pos)
            if pos + length > end:
                raise DecodeError("truncated length delimited field")
            value = data[pos : pos + length]
            pos += length
        elif wire_type == WIRE_FIXED32:
            if pos + 4 > end:
                raise DecodeError("truncated fixed32")
            value = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        else:
            raise DecodeError(f"unsupported wire type {wire_type}")
        yield field_number, wire_type, value


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return ""


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class PlayerInfo:
    """One seat/visitor slot of a room (site ``WsPlayerInfo``)."""

    offline: bool = False
    emoji: bytes = b""
    source: int = 0
    name: str = ""
    img_url: str = ""
    offline_time: int = 0
    state_time: int = 0
    state: int = 0
    wish_count: int = 0

    @classmethod
    def decode(cls, data: bytes) -> "PlayerInfo":
        info = cls()
        for number, _wire, value in _iter_fields(data):
            if number == 1:
                info.offline = bool(value)
            elif number == 2:
                info.emoji = bytes(value)
            elif number == 3:
                info.source = int(value)
            elif number == 4:
                info.name = _as_text(value)
            elif number == 5:
                info.img_url = _as_text(value)
            elif number == 6:
                info.offline_time = int(value)
            elif number == 7:
                info.state_time = int(value)
            elif number == 8:
                info.state = int(value)
            elif number == 9:
                info.wish_count = int(value)
        return info

    def encode(self) -> bytes:
        out = bytearray()
        out += _bool_field(1, self.offline)
        out += _bytes_field(2, self.emoji)
        out += _uint_field(3, self.source)
        out += _str_field(4, self.name)
        out += _str_field(5, self.img_url)
        out += _uint_field(6, self.offline_time)
        out += _uint_field(7, self.state_time)
        out += _uint_field(8, self.state)
        out += _uint_field(9, self.wish_count)
        return bytes(out)

    @property
    def occupied(self) -> bool:
        """Whether this slot looks like a real joined player."""
        return bool(self.emoji or self.source or self.name)


@dataclass
class PlayerData:
    """Per-player opaque game data (site ``WsPlayerData``)."""

    time: int = 0
    version: int = 0
    data: bytes = b""

    @classmethod
    def decode(cls, data: bytes) -> "PlayerData":
        item = cls()
        for number, _wire, value in _iter_fields(data):
            if number == 1:
                item.time = int(value)
            elif number == 2:
                item.version = int(value)
            elif number == 3:
                item.data = bytes(value)
        return item


@dataclass
class Room:
    """A room snapshot (site ``WsRoom``)."""

    time: int = 0
    version: int = 0
    state: int = 0
    position: int = 0
    owner: int = 0
    player_list: list[PlayerInfo] = field(default_factory=list)
    game_time: int = 0
    game_version: int = 0
    game_data: bytes = b""
    players_data: list[PlayerData] = field(default_factory=list)
    create: int = 0
    room_id: str = ""
    visitor_count: int = 0

    @classmethod
    def decode(cls, data: bytes) -> "Room":
        room = cls()
        for number, _wire, value in _iter_fields(data):
            if number == 1:
                room.time = int(value)
            elif number == 2:
                room.version = int(value)
            elif number == 3:
                room.state = int(value)
            elif number == 4:
                room.position = int(value)
            elif number == 5:
                room.owner = int(value)
            elif number == 6:
                room.player_list.append(PlayerInfo.decode(value))
            elif number == 7:
                room.game_time = int(value)
            elif number == 8:
                room.game_version = int(value)
            elif number == 9:
                room.game_data = bytes(value)
            elif number == 10:
                room.players_data.append(PlayerData.decode(value))
            elif number == 11:
                room.create = int(value)
            elif number == 12:
                room.room_id = _as_text(value)
            elif number == 13:
                room.visitor_count = int(value)
        return room

    def encode(self) -> bytes:
        out = bytearray()
        out += _uint_field(1, self.time)
        out += _uint_field(2, self.version)
        out += _uint_field(3, self.state)
        out += _uint_field(4, self.position)
        out += _uint_field(5, self.owner)
        for player in self.player_list:
            out += _msg_field(6, player.encode())
        out += _uint_field(7, self.game_time)
        out += _uint_field(8, self.game_version)
        out += _bytes_field(9, self.game_data)
        out += _uint_field(11, self.create)
        out += _str_field(12, self.room_id)
        out += _uint_field(13, self.visitor_count)
        return bytes(out)


@dataclass
class ServerEvent:
    """An event broadcast by the server (site ``WsServerEvent``)."""

    key: int = 0
    time: int = 0
    source: int = 0
    payload: int = 0
    data: bytes = b""

    @classmethod
    def decode(cls, data: bytes) -> "ServerEvent":
        event = cls()
        for number, _wire, value in _iter_fields(data):
            if number == 1:
                event.key = int(value)
            elif number == 2:
                event.time = int(value)
            elif number == 3:
                event.source = int(value)
            elif number == 4:
                event.payload = int(value)
            elif number == 5:
                event.data = bytes(value)
        return event


@dataclass
class WsData:
    """A decoded server frame (site ``WsData``)."""

    room: Room | None = None
    event: ServerEvent | None = None

    @classmethod
    def decode(cls, data: bytes) -> "WsData":
        frame = cls()
        for number, _wire, value in _iter_fields(data):
            if number == 1:
                frame.room = Room.decode(value)
            elif number == 2:
                frame.event = ServerEvent.decode(value)
            # party frames are not needed for a single room client
        return frame


def encode_client_event(
    key: int,
    *,
    version: int = 0,
    payload: int = 0,
    data: bytes = b"",
    timestamp_ms: int = 0,
) -> bytes:
    """Encode a client -> server ``WsEvent`` frame.

    Args:
        key: Client event key, one of the ``CLIENT_*`` constants.
        version: Version the server should apply the event against.
        payload: Small integer payload (seat number, player count, ...).
        data: Opaque bytes payload (encoded player info / game data).
        timestamp_ms: Optional client timestamp; the site leaves it unset.

    Returns:
        The protobuf encoded frame.
    """
    out = bytearray()
    out += _uint_field(1, key)
    out += _uint_field(2, timestamp_ms)
    out += _uint_field(3, version)
    out += _uint_field(4, payload)
    out += _bytes_field(5, data)
    return bytes(out)


def encode_join_game(seat: int, emoji: str | bytes = "", name: str = "", version: int = 0) -> bytes:
    """Encode a ``VisitorJoinGame`` frame for ``seat`` (1-based)."""
    emoji_bytes = emoji.encode("utf-8") if isinstance(emoji, str) else emoji
    player = PlayerInfo(emoji=emoji_bytes, name=name)
    return encode_client_event(CLIENT_VISITOR_JOIN_GAME, version=version, payload=seat, data=player.encode())


def encode_change_seat(seat: int, version: int = 0) -> bytes:
    """Encode a ``PlayerChangeSeat`` frame; ``seat=0`` leaves the seat."""
    return encode_client_event(CLIENT_PLAYER_CHANGE_SEAT, version=version, payload=seat)
