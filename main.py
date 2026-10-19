"""AstrBot plugin: play 国际象棋 in hullqin gjxq rooms from chat.

Command surface (all Chinese, registered with ``@filter.command``)::

    /象棋 帮助             show the command list
    /象棋 创建 [房间号]    create a room (random 5-char id when omitted)
    /象棋 加入 <房间号>    join an existing room
    /象棋 列表             list the rooms open in this chat (multi-room)
    /象棋 切换 <房间号>    make one room the active one
    /象棋 开始             owner starts the game once both seats are taken
    /象棋 棋盘             render the current board
    /象棋 状态             room + game status, players, recent moves
    /象棋 走 <走法>        play a move (e2e4, g1f3, e7e8q)
    /象棋 AI 开|关         toggle automatic AI play for this chat
    /象棋 难度 [Elo|关]    show/set the approximate Elo AI difficulty
    /象棋 离开座位         leave the seat (stay in the room as spectator)
    /象棋 入座 [座位号]    take a seat again (also used after leaving it)
    /象棋 认输             resign the current game
    /象棋 重开             restart after a finished game (bot plays white)
    /象棋 离开 [房间号]    disconnect from one room (no id: the active room)
    /象棋 房间             show room id / URL / seat info

Only these commands trigger the plugin.  Room state lives in
``chess_site.session.RoomSession``; this module owns per-chat session lookup,
AstrBot replies and proactive notifications.

Board pushes are **off by default**: move notifications only send text and the
board is only rendered on demand (``/象棋 棋盘``).  When board pushes are enabled
they can be batched: ``board_batch_plies`` merges that many moves into one
message to avoid flooding the chat.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
from pathlib import Path
from typing import Any, Optional

_PLUGIN_DIR = Path(__file__).resolve().parent

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.api import AstrBotConfig, logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # noqa: E402
from astrbot.api.star import Context, Star, register  # noqa: E402

try:  # AstrBot imports this file as ``data.plugins.<plugin_dir>.main``.
    from .chess_site import ai, render
    from .chess_site.session import (
        NOTIFY_BOARD,
        NOTIFY_CLOSED,
        NOTIFY_ERROR,
        NOTIFY_GAME_OVER,
        NOTIFY_MOVED,
        NOTIFY_OPPONENT_MOVED,
        NOTIFY_STARTED,
        NOTIFY_YOUR_TURN,
        RoomManager,
        RoomSession,
        SessionConfig,
    )
except ImportError:  # pragma: no cover - top-level loading (tests / flattened loaders)
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    from chess_site import ai, render
    from chess_site.session import (
        NOTIFY_BOARD,
        NOTIFY_CLOSED,
        NOTIFY_ERROR,
        NOTIFY_GAME_OVER,
        NOTIFY_MOVED,
        NOTIFY_OPPONENT_MOVED,
        NOTIFY_STARTED,
        NOTIFY_YOUR_TURN,
        RoomManager,
        RoomSession,
        SessionConfig,
    )

PLUGIN_NAME = "astrbot_plugin_gjxq_chess"
PLUGIN_VERSION = "1.3.1"

ROOM_URL_TEMPLATE = "https://game.hullqin.cn/gjxq/{room_id}"
ROOM_ID_PATTERN = re.compile(r"^[a-z0-9]{3,8}$")
ROOM_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"

HELP_TEXT = """国际象棋联机插件 v{version}

/象棋 创建 [房间号]   创建房间（不填则随机 5 位房间号）
/象棋 加入 <房间号>   加入别人的房间
/象棋 列表            查看本会话已开的房间（支持多开）
/象棋 切换 <房间号>   切换当前操作的房间
/象棋 开始            房主在两人入座后开始对局
/象棋 棋盘            查看当前棋盘
/象棋 状态            查看房间、对局与最近走棋
/象棋 走 <走法>       走棋，如 e2e4、g1f3、e7e8q
/象棋 AI 开|关        开关自动 AI 走棋
/象棋 难度 [Elo|关]   查看或设置 AI 难度（近似的 Elo 分，600-2400）
/象棋 离开座位        离开座位（留在房间观战，不断开连接）
/象棋 入座 [座位号]   重新入座（默认第一个空位）
/象棋 认输            认输
/象棋 重开            对局结束后重开（机器人执白）
/象棋 离开 [房间号]   离开房间（不填则离开当前房间）
/象棋 房间            查看房间信息和链接

房间链接：https://game.hullqin.cn/gjxq/<房间号>
提示：走法使用“起点+终点”格式且不带空格；双方都入座后才能开始对局。
默认不主动推送棋盘（可在配置中开启，并按 board_batch_plies 合并推送）。
AI 难度可用 /象棋 难度 1000 调整（600/800/1000/1200/1500/1800/2000/2400）。
离开房间会自动先离座（对局中且机器人为房主时会先结束对局，和网页的“结束游戏”一致）。""".format(
    version=PLUGIN_VERSION
)

NO_SESSION_HINT = """当前会话还没有房间。
创建房间：/象棋 创建 [房间号]
加入房间：/象棋 加入 <房间号>
发送 /象棋 帮助 查看全部指令。"""


@register(
    PLUGIN_NAME,
    "dsh",
    "在 HullQin 游戏大厅创建/加入国际象棋房间：文本棋盘、聊天走棋、可选自动 AI 对战",
    PLUGIN_VERSION,
)
class GjxqChessPlugin(Star):
    """Chat front-end for the hullqin gjxq (国际象棋) rooms."""

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None) -> None:
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        self._sessions = RoomManager()
        self._notify_tasks: set[asyncio.Task] = set()
        # (chat, room) pairs whose next "closed" notification should not be pushed
        # to the user (the command handler already answered them).
        self._quiet_close: set[tuple[str, str]] = set()
        # Pending move lines per (chat, room) used to batch board/text pushes.
        self._move_buffers: dict[tuple[str, str], list[str]] = {}
        self._terminating = False

    # ------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        """Log readiness and warn when the optional AI dependency is missing."""
        logger.info(
            "[gjxq] 国际象棋联机插件 v%s 已加载（%s）",
            PLUGIN_VERSION,
            ROOM_URL_TEMPLATE.format(room_id="<房间号>"),
        )
        if not ai.AVAILABLE:
            logger.warning(
                "[gjxq] 未安装 python-chess，自动 AI 走棋不可用；"
                "安装依赖后重载插件即可，也可用 /象棋 走 手动走棋。",
            )

    async def terminate(self) -> None:
        """Leave all rooms and stop background notification pushes."""
        self._terminating = True
        try:
            await self._sessions.close_all()
        except Exception as exc:  # noqa: BLE001 - unload must never raise
            logger.warning("[gjxq] 关闭房间时出错：%s", exc)
        for task in list(self._notify_tasks):
            task.cancel()
        self._notify_tasks.clear()
        self._move_buffers.clear()

    # -------------------------------------------------------------- config

    def _conf(self, key: str, default: Any) -> Any:
        """Read one plugin config value with a hard-coded fallback."""
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001 - config may be a plain mapping or None
            return default
        return default if value is None else value

    def _conf_bool(self, key: str, default: bool) -> bool:
        value = self._conf(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开", "是")
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _conf_int(
        self,
        key: str,
        default: int,
        *,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        try:
            value = int(float(self._conf(key, default)))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _conf_float(
        self,
        key: str,
        default: float,
        *,
        minimum: Optional[float] = None,
    ) -> float:
        try:
            value = float(self._conf(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _conf_str(self, key: str, default: str) -> str:
        value = self._conf(key, default)
        return value if isinstance(value, str) else default

    def _session_config(self) -> SessionConfig:
        """Build the per-room tunables from the plugin config."""
        return SessionConfig(
            ai_enabled=self._conf_bool("ai_enabled", True),
            ai_depth=self._conf_int("ai_depth", 3, minimum=1, maximum=6),
            ai_time_limit=self._conf_float("ai_time_limit", 2.0, minimum=0.1),
            ai_elo=self._conf_int("ai_elo", 0, minimum=0, maximum=3000),
            auto_start=self._conf_bool("auto_start", True),
            move_delay=self._conf_float("move_delay", 0.8, minimum=0.0),
            emoji=self._conf_str("emoji", "😀") or "😀",
        )

    def _max_sessions(self) -> int:
        return self._conf_int("max_sessions", 5, minimum=1)

    def _move_push_enabled(self) -> bool:
        """Whether opponent/AI move notifications are pushed at all."""
        return self._conf_bool("notify_opponent_move", False)

    def _board_push_enabled(self) -> bool:
        """Whether proactive messages may include the rendered board."""
        return self._conf_bool("board_push_enabled", False)

    def _batch_size(self) -> int:
        """How many move notifications to merge into one message (1 = no batch)."""
        return self._conf_int("board_batch_plies", 4, minimum=1, maximum=40)

    # ------------------------------------------------------- session wiring

    def _build_session(self, room_id: str, umo: str, *, room_created: bool) -> RoomSession:
        """Create a session whose notifications are pushed to ``umo``."""

        def notify(kind: str, text: str) -> None:
            self._on_notify(umo, session, kind, text)

        session = RoomSession(
            room_id,
            notify,
            config=self._session_config(),
            room_created=room_created,
        )
        return session

    def _drop_session(self, umo: str, session: RoomSession) -> None:
        self._sessions.remove(umo, session)
        self._move_buffers.pop((umo, session.room_id), None)

    # --------------------------------------------------- proactive messages

    def _schedule_push(self, coro: Any) -> None:
        """Run one notification coroutine in the background."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            logger.warning("[gjxq] 没有运行中的事件循环，丢弃一条主动消息")
            return
        task = loop.create_task(coro)
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _push_text(self, umo: str, text: str) -> None:
        """Send one plain-text proactive message to the chat ``umo``."""
        if not text:
            return
        try:
            chain = MessageChain(chain=[Comp.Plain(text)])
            await self.context.send_message(umo, chain)
        except Exception as exc:  # noqa: BLE001 - pushing must never crash the session
            logger.warning("[gjxq] 主动推送消息失败：%s", exc)

    def _room_tag(self, umo: str, session: RoomSession) -> str:
        """Prefix for pushes when several rooms are open in the same chat."""
        if len(self._sessions.all_for(umo)) > 1:
            return f"【{session.room_id}】"
        return ""

    def _on_notify(self, umo: str, session: RoomSession, kind: str, text: str) -> None:
        """Translate a session notification into (possibly batched) chat pushes."""
        if self._terminating:
            return
        try:
            buffer_key = (umo, session.room_id)
            if kind == NOTIFY_CLOSED:
                quiet = buffer_key in self._quiet_close
                self._quiet_close.discard(buffer_key)
                self._flush_moves(umo, session, drop_only=True)
                self._drop_session(umo, session)
                if not quiet and text:
                    self._schedule_push(self._push_text(umo, f"{self._room_tag(umo, session)}{text}"))
                return
            if kind == NOTIFY_ERROR:
                if text:
                    self._schedule_push(self._push_text(umo, f"⚠️ {self._room_tag(umo, session)}{text}"))
                return
            if kind == NOTIFY_BOARD:
                if self._board_push_enabled():
                    self._schedule_push(self._push_text(umo, self._board_text(session)))
                return
            if kind == NOTIFY_STARTED:
                if text:
                    body = f"♟️ {self._room_tag(umo, session)}{text}"
                    if self._board_push_enabled():
                        body += "\n" + self._board_text(session)
                    self._schedule_push(self._push_text(umo, body))
                return
            if kind == NOTIFY_OPPONENT_MOVED:
                if not self._move_push_enabled():
                    return
                self._buffer_move(umo, session, f"对手走棋：{text}")
                return
            if kind == NOTIFY_YOUR_TURN:
                if session.config.ai_enabled or not text:
                    return
                if not self._move_push_enabled():
                    return
                body = f"⏳ {self._room_tag(umo, session)}{text}"
                if self._board_push_enabled():
                    body += "\n" + self._board_text(session)
                self._schedule_push(self._push_text(umo, body))
                return
            if kind == NOTIFY_GAME_OVER:
                self._flush_moves(umo, session)
                body = f"🏁 {self._room_tag(umo, session)}{text}"
                if self._board_push_enabled():
                    body += "\n" + self._board_text(session)
                self._schedule_push(self._push_text(umo, body))
                return
            if kind == NOTIFY_MOVED:
                # Only AI moves arrive here; human moves are answered by the
                # /象棋 走 handler itself.
                if not text or not self._move_push_enabled():
                    return
                self._buffer_move(umo, session, f"🤖 {text}")
                return
            # NOTIFY_INFO / NOTIFY_ROOM_CREATED are covered by command replies.
            logger.debug("[gjxq] 忽略通知 %s：%s", kind, text)
        except Exception as exc:  # noqa: BLE001 - notify must never break the session
            logger.warning("[gjxq] 处理通知 %s 失败：%s", kind, exc)

    def _buffer_move(self, umo: str, session: RoomSession, line: str) -> None:
        """Queue one move notification, flushing once the batch is full."""
        buffer = self._move_buffers.setdefault((umo, session.room_id), [])
        buffer.append(line)
        if len(buffer) >= self._batch_size():
            self._flush_moves(umo, session)

    def _flush_moves(self, umo: str, session: RoomSession, *, drop_only: bool = False) -> None:
        """Send the queued move notifications as one message (optionally discard)."""
        lines = self._move_buffers.pop((umo, session.room_id), [])
        if drop_only or not lines:
            return
        tag = self._room_tag(umo, session)
        if len(lines) == 1:
            body = f"{tag}{lines[0]}"
        else:
            body = tag + f"最近 {len(lines)} 步棋：" + "\n" + "\n".join(lines)
        if self._board_push_enabled() and session.view is not None:
            body += "\n" + self._board_text(session)
        self._schedule_push(self._push_text(umo, body))

    # ------------------------------------------------------------- rendering

    @staticmethod
    def _my_white(session: RoomSession) -> Optional[bool]:
        color = session.my_color
        if color == "white":
            return True
        if color == "black":
            return False
        return None

    def _board_text(self, session: RoomSession) -> str:
        view = session.view
        last_move = None
        if view is not None and getattr(view, "records", None):
            last_move = view.records[0]
        return render.render_board_text(
            view,
            my_white=self._my_white(session),
            last_move=last_move,
        )

    def _board_reply(self, session: RoomSession, prefix: str = "") -> str:
        parts = []
        if prefix:
            parts.append(prefix)
        status = session.status_line()
        if status:
            parts.append(status)
        parts.append(self._board_text(session))
        return "\n".join(parts)

    # -------------------------------------------------------------- helpers

    def _normalize_room_id(self, raw: str) -> str:
        """Accept a bare room id or a pasted ``.../gjxq/<id>`` URL."""
        value = (raw or "").strip()
        if not value:
            return ""
        match = re.search(r"/gjxq/([a-zA-Z0-9]{3,8})", value)
        if match:
            return match.group(1).lower()
        return value.lower()

    def _random_room_id(self) -> str:
        return "".join(random.choice(ROOM_ID_CHARS) for _ in range(5))

    def _room_url(self, room_id: str) -> str:
        return ROOM_URL_TEMPLATE.format(room_id=room_id)

    def _seat_text(self, session: RoomSession) -> str:
        """Chinese description of the bot seat / colour."""
        seat = session.seat
        color = session.my_color
        color_text = {"white": "白方", "black": "黑方"}.get(color or "", "待定")
        if not seat:
            return "观战"
        role = "房主" if session.is_owner else "玩家"
        return f"{seat} 号位（{role}，{color_text}）"

    def _ai_difficulty_text(self, session: RoomSession) -> str:
        """Human readable description of the current AI strength setting."""
        rating = int(getattr(session.config, "ai_elo", 0) or 0)
        if rating > 0:
            profile = ai.elo_profile(rating)
            return f"Elo {rating}（近似，搜索深度 {profile['depth']}）"
        return f"自定义深度 {session.config.ai_depth}（未启用 Elo）"

    def _room_state_text(self, session: RoomSession) -> str:
        if session.is_playing:
            return "对局中"
        if session.room.state == 1:
            return "对局中"
        if not session.connected:
            return "未连接"
        return "等待开始"

    def _room_line(self, index: int, session: RoomSession, active: bool) -> str:
        mark = "▶ " if active else "  "
        return (
            f"{mark}{index}. {session.room_id}｜{self._seat_text(session)}｜"
            f"{self._room_state_text(session)}"
        )

    def _no_room_reply(self, umo: str) -> str:
        rooms = self._sessions.all_for(umo)
        if not rooms:
            return NO_SESSION_HINT
        lines = [NO_SESSION_HINT, "", "本会话已有的房间："]
        for index, session in enumerate(rooms, start=1):
            lines.append(self._room_line(index, session, session is self._sessions.get(umo)))
        lines.append("发送 /象棋 切换 <房间号> 选择要操作的房间。")
        return "\n".join(lines)

    async def _close_session(self, umo: str, session: RoomSession) -> None:
        """Close one session without spamming the chat with a second message."""
        key = (umo, session.room_id)
        self._quiet_close.add(key)
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001 - closing is best effort
            logger.warning("[gjxq] 关闭房间失败：%s", exc)
        finally:
            self._quiet_close.discard(key)
            self._move_buffers.pop(key, None)
            self._sessions.remove(umo, session)

    # ------------------------------------------------------------- commands

    @filter.command("象棋 帮助", alias={"帮助象棋"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看国际象棋联机插件的全部指令。"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("象棋 创建")
    async def cmd_create(self, event: AstrMessageEvent, room_id: str = ""):
        """创建国际象棋房间（机器人成为房主/白方）；支持同一会话多开。"""
        umo = event.unified_msg_origin
        max_sessions = self._max_sessions()
        if self._sessions.count() >= max_sessions:
            yield event.plain_result(
                f"同时进行的房间数已达上限（{max_sessions} 个）。\n"
                "请先用 /象棋 离开 关闭一个房间，或在配置中调大 max_sessions。"
            )
            return

        wanted = self._normalize_room_id(room_id)
        if wanted and not ROOM_ID_PATTERN.fullmatch(wanted):
            yield event.plain_result(
                f"❌ 房间号格式不正确：{room_id}\n"
                "房间号由 3-8 位小写字母或数字组成，例如 abc12。"
            )
            return
        room = wanted or self._random_room_id()
        if self._sessions.get(umo, room) is not None:
            yield event.plain_result(
                f"❌ 本会话已经在房间 {room} 里了。\n"
                "发送 /象棋 列表 查看已有房间，或换一个房间号。"
            )
            return

        session = self._build_session(room, umo, room_created=True)
        self._sessions.add(umo, session)
        try:
            ok, message = await session.open()
        except Exception as exc:  # noqa: BLE001 - network errors should be reported
            logger.warning("[gjxq] 创建房间失败：%s", exc)
            ok, message = False, str(exc)
        if not ok:
            self._sessions.remove(umo, session)
            await self._close_session(umo, session)
            yield event.plain_result(f"❌ 创建房间失败：{message}")
            return

        auto_start = self._conf_bool("auto_start", True)
        lines = [
            f"✅ 房间已创建：{session.url}",
            f"房间号：{session.room_id}",
            "把链接发给对手，对方在网页中点击“入座”即可。",
        ]
        if auto_start:
            lines.append("双方入座后机器人会自动开始对局（auto_start 已开启）。")
        else:
            lines.append("双方入座后发送 /象棋 开始 开始对局。")
        lines.append("机器人已入座 1 号位（房主，执白）。")
        if self._sessions.count() > 1:
            lines.append("本会话已开多个房间：/象棋 列表 查看、/象棋 切换 <房间号> 切换。")
        lines.append("发送 /象棋 棋盘 查看棋盘，/象棋 状态 查看状态。")
        yield event.plain_result("\n".join(lines))

    @filter.command("象棋 加入")
    async def cmd_join(self, event: AstrMessageEvent, room_id: str = ""):
        """加入已存在的国际象棋房间；支持同一会话多开。"""
        umo = event.unified_msg_origin
        room = self._normalize_room_id(room_id)
        if not room:
            yield event.plain_result(
                "❌ 请提供要加入的房间号，例如：/象棋 加入 abc12\n"
                "也可以直接粘贴房间链接。"
            )
            return
        if not ROOM_ID_PATTERN.fullmatch(room):
            yield event.plain_result(
                f"❌ 房间号格式不正确：{room_id}\n"
                "房间号由 3-8 位小写字母或数字组成，例如 abc12。"
            )
            return
        if self._sessions.get(umo, room) is not None:
            yield event.plain_result(
                f"❌ 本会话已经在房间 {room} 里了。\n"
                "发送 /象棋 列表 查看已有房间，或换一个房间号。"
            )
            return

        max_sessions = self._max_sessions()
        if self._sessions.count() >= max_sessions:
            yield event.plain_result(
                f"同时进行的房间数已达上限（{max_sessions} 个）。\n"
                "请先用 /象棋 离开 关闭一个房间，或在配置中调大 max_sessions。"
            )
            return

        session = self._build_session(room, umo, room_created=False)
        self._sessions.add(umo, session)
        try:
            ok, message = await session.open()
        except Exception as exc:  # noqa: BLE001 - network errors should be reported
            logger.warning("[gjxq] 加入房间失败：%s", exc)
            ok, message = False, str(exc)
        if not ok:
            self._sessions.remove(umo, session)
            await self._close_session(umo, session)
            yield event.plain_result(f"❌ 加入房间失败：{message}")
            return

        lines = [
            f"✅ 已加入房间：{session.url}",
            f"当前身份：{self._seat_text(session)}",
        ]
        if session.is_playing:
            lines.append("对局进行中，发送 /象棋 棋盘 查看棋盘。")
        else:
            lines.append("等待房主在网页上开始对局；开始后可发送 /象棋 棋盘 查看。")
        if self._sessions.count() > 1:
            lines.append("本会话已开多个房间：/象棋 列表 查看、/象棋 切换 <房间号> 切换。")
        lines.append("发送 /象棋 状态 查看房间状态。")
        yield event.plain_result("\n".join(lines))

    @filter.command("象棋 列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出本会话已开的房间（多开时用于查看与切换）。"""
        umo = event.unified_msg_origin
        rooms = self._sessions.all_for(umo)
        if not rooms:
            yield event.plain_result(NO_SESSION_HINT)
            return
        active = self._sessions.get(umo)
        lines = [f"🏠 本会话共有 {len(rooms)} 个房间（上限 {self._max_sessions()} 个）："]
        for index, session in enumerate(rooms, start=1):
            lines.append(self._room_line(index, session, session is active))
            lines.append(f"    {session.url}")
        lines.append("操作：/象棋 切换 <房间号>、/象棋 离开 [房间号]、/象棋 棋盘")
        yield event.plain_result("\n".join(lines))

    @filter.command("象棋 切换")
    async def cmd_switch(self, event: AstrMessageEvent, room_id: str = ""):
        """切换当前操作的房间（多开时使用）。"""
        umo = event.unified_msg_origin
        rooms = self._sessions.all_for(umo)
        if not rooms:
            yield event.plain_result(NO_SESSION_HINT)
            return
        room = self._normalize_room_id(room_id)
        if not room:
            active = self._sessions.get(umo)
            lines = ["用法：/象棋 切换 <房间号>", "当前房间：" + (active.room_id if active else "无")]
            lines.append("可选房间：" + "、".join(session.room_id for session in rooms))
            yield event.plain_result("\n".join(lines))
            return
        if not self._sessions.set_active(umo, room):
            yield event.plain_result(
                f"❌ 本会话没有房间 {room}。\n"
                "可选房间：" + "、".join(session.room_id for session in rooms)
            )
            return
        session = self._sessions.get(umo)
        assert session is not None
        yield event.plain_result(
            f"✅ 已切换到房间 {session.room_id}\n{session.status_line()}\n"
            "后续 /象棋 棋盘、/象棋 走、/象棋 状态 等指令都作用于该房间。"
        )

    @filter.command("象棋 开始")
    async def cmd_start(self, event: AstrMessageEvent):
        """双方入座后由房主开始对局。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        try:
            ok, message = await session.start_game()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            logger.warning("[gjxq] 开始对局失败：%s", exc)
            ok, message = False, str(exc)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        yield event.plain_result(f"✅ {message}")

    @filter.command("象棋 棋盘")
    async def cmd_board(self, event: AstrMessageEvent):
        """查看当前棋盘。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        yield event.plain_result(self._board_reply(session))

    @filter.command("象棋 状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看房间状态、双方身份与最近走棋。"""
        umo = event.unified_msg_origin
        session = self._sessions.get(umo)
        if session is None:
            yield event.plain_result(self._no_room_reply(umo))
            return

        room = session.room
        players = []
        for index, player in enumerate(getattr(room, "player_list", []) or [], start=1):
            if not getattr(player, "occupied", False):
                players.append(f"{index} 号位：空")
                continue
            name = getattr(player, "name", "") or "玩家"
            mark = "（机器人）" if index == session.seat else ""
            players.append(f"{index} 号位：{name}{mark}")

        view = session.view
        ai_state = "开启" if session.config.ai_enabled else "关闭"
        lines = [
            "📊 国际象棋房间状态",
            session.status_line(),
            f"房间链接：{session.url}",
            f"机器人身份：{self._seat_text(session)}",
            f"自动 AI 走棋：{ai_state}（/象棋 AI 开|关）",
            f"AI 难度：{self._ai_difficulty_text(session)}（/象棋 难度 设置）",
        ]
        if len(self._sessions.all_for(umo)) > 1:
            lines.append("本会话房间数：" + str(len(self._sessions.all_for(umo))) + "（/象棋 列表）")
        if players:
            lines.append("座位：" + "；".join(players))
        move_list = render.render_move_list(view, limit=10)
        lines.append("最近走棋：" + (move_list.replace("\n", "；") if move_list else "暂无"))
        fen = render.render_fen_line(view)
        if fen:
            lines.append(f"FEN：{fen}")
        yield event.plain_result("\n".join(lines))

    @filter.command("象棋 走")
    async def cmd_move(self, event: AstrMessageEvent, move: str = ""):
        """按 UCI 格式走棋，例如 e2e4、g1f3、e7e8q。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        text = (move or "").strip()
        if not text:
            yield event.plain_result(
                "用法：/象棋 走 e2e4（升变：e7e8q）\n"
                "走法为“起点+终点”，不要带空格；发送 /象棋 棋盘 可查看棋盘。"
            )
            return
        try:
            ok, message = await session.play_uci(text)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            logger.warning("[gjxq] 走棋失败：%s", exc)
            ok, message = False, str(exc)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        yield event.plain_result(self._board_reply(session, prefix=f"✅ {message}"))

    @filter.command("象棋 AI")
    async def cmd_ai(self, event: AstrMessageEvent, action: str = ""):
        """开关当前房间的自动 AI 走棋。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(
                "当前会话还没有房间。\n"
                "发送 /象棋 创建 或 /象棋 加入 <房间号> 后再设置 AI。"
            )
            return

        choice = (action or "").strip().lower()
        enable_words = {"开", "开启", "打开", "启用", "on", "1", "true", "yes"}
        disable_words = {"关", "关闭", "禁用", "off", "0", "false", "no"}
        if choice in enable_words or choice in disable_words:
            enabled = choice in enable_words
            try:
                session.set_ai_enabled(enabled)
            except Exception as exc:  # noqa: BLE001 - optional best effort
                logger.warning("[gjxq] 切换 AI 状态失败：%s", exc)
                yield event.plain_result(f"❌ 切换 AI 状态失败：{exc}")
                return
            yield event.plain_result(
                "✅ 自动 AI 走棋已开启。" if enabled else "✅ 自动 AI 走棋已关闭。"
            )
            return
        if not choice:
            state = "开启" if session.config.ai_enabled else "关闭"
            yield event.plain_result(
                f"当前自动 AI 走棋：{state}\n"
                "用法：/象棋 AI 开 或 /象棋 AI 关"
            )
            return
        yield event.plain_result(
            f"❌ 无法识别的选项：{action}\n用法：/象棋 AI 开 或 /象棋 AI 关"
        )

    @filter.command("象棋 难度")
    async def cmd_difficulty(self, event: AstrMessageEvent, elo: str = ""):
        """查看或设置 AI 难度（近似 Elo 分，600-2400；「关」恢复按深度设置）。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return

        text = (elo or "").strip()
        if not text:
            yield event.plain_result(
                "当前 AI 难度：" + self._ai_difficulty_text(session) + "\n"
                "用法：/象棋 难度 <600-2400>    例如 /象棋 难度 1000\n"
                "      /象棋 难度 关            恢复按 ai_depth 的自定义搜索\n"
                "参考：600 入门 / 800 简单 / 1000 普通 / 1200 进阶 / 1600 较难 / 2000 困难 / 2400 最强"
            )
            return

        if text in {"关", "关闭", "off", "自定义", "默认", "auto"}:
            session.set_elo(0)
            yield event.plain_result(
                f"✅ 已关闭 Elo 难度，恢复自定义搜索：深度 {session.config.ai_depth}"
            )
            return

        try:
            requested = int(float(text))
        except (TypeError, ValueError):
            yield event.plain_result(
                f"❌ 无法识别的难度：{elo}\n"
                "用法：/象棋 难度 <600-2400> 或 /象棋 难度 关"
            )
            return
        rating = session.set_elo(requested)
        if rating <= 0:
            yield event.plain_result("❌ 难度需要大于 0；关闭 Elo 请发送 /象棋 难度 关")
            return
        profile = ai.elo_profile(rating)
        note = ""
        if requested != rating:
            note = f"（已裁剪为 {rating}）"
        yield event.plain_result(
            f"✅ AI 难度已设为 Elo {rating}{note}\n"
            f"搜索深度 {profile['depth']}，单步思考上限 {profile['time_limit']}s，"
            f"随机失误率约 {int(profile['blunder_prob'] * 100)}%。\n"
            "Elo 为近似难度，不是真实等级分；下一手棋开始生效。"
        )

    @filter.command("象棋 认输")
    async def cmd_resign(self, event: AstrMessageEvent):
        """认输当前对局。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        try:
            ok, message = await session.resign()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            logger.warning("[gjxq] 认输失败：%s", exc)
            ok, message = False, str(exc)
        if not ok:
            yield event.plain_result(f"❌ {message}")

    @filter.command("象棋 重开")
    async def cmd_restart(self, event: AstrMessageEvent):
        """对局结束后重开一局（机器人执白）。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        try:
            ok, message = await session.restart()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            logger.warning("[gjxq] 重开对局失败：%s", exc)
            ok, message = False, str(exc)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        yield event.plain_result(f"✅ {message}")

    @filter.command("象棋 离开")
    async def cmd_leave(self, event: AstrMessageEvent, room_id: str = ""):
        """离开当前房间；带房间号时只离开指定房间。"""
        umo = event.unified_msg_origin
        rooms = self._sessions.all_for(umo)
        if not rooms:
            yield event.plain_result(NO_SESSION_HINT)
            return
        wanted = self._normalize_room_id(room_id)
        if wanted:
            session = self._sessions.get(umo, wanted)
            if session is None:
                yield event.plain_result(
                    f"❌ 本会话没有房间 {wanted}。\n"
                    "可选房间：" + "、".join(s.room_id for s in rooms)
                )
                return
        else:
            session = self._sessions.get(umo)
            assert session is not None
        room = session.room_id
        seat_ok, seat_message = True, ""
        try:
            seat_ok, seat_message = await session.leave_seat()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            seat_ok, seat_message = False, str(exc)
        await self._close_session(umo, session)
        if not seat_ok:
            note = (
                f"⚠️ 已断开房间连接，但未能确认离座：{seat_message}\n"
                "（座位可能仍显示为占用，房主/对局结束后可在网页处理）"
            )
        else:
            note = f"✅ 已离开房间 {room}（{seat_message}）。"
        remaining = len(self._sessions.all_for(umo))
        if remaining:
            active = self._sessions.get(umo)
            yield event.plain_result(
                f"{note}\n还剩 {remaining} 个房间。当前房间：{active.room_id if active else '无'}\n"
                "发送 /象棋 列表 查看全部房间。"
            )
        else:
            yield event.plain_result(note)

    @filter.command("象棋 离开座位", alias={"象棋 观战", "象棋 离座"})
    async def cmd_leave_seat(self, event: AstrMessageEvent):
        """离开座位留在房间观战（对局中且机器人为房主时会先结束对局）。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        was_playing = session.is_playing
        was_owner = session.is_owner
        try:
            ok, message = await session.leave_seat()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            ok, message = False, str(exc)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        if session.room.position:
            yield event.plain_result(f"❌ 离座未生效：{message}")
            return
        extra = ""
        if was_playing and was_owner:
            extra = "（对局已结束，房间回到等待状态）"
        yield event.plain_result(
            f"✅ {message}{extra}，机器人现在在房间里观战。\n"
            "发送 /象棋 入座 可重新入座，/象棋 离开 可完全离开房间。"
        )

    @filter.command("象棋 入座", alias={"象棋 坐下"})
    async def cmd_take_seat(self, event: AstrMessageEvent, seat: str = ""):
        """重新入座（默认第一个空位）。"""
        session = self._sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(self._no_room_reply(event.unified_msg_origin))
            return
        wanted: Optional[int] = None
        text = (seat or "").strip()
        if text:
            try:
                wanted = int(text)
            except (TypeError, ValueError):
                yield event.plain_result(
                    f"❌ 座位号无效：{seat}\n用法：/象棋 入座 或 /象棋 入座 2"
                )
                return
        try:
            ok, message = await session.take_seat(wanted)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the handler
            ok, message = False, str(exc)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        yield event.plain_result(f"✅ {message}\n{session.status_line()}")

    @filter.command("象棋 房间")
    async def cmd_room(self, event: AstrMessageEvent):
        """查看当前房间号、链接与座位信息。"""
        umo = event.unified_msg_origin
        session = self._sessions.get(umo)
        if session is None:
            yield event.plain_result(self._no_room_reply(umo))
            return
        lines = [
            "🏠 国际象棋房间",
            f"房间号：{session.room_id}",
            f"房间链接：{session.url}",
            f"机器人身份：{self._seat_text(session)}",
            f"连接状态：{'已连接' if session.connected else '未连接'}",
            session.status_line(),
        ]
        count = len(self._sessions.all_for(umo))
        if count > 1:
            lines.append(f"本会话共有 {count} 个房间（/象棋 列表 查看、/象棋 切换 切换）")
        lines.append("操作：/象棋 开始、/象棋 棋盘、/象棋 状态、/象棋 离开")
        yield event.plain_result("\n".join(lines))
