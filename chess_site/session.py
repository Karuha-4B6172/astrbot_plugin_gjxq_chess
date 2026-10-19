"""High level room session: connect, seat, start, move and auto-play.

One :class:`RoomSession` owns one hullqin room connection and translates between
the site's game-data snapshots (:mod:`chess_site.game`) and chat commands
handled by ``main.py``.

The session never talks to AstrBot directly; it reports interesting things via
the ``notify(kind, text)`` callback supplied by the plugin.  The plugin then reads
:attr:`RoomSession.view` / :attr:`RoomSession.room` and renders a reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import ai, game, proto, protocol
from .logs import SilentLog

ROOM_URL_TEMPLATE = "https://game.hullqin.cn/gjxq/{room_id}"

# Notify kinds understood by the plugin layer.
NOTIFY_INFO = "info"
NOTIFY_BOARD = "board"
NOTIFY_STARTED = "started"
NOTIFY_OPPONENT_MOVED = "opponent_moved"
NOTIFY_YOUR_TURN = "your_turn"
NOTIFY_MOVED = "moved"
NOTIFY_GAME_OVER = "game_over"
NOTIFY_ERROR = "error"
NOTIFY_CLOSED = "closed"
NOTIFY_ROOM_CREATED = "room_created"
NOTIFY_OPPONENT_JOINED = "opponent_joined"

DEFAULT_EMOJI = "😀"
PROMOTION_LETTERS = {
    game.PIECE_QUEEN: "q",
    game.PIECE_ROCK: "r",
    game.PIECE_BISHOP: "b",
    game.PIECE_KNIGHT: "n",
}
PROMOTION_TYPES = {value: key for key, value in PROMOTION_LETTERS.items()}

NotifyCallback = Callable[[str, str], Awaitable[None] | None]

# Reconnect policy mirrored from the site bundle.
RECONNECT_DELAYS = (2.0, 4.0, 6.0)


@dataclass
class SessionConfig:
    """Tunables for one room session."""

    ai_enabled: bool = True
    ai_depth: int = 3
    ai_time_limit: float = 2.0
    #: Approximate Elo difficulty (600-2400); 0 disables it and uses depth/time.
    ai_elo: int = 0
    auto_start: bool = True
    move_delay: float = 0.8
    emoji: str = DEFAULT_EMOJI


@dataclass
class MoveInfo:
    """A decoded move record plus a convenient UCI-like notation."""

    record: game.Record
    uci: str
    is_white: bool
    promotion_type: int | None = None
    label: str = ""


class RoomSession:
    """Drives one room on game.hullqin.cn."""

    def __init__(
        self,
        room_id: str,
        notify: NotifyCallback,
        *,
        config: SessionConfig | None = None,
        logger: Any | None = None,
        room_created: bool = False,
    ) -> None:
        self.room_id = room_id
        self.notify = notify
        self.config = config or SessionConfig()
        # ``main.py`` injects the AstrBot logger; the silent sink keeps the
        # package usable without AstrBot (offline tests / local scripts).
        self.logger = logger or SilentLog()
        self.created_room = room_created
        self._conn: protocol.RoomConnection | None = None
        self._view: game.View | None = None
        self._view_key: tuple[int, int, int] | None = None
        self._my_index: int = 0
        self._my_seat: int = 0
        self._my_color: str | None = None
        self._ai_task: asyncio.Task | None = None
        self._auto_start_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closed = False
        self._closing = False
        self._game_over_notified = False
        self._known_players = 0
        self._pending_move_version: int | None = None
        self.last_leave_message = ""
        self._notified_start = False
        self._room_state = 0
        self.last_moves: list[MoveInfo] = []
        self._last_auto_turn_key: tuple[int, int] | None = None

    # ------------------------------------------------------------------ state

    @property
    def room(self) -> proto.Room:
        """Latest room snapshot (empty snapshot before connecting)."""
        if self._conn is None:
            return proto.Room(room_id=self.room_id)
        return self._conn.room

    @property
    def connected(self) -> bool:
        """Whether the websocket is currently open."""
        return self._conn is not None and self._conn.is_connected

    @property
    def view(self) -> game.View | None:
        """Current decoded chess position, or None when no game data yet."""
        return self._view

    @property
    def seat(self) -> int:
        """Our seat (1-based, 0 = spectator)."""
        return self.room.position

    @property
    def my_index(self) -> int:
        """Our player index (0-based)."""
        return max(0, self.room.position - 1)

    @property
    def my_color(self) -> str | None:
        """``"white"``/``"black"`` while seated and playing, else None."""
        if self._view is None or not self.room.position:
            return None
        return "white" if self._view.white_id == self.my_index else "black"

    @property
    def is_owner(self) -> bool:
        """Whether we are the room owner."""
        return bool(self.room.position) and self.room.position == self.room.owner

    @property
    def is_playing(self) -> bool:
        """Whether a game is currently in progress."""
        return self.room.state == 1 and self._view is not None

    @property
    def is_my_turn(self) -> bool:
        """Whether it is our turn to move in a running (not finished) game."""
        if self._view is None or not self.is_playing:
            return False
        if game.is_finished(self._view):
            return False
        color = self.my_color
        if color is None:
            return False
        return (color == "white") == game.is_white_turn(self._view)

    @property
    def url(self) -> str:
        """Browser URL of the room."""
        return ROOM_URL_TEMPLATE.format(room_id=self.room_id)

    def status_line(self) -> str:
        """Short one line status suitable for chat."""
        room = self.room
        if not self.connected and room.version == 0:
            return "未连接房间"
        parts = [f"房间 {self.room_id}", self.url]
        if room.state == 1:
            color = {"white": "白方", "black": "黑方"}.get(self.my_color or "", "观战")
            turn = "对局中"
            if self._view is not None:
                side = "白方" if game.is_white_turn(self._view) else "黑方"
                if game.is_finished(self._view):
                    turn = "对局结束"
                else:
                    turn = f"轮到{side}"
            if game.is_finished(self._view) if self._view else False:
                turn = "对局结束"
            parts.append(f"{turn}（你是{color}）")
        elif room.position:
            waiting = min(len(room.player_list), 2)
            seated = sum(1 for player in room.player_list if player.occupied)
            parts.append(f"等待开始（{seated}/{waiting} 人已入座）")
        else:
            parts.append("观战中")
        return " | ".join(parts)

    # -------------------------------------------------------------- lifecycle

    async def open(self) -> tuple[bool, str]:
        """Connect to the room and, when needed, take a seat.

        Returns:
            ``(ok, message)`` describing the result.
        """
        self._conn = protocol.RoomConnection(
            self.room_id,
            on_room=self._on_room,
            on_event=self._on_event,
            on_close=self._on_close,
            logger=self.logger,
        )
        try:
            initial = await self._conn.connect()
        except (protocol.RoomConnectionError, asyncio.TimeoutError) as exc:
            return False, f"连接房间失败：{exc}"

        # A brand new room has no owner yet; joining slot 0 makes us the owner and
        # the white player.  Existing rooms: take the first free seat.
        if initial.owner == 0:
            self._my_seat = 1
            self.created_room = True
        else:
            self._my_seat = self._first_free_seat(initial) or 0

        if not self._my_seat:
            return False, "加入房间失败：没有空余座位（可先观战）"

        await self._conn.join_game(self._my_seat, self.config.emoji)
        try:
            await self._wait_for(lambda: self.room.position > 0, timeout=8.0)
        except asyncio.TimeoutError:
            return False, "加入座位超时，请稍后重试"
        self._my_index = self.my_index

        if self.created_room:
            self._notify(NOTIFY_ROOM_CREATED, self.url)
        self._notify(NOTIFY_INFO, self.status_line())
        if self.room.state == 0 and self._view is None:
            self._notify(NOTIFY_INFO, self._waiting_hint())
        return True, self.status_line()

    def _waiting_hint(self) -> str:
        if self.created_room or self.is_owner:
            return "把房间链接发给对手，等对方在网页加入座位后我会自动开局"
        return "房间已加入，等待房主在网页上开始游戏"

    async def leave_seat(self) -> tuple[bool, str]:
        """Leave the seat properly, ending the game first when necessary.

        The site silently ignores ``PlayerChangeSeat`` while a game is running,
        so an owner who leaves mid-game must first publish ``OwnerExitGame``
        (the same thing the page's "结束游戏" button does).  Only after the room
        is back to the waiting state can the seat be released.

        Returns:
            ``(ok, message)`` where ``message`` is suitable for the chat.
        """
        if not self.connected or self._conn is None:
            return False, "尚未连接房间"
        if not self.room.position:
            return True, "当前不在座位上"
        if self.room.state == 1:
            if not self.is_owner:
                return False, "对局进行中且机器人不是房主，无法离座；可等房主结束对局后再离开"
            with contextlib.suppress(Exception):
                await self._conn.owner_exit_game(self.room.game_version)
            try:
                await self._wait_for(lambda: self.room.state == 0, timeout=4.0)
            except asyncio.TimeoutError:
                return False, "结束对局的请求没有得到服务器确认，座位可能仍然占用"
        with contextlib.suppress(Exception):
            await self._conn.change_seat(0)
        try:
            await self._wait_for(lambda: self.room.position == 0, timeout=3.0)
        except asyncio.TimeoutError:
            return False, "服务器没有确认离座，座位可能仍然占用"
        return True, "已离开座位"

    async def take_seat(self, seat: int | None = None) -> tuple[bool, str]:
        """Take a seat in the (already joined) room.

        Args:
            seat: 1-based seat number; when omitted the first free seat is used.

        Returns:
            ``(ok, message)``.
        """
        if not self.connected or self._conn is None:
            return False, "尚未连接房间"
        if self.room.position:
            return False, f"已经在 {self.room.position} 号座位上"
        if seat is None:
            seat = self._first_free_seat(self.room) or 1
        max_seat = max(2, len(self.room.player_list))
        if seat < 1 or seat > max_seat:
            return False, f"座位号需要在 1-{max_seat} 之间"
        await self._conn.join_game(seat, self.config.emoji)
        try:
            await self._wait_for(lambda: self.room.position == seat, timeout=4.0)
        except asyncio.TimeoutError:
            current = self.room.position
            if current:
                return False, f"入座超时，当前在 {current} 号座位"
            return False, "入座超时，座位可能已被占用"
        return True, f"已入座 {seat} 号位"

    async def close(self, *, graceful: bool = True) -> None:
        """Leave the room and release all tasks.

        When ``graceful`` is set, a ``PlayerChangeSeat`` (leave-seat) is sent and
        confirmed *before* the websocket is closed: merely dropping the socket
        keeps the seat occupied on the site.
        """
        if self._closing:
            return
        self._closing = True
        self._closed = True
        for task in (self._ai_task, self._auto_start_task, self._reconnect_task):
            if task is not None and not task.done():
                task.cancel()
        self.last_leave_message = ""
        if self._conn is not None and graceful and self._conn.is_connected:
            try:
                ok, message = await self.leave_seat()
                self.last_leave_message = message
                if not ok:
                    self.logger.warning("离开座位失败：%s", message)
            except Exception as exc:  # noqa: BLE001 - closing must never raise
                self.last_leave_message = f"离座时出错：{exc}"
                self.logger.warning("离开座位时出错：%s", exc)
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._notify(NOTIFY_CLOSED, self.last_leave_message or "已离开房间")

    # ------------------------------------------------------------ callbacks

    async def _on_room(self, conn: protocol.RoomConnection) -> None:
        room = conn.room
        self._my_index = max(0, room.position - 1)
        seated = sum(1 for player in room.player_list if player.occupied)
        if seated > self._known_players and self._known_players > 0:
            opponent = self._opponent_player()
            if opponent is not None and opponent.occupied:
                self._notify(NOTIFY_OPPONENT_JOINED, "对手已加入房间")
        self._known_players = seated

        ended_game = self._room_state == 1 and room.state == 0
        if ended_game:
            # The owner ended the game / everyone went back to the waiting room.
            self._notified_start = False
            self._game_over_notified = False
            self._last_auto_turn_key = None
            self._ai_task = None
            self._view = None
            self._view_key = None
            self._pending_move_version = None
            self._notify(NOTIFY_INFO, "对局已结束，房间回到等待状态")
        elif self._room_state == 0 and room.state == 1:
            # A new game started: never inherit the previous game's state.
            self._game_over_notified = False
            self._last_auto_turn_key = None
        self._room_state = room.state

        if ended_game:
            # Do not decode the stale game bytes some room frames still carry.
            self._maybe_auto_start()
            return

        self._refresh_view()
        if self.room.state == 1:
            self._handle_game_update()
        elif self.room.state == 0:
            self._maybe_auto_start()
        self._maybe_schedule_ai()

    async def _on_event(self, conn: protocol.RoomConnection, event: proto.ServerEvent) -> None:
        if event.key == proto.SERVER_UPDATE_GAME_DATA:
            self._refresh_view()
            self._handle_game_update()
            self._maybe_schedule_ai()
        elif event.key == proto.SERVER_GAME_STARTED:
            self.logger.info("房间 %s 游戏开始（payload=%s）", self.room_id, event.payload)
            self._refresh_view()
            self._handle_game_update()
            self._maybe_schedule_ai()

    async def _on_close(self, conn: protocol.RoomConnection, code: int, reason: str) -> None:
        if self._closing or self._closed:
            return
        description = protocol.describe_close(code)
        self._notify(NOTIFY_ERROR, f"与房间的连接断开了：{description}")
        if code in (
            proto.CLOSE_SAME_USER_LOGIN,
            proto.CLOSE_OWNER_KICK_OUT,
            proto.CLOSE_BAD_COOKIE,
            proto.CLOSE_NOT_LOGIN,
            proto.CLOSE_GAME_NOT_EXIST,
        ):
            self._closed = True
            self._notify(NOTIFY_CLOSED, "房间连接已终止")
            return
        self._reconnect_task = asyncio.create_task(self._reconnect(code), name=f"gjxq-reconnect-{self.room_id}")

    async def _reconnect(self, code: int) -> None:
        for attempt, delay in enumerate(RECONNECT_DELAYS, start=1):
            await asyncio.sleep(delay + random.random())
            if self._closing or self._closed:
                return
            self.logger.info("尝试重连房间 %s（第 %s 次）", self.room_id, attempt)
            conn = protocol.RoomConnection(
                self.room_id,
                on_room=self._on_room,
                on_event=self._on_event,
                on_close=self._on_close,
                logger=self.logger,
            )
            self._conn = conn
            try:
                await conn.connect()
            except (protocol.RoomConnectionError, asyncio.TimeoutError) as exc:
                self.logger.warning("重连失败：%s", exc)
                continue
            if self._my_seat and self.room.position == 0:
                with contextlib.suppress(Exception):
                    await conn.join_game(self._my_seat, self.config.emoji)
                    await asyncio.sleep(0.5)
            if self.room.state == 1:
                self._refresh_view()
                self._handle_game_update()
                self._maybe_schedule_ai()
            self._notify(NOTIFY_INFO, "已重新连接房间")
            return
        self._closed = True
        self._notify(NOTIFY_CLOSED, "多次重连失败，已退出房间")

    # ----------------------------------------------------------- game events

    def _refresh_view(self) -> None:
        room = self.room
        # Only an active game has meaningful game data.  After OwnerExitGame the
        # server resets gameVersion/gameTime to 0 but room frames may still carry
        # a (stale) copy of the previous game's bytes; decoding those would bring
        # the finished game back and block the next game's AI.
        if room.state != 1:
            return
        if not room.game_data:
            return
        key = (room.game_version, room.game_time, len(room.game_data))
        if self._view_key == key:
            return
        # The room clock (game_time) is monotonically increasing; the game
        # version resets to 1 for every new game, so it must not be used as the
        # primary ordering key.
        previous_time = self._view_key[1] if self._view_key is not None else None
        if previous_time is not None and room.game_time < previous_time:
            return
        try:
            view = game.decode_game_data(room.game_data)
        except Exception as exc:  # noqa: BLE001 - malformed snapshots should not kill the session
            self.logger.warning("解码棋局数据失败：%s", exc)
            return
        previous = self._view
        self._view_key = key
        self._view = view
        self._pending_move_version = None
        if previous is not None:
            self._track_moves(previous, view)

    def _handle_game_update(self) -> None:
        view = self._view
        room = self.room
        if view is None or room.state != 1:
            return
        if room.state == 1 and not self._notified_start:
            self._notified_start = True
            self._notify(NOTIFY_STARTED, f"对局开始，你执{'白' if self.my_color == 'white' else '黑'}方")
            self._notify(NOTIFY_BOARD, "")
        if game.is_finished(view):
            # A finished position is not a turn: drop any scheduled AI move.
            self._last_auto_turn_key = None
            if self._ai_task is not None and not self._ai_task.done():
                self._ai_task.cancel()
            if not self._game_over_notified:
                self._game_over_notified = True
                if view.state == game.STATE_WHITE_WIN:
                    result = "白方获胜"
                else:
                    result = "黑方获胜"
                mine = self.my_color
                if mine == "white":
                    result += "，你赢了 🎉" if view.state == game.STATE_WHITE_WIN else "，你输了"
                elif mine == "black":
                    result += "，你赢了 🎉" if view.state == game.STATE_BLACK_WIN else "，你输了"
                self._notify(NOTIFY_GAME_OVER, result)
        elif self.is_my_turn:
            if self.config.ai_enabled:
                self._notify(NOTIFY_YOUR_TURN, "轮到你走棋，AI 正在思考…")
            else:
                self._notify(NOTIFY_YOUR_TURN, "轮到你走棋")

    def _track_moves(self, previous: game.View, current: game.View) -> None:
        old_records = previous.records
        new_records = current.records
        if len(new_records) <= len(old_records):
            return
        added = new_records[: len(new_records) - len(old_records)]
        for record in reversed(added):
            info = self._describe_record(current, record)
            self.last_moves.append(info)
            if self.my_color is not None and info.is_white != (self.my_color == "white"):
                self._notify(NOTIFY_OPPONENT_MOVED, info.label)

    def _describe_record(self, view: game.View, record: game.Record) -> MoveInfo:
        before_square = game.pos_to_square(record.before)
        after_square = game.pos_to_square(record.after)
        promotion_type: int | None = None
        uci = f"{before_square}{after_square}"
        if record.sp == game.SP_PROMOTION:
            piece = view.pieces[record.id] if record.id < len(view.pieces) else None
            if piece is not None:
                promotion_type = piece.type
                uci += PROMOTION_LETTERS.get(piece.type, "q")
        is_white = record.id < 16
        label = uci
        if record.eat is not None:
            label += " 吃子"
        if record.sp == game.SP_CASTLING:
            label = f"{before_square}{after_square} 王车易位"
        elif record.sp == game.SP_PROMOTION:
            label = f"{before_square}{after_square} 升变"
        return MoveInfo(
            record=record,
            uci=uci,
            is_white=is_white,
            promotion_type=promotion_type,
            label=label,
        )

    # ---------------------------------------------------------------- actions

    async def start_game(self, *, shuffle: bool = False) -> tuple[bool, str]:
        """Start the game (owner only) with a fresh initial position."""
        if not self.connected:
            return False, "尚未连接房间"
        if not self.is_owner:
            return False, "只有房主可以开始游戏"
        if self.room.state == 1:
            return False, "对局已经开始了"
        seated = sum(1 for player in self.room.player_list if player.occupied)
        if seated < 2:
            return False, "需要两名玩家入座后才能开始"
        data = game.encode_game_data(game.initial_view(white_id=0))
        ok = await self._conn.start_game(data, shuffle=shuffle)  # type: ignore[union-attr]
        if not ok:
            return False, "开始游戏的消息发送失败"
        with contextlib.suppress(asyncio.TimeoutError):
            await self._wait_for(lambda: self.room.state == 1, timeout=6.0)
        if self.room.state != 1:
            return False, "服务器没有响应开始游戏的请求"
        return True, "对局开始"

    async def play_uci(self, uci: str, *, source: str = "player") -> tuple[bool, str]:
        """Validate and publish a move in UCI notation (``e2e4``, ``e7e8q``)."""
        move = self._parse_uci(uci)
        if move is None:
            return False, f"无法识别的走法：{uci}（示例：e2e4、g1f3、e7e8q）"
        before_square, after_square, promotion_type = move
        view = self._view
        if view is None or not self.is_playing:
            return False, "当前没有进行中的对局"
        if not self.is_my_turn:
            return False, "还没轮到你走棋"
        if self._pending_move_version is not None:
            return False, "上一步还在等待服务器确认，请稍候"
        before = game.square_to_pos(before_square)
        after = game.square_to_pos(after_square)
        piece = game.piece_at(view, before)
        if piece is None:
            return False, f"{before_square} 上没有棋子"
        expected_white = self.my_color == "white"
        if (piece.id < 16) != expected_white:
            return False, "不能移动对方的棋子"
        if promotion_type is None and piece.type == game.PIECE_PAWN and after in range(0, 8):
            promotion_type = game.PIECE_QUEEN
        new_view, error = game.apply_move(view, piece.id, after, promotion_type)
        if new_view is None or error:
            return False, f"这步棋不能走：{error or '非法走法'}"
        data = game.encode_game_data(new_view)
        version = self.room.game_version
        self._pending_move_version = version
        ok = await self._conn.update_game_data(version, data)  # type: ignore[union-attr]
        if not ok:
            self._pending_move_version = None
            return False, "发送走棋消息失败，请重试"
        # The server echoes the snapshot through an event; wait briefly so the
        # next command sees the new version.
        with contextlib.suppress(asyncio.TimeoutError):
            await self._wait_for(lambda: self.room.game_version != version, timeout=4.0)
        if self.room.game_version == version:
            self._pending_move_version = None
            return False, "服务器没有确认这步棋，请重试"
        if source == "ai":
            # Human moves are answered by the command handler that called us;
            # only AI moves need a proactive push.
            self._notify(NOTIFY_MOVED, f"AI 走：{before_square}{after_square}")
        return True, f"已走：{before_square}{after_square}"

    def set_ai_enabled(self, enabled: bool) -> None:
        """Turn automatic AI play on or off at runtime.

        Args:
            enabled: ``True`` schedules a move when it is already our turn,
                ``False`` cancels a pending AI move.
        """
        self.config.ai_enabled = bool(enabled)
        if enabled:
            self._last_auto_turn_key = None
            self._maybe_schedule_ai()
            return
        if self._ai_task is not None and not self._ai_task.done():
            self._ai_task.cancel()
        self._ai_task = None
        self._last_auto_turn_key = None

    def set_elo(self, elo: int | None) -> int:
        """Set the approximate Elo difficulty for upcoming AI moves.

        Args:
            elo: 600-2400, or ``None`` / ``0`` to disable Elo mode and fall back
                to ``ai_depth`` / ``ai_time_limit``.

        Returns:
            The clamped/effective Elo value (0 means Elo mode disabled).
        """
        rating = ai.clamp_elo(elo)
        self.config.ai_elo = rating
        return rating

    def _parse_uci(self, text: str) -> tuple[str, str, int | None] | None:
        cleaned = text.strip().lower().replace(" ", "").replace("-", "").replace("－", "")
        if len(cleaned) not in (4, 5):
            return None
        files = "abcdefgh"
        ranks = "12345678"
        if cleaned[0] not in files or cleaned[2] not in files:
            return None
        if cleaned[1] not in ranks or cleaned[3] not in ranks:
            return None
        before = cleaned[0:2]
        after = cleaned[2:4]
        promotion: int | None = None
        if len(cleaned) == 5:
            promotion = PROMOTION_TYPES.get(cleaned[4])
            if promotion is None:
                return None
        return before, after, promotion

    async def resign(self) -> tuple[bool, str]:
        """Resign the current game (allowed even when it is not our turn)."""
        view = self._view
        if view is None or not self.is_playing:
            return False, "当前没有进行中的对局"
        if self.my_color is None:
            return False, "观战者无法认输"
        if game.is_finished(view):
            return False, "对局已经结束"
        new_state = game.STATE_BLACK_WIN if self.my_color == "white" else game.STATE_WHITE_WIN
        # Encode with the real position's turn and only patch the winner state:
        # flipping the state can invert the side to move implied by the records.
        data = game.encode_game_data(view, state_override=new_state)
        version = self.room.game_version
        ok = await self._conn.update_game_data(version, data)  # type: ignore[union-attr]
        if not ok:
            return False, "发送认输消息失败"
        self._adopt_local_state(new_state)
        with contextlib.suppress(asyncio.TimeoutError):
            await self._wait_for(lambda: self.room.game_version != version, timeout=4.0)
        return True, "已认输"

    async def claim_win(self) -> tuple[bool, str]:
        """Claim the win when the opponent has no legal move left."""
        view = self._view
        if view is None or not self.is_playing or self.my_color is None:
            return False, "当前无法宣布胜利"
        if game.is_finished(view):
            return False, "对局已经结束"
        if game.legal_moves(view):
            return False, "对手仍有合法走法，不能宣布胜利"
        new_state = game.STATE_WHITE_WIN if self.my_color == "white" else game.STATE_BLACK_WIN
        data = game.encode_game_data(view, state_override=new_state)
        ok = await self._conn.update_game_data(self.room.game_version, data)  # type: ignore[union-attr]
        if not ok:
            return False, "发送宣布胜利消息失败"
        self._adopt_local_state(new_state)
        return True, "已宣布胜利"

    def _adopt_local_state(self, state: int) -> None:
        """Optimistically reflect a terminal state we just published."""
        view = self._view
        if view is None:
            return
        self._view = game.View(
            white_id=view.white_id,
            state=state,
            pieces=[game.Piece(p.id, p.pos, p.type, p.moved) for p in view.pieces],
            records=list(view.records),
            back_piece_id=view.back_piece_id,
            back_piece_before=view.back_piece_before,
        )
        self._handle_game_update()

    async def restart(self) -> tuple[bool, str]:
        """Restart the game as white (site's "重开，我执白" button)."""
        view = self._view
        if view is None or not game.is_finished(view):
            return False, "对局尚未结束，不能重开"
        if self.my_color is None:
            return False, "观战者无法重开对局"
        data = game.encode_game_data(game.initial_view(white_id=self.my_index))
        ok = await self._conn.update_game_data(self.room.game_version, data)  # type: ignore[union-attr]
        if not ok:
            return False, "发送重开消息失败"
        self._game_over_notified = False
        self._notified_start = False
        return True, "已重开对局（你执白）"

    # -------------------------------------------------------------- auto play

    def _maybe_auto_start(self) -> None:
        if not self.config.auto_start or not self.is_owner or self.room.state != 0:
            return
        if self._auto_start_task is not None and not self._auto_start_task.done():
            return
        if not self._both_players_ready():
            return
        self._auto_start_task = asyncio.create_task(
            self._auto_start(), name=f"gjxq-autostart-{self.room_id}"
        )

    def _both_players_ready(self) -> bool:
        players = self.room.player_list
        if len(players) < 2:
            return False
        seated = [player for player in players[:2]]
        return all(player.occupied for player in seated)

    async def _auto_start(self) -> None:
        await asyncio.sleep(1.0)
        for _attempt in range(3):
            if self._closed or self.room.state != 0:
                return
            if not self._both_players_ready():
                return
            ok, message = await self.start_game()
            if ok:
                return
            # A concurrent room update (join/emoji/seat change) bumps room.version;
            # the server silently ignores a start with a stale version, so retry.
            self.logger.warning("自动开始游戏失败，重试：%s", message)
            await asyncio.sleep(1.0)

    def _maybe_schedule_ai(self) -> None:
        if not self.config.ai_enabled or not self.is_my_turn:
            return
        if self._view is None or game.is_finished(self._view):
            return
        version = self.room.game_version
        key = (version, len(self._view.records))
        if self._last_auto_turn_key == key and self._ai_task is not None and not self._ai_task.done():
            return
        # A task scheduled for an older position (e.g. before a restart) must not
        # block the new turn: cancel it and schedule the current one.
        if self._ai_task is not None and not self._ai_task.done():
            self._ai_task.cancel()
        self._last_auto_turn_key = key
        self._ai_task = asyncio.create_task(self._ai_move(key), name=f"gjxq-ai-{self.room_id}")

    async def _ai_move(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(self.config.move_delay)
        except asyncio.CancelledError:
            raise
        if self._closed:
            return
        current_key = (self.room.game_version, len(self._view.records) if self._view else 0)
        if current_key != key:
            # The position changed while we were waiting (restart/new move):
            # schedule for the new key instead of silently doing nothing.
            if self.is_my_turn:
                self._maybe_schedule_ai()
            return
        if not self.is_my_turn:
            return
        if not ai.AVAILABLE:
            self._notify(NOTIFY_ERROR, "缺少 python-chess 依赖，无法自动走棋，请先发送 /象棋 走 <走法>")
            return
        view = self._view
        if view is None:
            return
        fen = game.view_to_fen(view)
        try:
            uci = await asyncio.to_thread(
                ai.choose_move,
                fen,
                self.config.ai_depth,
                self.config.ai_time_limit,
                self.config.ai_elo or None,
            )
        except Exception as exc:  # noqa: BLE001 - AI errors must not kill the session
            self.logger.warning("AI 走棋失败：%s", exc)
            self._notify(NOTIFY_ERROR, f"AI 走棋出错，请手动走棋：{exc}")
            return
        if not uci:
            self._notify(NOTIFY_ERROR, "AI 没有找到合法走法，请手动走棋")
            return
        if self._closed or not self.is_my_turn:
            return
        if (self.room.game_version, len(view.records)) != key:
            return
        ok, message = await self.play_uci(uci, source="ai")
        if not ok:
            self.logger.warning("AI 走棋被拒绝：%s", message)
            self._notify(NOTIFY_ERROR, f"AI 走棋失败：{message}")

    # ---------------------------------------------------------------- helpers

    def _first_free_seat(self, room: proto.Room) -> int:
        for index, player in enumerate(room.player_list):
            if not player.occupied:
                return index + 1
        return 0

    def _opponent_player(self) -> proto.PlayerInfo | None:
        room = self.room
        if not room.position:
            return None
        index = 0 if self.my_index == 1 else 1
        if index < len(room.player_list):
            return room.player_list[index]
        return None

    async def _wait_for(self, predicate: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.05)
        raise asyncio.TimeoutError

    def _notify(self, kind: str, text: str) -> None:
        try:
            result = self.notify(kind, text)
        except Exception as exc:  # noqa: BLE001 - notify callback must not break state machine
            self.logger.warning("notify 回调异常：%s", exc)
            return
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)


@dataclass
class RoomManager:
    """Tracks the room sessions of every chat (unified message origin).

    Multiple rooms may run in the same chat ("多开"); ``active`` remembers which
    room the plain commands (:attr:`get` without ``room_id``) refer to.
    """

    sessions: dict[str, dict[str, RoomSession]] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, room_id: str | None = None) -> RoomSession | None:
        """Return the active (or the given) room session of a chat."""
        rooms = self.sessions.get(key)
        if not rooms:
            return None
        if room_id is not None:
            return rooms.get(room_id)
        active_id = self.active.get(key)
        if active_id is not None and active_id in rooms:
            return rooms[active_id]
        return next(reversed(rooms.values()), None)

    def all_for(self, key: str) -> list[RoomSession]:
        """All room sessions of one chat, in creation order."""
        return list(self.sessions.get(key, {}).values())

    def room_ids(self, key: str) -> list[str]:
        """Room ids of one chat, in creation order."""
        return list(self.sessions.get(key, {}).keys())

    def add(self, key: str, session: RoomSession) -> None:
        """Register a room session and make it the active one."""
        self.sessions.setdefault(key, {})[session.room_id] = session
        self.active[key] = session.room_id

    def set_active(self, key: str, room_id: str) -> bool:
        """Select the active room of a chat; True when it exists."""
        if room_id not in self.sessions.get(key, {}):
            return False
        self.active[key] = room_id
        return True

    def remove(self, key: str, session: RoomSession | str) -> RoomSession | None:
        """Remove and return one room session (does not close it)."""
        rooms = self.sessions.get(key)
        if not rooms:
            return None
        room_id = session if isinstance(session, str) else session.room_id
        removed = rooms.pop(room_id, None)
        if not rooms:
            self.sessions.pop(key, None)
            self.active.pop(key, None)
        elif self.active.get(key) == room_id:
            self.active[key] = next(reversed(rooms.keys()), "")
        return removed

    def count(self) -> int:
        """Total number of room sessions across all chats."""
        return sum(len(rooms) for rooms in self.sessions.values())

    async def close(self, key: str, room_id: str | None = None) -> bool:
        """Close one room (the active one when ``room_id`` is omitted)."""
        session = self.get(key, room_id)
        if session is None:
            return False
        self.remove(key, session)
        await session.close()
        return True

    async def close_all(self) -> None:
        """Close every room session of every chat."""
        for key in list(self.sessions):
            for session in list(self.sessions.get(key, {}).values()):
                self.remove(key, session)
                await session.close()




