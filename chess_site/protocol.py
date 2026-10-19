"""Low level WebSocket client for one hullqin game room.

Only the transport and room-snapshot merging live here; game semantics are in
:mod:`chess_site.session`.  Schema details: ``_dev/SPEC.md`` §3.

The site requires an anonymous ``gid`` cookie which is handed out by the HTTP
entry point, therefore the same :class:`aiohttp.ClientSession` (with its cookie
jar) is used for the HTTP bootstrap request and the websocket handshake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from . import proto

LOGGER = logging.getLogger("astrbot_plugin_gjxq_chess.protocol")

BASE_URL = "https://game.hullqin.cn"
PAGE_PATH = "/gjxq/"
WS_PATH = "/gjxq/{room_id}?v=1"

# The site re-uses this reconnect backoff policy (2.0s + 0.7s jitter, 3 tries).
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_TRIES = 3

RoomCallback = Callable[["RoomConnection"], Awaitable[None] | None]
EventCallback = Callable[["RoomConnection", proto.ServerEvent], Awaitable[None] | None]


class RoomConnectionError(RuntimeError):
    """Raised when the room websocket cannot be established."""


def describe_close(code: int) -> str:
    """Return a human readable description for a websocket close code.

    Args:
        code: WebSocket close code reported by aiohttp.

    Returns:
        Chinese description of the close code, or a generic message.
    """
    return proto.CLOSE_MESSAGES.get(code, f"连接已断开（代码 {code}）")


class RoomConnection:
    """One websocket connection to ``wss://game.hullqin.cn/gjxq/<room>``.

    Callbacks are invoked from the receive loop and may be sync or async.  All
    room snapshots are merged into :attr:`room` before callbacks run.
    """

    def __init__(
        self,
        room_id: str,
        *,
        on_room: RoomCallback | None = None,
        on_event: EventCallback | None = None,
        on_close: Callable[["RoomConnection", int, str], Awaitable[None] | None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.room_id = room_id
        self.room: proto.Room = proto.Room(room_id=room_id)
        self.last_frame_at: int = 0
        self.closed = asyncio.Event()
        self.close_code: int = 0
        self.close_reason: str = ""
        self.on_room = on_room
        self.on_event = on_event
        self.on_close = on_close
        self.logger = logger or LOGGER
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._cookie_ready = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Whether the websocket is currently open."""
        return self._ws is not None and not self._ws.closed

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=None)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _fetch_cookie(self) -> None:
        """Ask the HTTP entry point for the anonymous ``gid`` cookie."""
        if self._cookie_ready:
            return
        session = await self._ensure_session()
        async with session.get(f"{BASE_URL}{PAGE_PATH}") as response:
            await response.read()
            if response.status != 200:
                raise RoomConnectionError(f"获取房间 cookie 失败（HTTP {response.status}）")
        if not any(cookie.key == "gid" for cookie in session.cookie_jar):
            self.logger.warning("HTTP 响应没有下发 gid cookie，仍尝试连接")
        self._cookie_ready = True

    async def connect(self, timeout: float = 15.0) -> proto.Room:
        """Open the websocket and wait for the first room snapshot.

        Args:
            timeout: Seconds to wait for the initial room frame.

        Returns:
            The initial room snapshot.

        Raises:
            RoomConnectionError: When the handshake fails or times out.
            asyncio.TimeoutError: When the first room frame does not arrive.
        """
        await self._fetch_cookie()
        session = await self._ensure_session()
        url = f"wss://game.hullqin.cn{WS_PATH.format(room_id=self.room_id)}"
        try:
            self._ws = await session.ws_connect(
                url,
                headers={"Origin": BASE_URL},
                max_msg_size=4 * 1024 * 1024,
                heartbeat=30.0,
                autoping=True,
            )
        except aiohttp.WSServerHandshakeError as exc:
            raise RoomConnectionError(f"房间连接被拒绝（HTTP {exc.status}）") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise RoomConnectionError(f"房间连接失败：{exc}") from exc

        first_room = asyncio.get_running_loop().create_future()

        async def _on_first_room(conn: "RoomConnection") -> None:
            if not first_room.done():
                first_room.set_result(conn.room)

        # The receive loop is responsible for dispatching both callbacks; the
        # temporary override only exists until the initial snapshot arrived.
        original_on_room = self.on_room
        self.on_room = _on_first_room
        self._recv_task = asyncio.create_task(self._receive_loop(), name=f"gjxq-recv-{self.room_id}")
        try:
            room = await asyncio.wait_for(first_room, timeout=timeout)
        except asyncio.TimeoutError:
            await self.close(code=proto.CLOSE_NORMAL, reason="initial timeout")
            raise
        finally:
            self.on_room = original_on_room
        if self.on_room is not None:
            await _call(self.on_room, self)
        return room

    async def close(self, code: int = proto.CLOSE_NORMAL, reason: str = "") -> None:
        """Close the websocket and the HTTP session."""
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close(code=code, message=reason.encode("utf-8")[:120])
            except Exception:  # noqa: BLE001 - best effort shutdown
                pass
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        self._recv_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self.closed.set()

    # -- receive loop ------------------------------------------------------

    async def _receive_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_frame(bytes(message.data))
                elif message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self._handle_frame(message.data.encode("utf-8"))
                    except Exception:  # noqa: BLE001 - ignore malformed text frames
                        self.logger.debug("忽略文本帧")
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
                elif message.type == aiohttp.WSMsgType.ERROR:
                    self.logger.warning("房间 websocket 传输错误：%s", ws.exception())
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors
            self.logger.warning("房间接收循环异常：%s", exc)
        finally:
            self.close_code = ws.close_code or 1006
            self.close_reason = getattr(ws, "close_reason", "") or ""
            self.closed.set()
            if self.on_close is not None:
                await _call(self.on_close, self, self.close_code, self.close_reason)

    async def _handle_frame(self, payload: bytes) -> None:
        self.last_frame_at = _now_ms()
        try:
            frame = proto.WsData.decode(payload)
        except proto.DecodeError as exc:
            self.logger.debug("无法解码房间帧：%s", exc)
            return
        if frame.room is not None:
            room = frame.room
            previous_state = self.room.state
            previous_version = self.room.version
            if room.room_id == "":
                room.room_id = self.room_id
            current_room = self.room
            # Room-level snapshots do not advance the game version (live verified);
            # they may even carry a stale/empty game payload.  Never regress the
            # game snapshot we already hold from an UpdateGameData event.
            if current_room.game_data and (
                not room.game_data
                or not _is_newer_snapshot(
                    current_room.game_time,
                    current_room.game_version,
                    room.game_time,
                    room.game_version,
                )
            ):
                room.game_time = current_room.game_time
                room.game_version = current_room.game_version
                room.game_data = current_room.game_data
            self.room = room
            self.logger.debug(
                "房间快照 v%s state=%s position=%s owner=%s players=%s",
                room.version,
                room.state,
                room.position,
                room.owner,
                len(room.player_list),
            )
            if previous_state != room.state or previous_version != room.version:
                self.logger.debug("房间状态变化 %s/%s -> %s/%s", previous_state, previous_version, room.state, room.version)
            if self.on_room is not None:
                await _call(self.on_room, self)
        if frame.event is not None:
            await self._apply_event(frame.event)
            if self.on_event is not None:
                await _call(self.on_event, self, frame.event)

    async def _apply_event(self, event: proto.ServerEvent) -> None:
        """Merge a broadcast event into the cached room snapshot."""
        room = self.room
        if event.key == proto.SERVER_UPDATE_GAME_DATA:
            # Snapshot merging: newer time wins, ties broken by version.
            incoming_version = event.payload or 0
            incoming_time = event.time or 0
            if _is_newer_snapshot(room.game_time, room.game_version, incoming_time, incoming_version):
                room.game_time = incoming_time
                room.game_version = incoming_version
                room.game_data = event.data or b""
        elif event.key == proto.SERVER_UPDATE_PLAYER_STATE:
            index = event.source or 0
            if index < len(room.player_list):
                room.player_list[index].state = event.payload or 0
                room.player_list[index].state_time = event.time or _now_ms()
        elif event.key == proto.SERVER_UPDATE_PLAYER_OFFLINE:
            index = event.source or 0
            if index < len(room.player_list):
                room.player_list[index].offline = bool(event.payload)
                room.player_list[index].offline_time = (event.time or _now_ms()) if event.payload else 0
        elif event.key == proto.SERVER_UPDATE_VISITOR_COUNT:
            room.visitor_count = event.payload or 0
        elif event.key == proto.SERVER_UPDATE_PLAYER_DATA:
            index = event.source or 0
            while len(room.players_data) <= index:
                room.players_data.append(proto.PlayerData())
            item = room.players_data[index]
            item.time = event.time or 0
            item.version = event.payload or 0
            item.data = event.data or b""

    # -- sending -----------------------------------------------------------

    async def send(self, frame: bytes) -> bool:
        """Send one already encoded client frame.

        Args:
            frame: Protobuf encoded ``WsEvent`` bytes.

        Returns:
            True when the frame was handed to the websocket, False otherwise.
        """
        if not self.is_connected or self._ws is None:
            return False
        async with self._send_lock:
            try:
                await self._ws.send_bytes(frame)
                return True
            except (aiohttp.ClientError, OSError, RuntimeError) as exc:
                self.logger.warning("发送房间消息失败：%s", exc)
                return False

    async def join_game(self, seat: int, emoji: str, name: str = "") -> bool:
        """Join (or switch to) a seat; ``seat`` is 1-based."""
        return await self.send(proto.encode_join_game(seat, emoji, name, version=self.room.version))

    async def change_seat(self, seat: int) -> bool:
        """Switch seat (``seat=0`` leaves the current seat)."""
        return await self.send(proto.encode_change_seat(seat, version=self.room.version))

    async def update_game_data(self, version: int, data: bytes) -> bool:
        """Publish a full game data snapshot for the current game version."""
        return await self.send(
            proto.encode_client_event(proto.CLIENT_PLAYER_UPDATE_GAME_DATA, version=version, data=data)
        )

    async def start_game(self, data: bytes, *, shuffle: bool = False) -> bool:
        """Ask the server (owner only) to start the game with initial data.

        The event version must be the current *room* version, otherwise the
        server silently drops the request (verified against the live server).
        """
        return await self.send(
            proto.encode_client_event(
                proto.CLIENT_OWNER_START_GAME,
                version=self.room.version,
                payload=1 if shuffle else 0,
                data=data,
            )
        )

    async def owner_update_game_data(self, data: bytes) -> bool:
        """Owner updates the shared game data (e.g. timer settings) while waiting."""
        return await self.send(
            proto.encode_client_event(
                proto.CLIENT_OWNER_UPDATE_GAME_DATA,
                version=self.room.version,
                data=data,
            )
        )

    async def update_user_info(self, payload: int) -> bool:
        """Update the player's emoji/state (``PlayerUpdateUserInfo``)."""
        return await self.send(
            proto.encode_client_event(proto.CLIENT_PLAYER_UPDATE_USER_INFO, version=0, payload=payload)
        )

    async def owner_exit_game(self, version: int, data: bytes = b"") -> bool:
        """Owner finishes/ends the current game."""
        return await self.send(
            proto.encode_client_event(proto.CLIENT_OWNER_EXIT_GAME, version=version, data=data)
        )


async def _call(callback: Callable[..., Any], *args: Any) -> None:
    try:
        result = callback(*args)
    except Exception as exc:  # noqa: BLE001 - callbacks must not kill the loop
        LOGGER.warning("房间回调异常：%s", exc)
        return
    if asyncio.iscoroutine(result):
        try:
            await result
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("房间异步回调异常：%s", exc)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _is_newer_snapshot(
    current_time: int,
    current_version: int,
    incoming_time: int,
    incoming_version: int,
) -> bool:
    """Replicate the site's snapshot ordering rule (module 2143 ``P``).

    Args:
        current_time: Timestamp of the stored snapshot in ms (0 = none).
        current_version: Game version of the stored snapshot.
        incoming_time: Timestamp of the received snapshot in ms.
        incoming_version: Game version of the received snapshot.

    Returns:
        True when the received snapshot should replace the stored one.
    """
    if current_time == 0:
        return True
    if incoming_time != current_time:
        return incoming_time > current_time
    # Same timestamp: the site treats an old client version specially, leave the
    # comparison on version numbers for equal timestamps.
    return incoming_version >= current_version
