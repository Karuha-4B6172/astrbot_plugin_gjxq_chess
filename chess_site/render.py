"""Dependency-free text rendering for GJXQ (国际象棋) game snapshots.

This module only reads plain attributes from the objects produced by
:mod:`chess_site.game` (``View`` / ``Piece`` / ``Record``); it deliberately does
not import that module or anything outside the Python standard library, so it
can be imported and unit-tested in isolation.

Output targets text-only chat platforms such as QQ: the board is wrapped in a
Markdown code fence so clients that do render Markdown keep a monospaced layout.

Coordinate conventions mirror the site (see SPEC §4.1):

* ``pos = y * 8 + x`` with ``x = pos & 7`` (file a..h) and ``y = pos >> 3``
  (rank 8 at ``y = 0``, rank 1 at ``y = 7``).
* White pieces live on ``y = 6/7`` and black on ``y = 0/1``.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "render_board_text",
    "render_move_list",
    "render_fen_line",
    "render_last_move_line",
    "render_status_text",
]

# ---------------------------------------------------------------------------
# Constants mirrored from chess_site.game (protocol-level, stable).
# ---------------------------------------------------------------------------

PIECE_KING = 0
PIECE_QUEEN = 1
PIECE_ROCK = 2
PIECE_BISHOP = 3
PIECE_KNIGHT = 4
PIECE_PAWN = 5

SP_NONE = 0
SP_IN_PASSING = 1
SP_PROMOTION = 2
SP_CASTLING = 3
SP_INIT_MOVE = 4

STATE_WAIT_WHITE = 0
STATE_WAIT_BLACK = 1
STATE_WHITE_WIN = 2
STATE_BLACK_WIN = 3
STATE_WHITE_REQ_BACK = 4
STATE_BLACK_REQ_BACK = 5

POS_CAPTURED = 127

_FILES = "abcdefgh"

_WHITE_GLYPHS = {
    PIECE_KING: "♔",
    PIECE_QUEEN: "♕",
    PIECE_ROCK: "♖",
    PIECE_BISHOP: "♗",
    PIECE_KNIGHT: "♘",
    PIECE_PAWN: "♙",
}
_BLACK_GLYPHS = {
    PIECE_KING: "♚",
    PIECE_QUEEN: "♛",
    PIECE_ROCK: "♜",
    PIECE_BISHOP: "♝",
    PIECE_KNIGHT: "♞",
    PIECE_PAWN: "♟",
}
_EMPTY_CELL = "·"

_PROMOTION_LETTERS = {
    PIECE_QUEEN: "q",
    PIECE_ROCK: "r",
    PIECE_BISHOP: "b",
    PIECE_KNIGHT: "n",
}

_FEN_LETTERS = {
    PIECE_KING: "k",
    PIECE_QUEEN: "q",
    PIECE_ROCK: "r",
    PIECE_BISHOP: "b",
    PIECE_KNIGHT: "n",
    PIECE_PAWN: "p",
}

_NO_VIEW_TEXT = "（暂无棋局数据，等待对局开始）"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _pos_to_square(pos: Any) -> str:
    """Convert a board position to algebraic notation (``52 -> "e2"``)."""
    if not isinstance(pos, int) or not 0 <= pos < 64:
        return "??"
    return "%s%d" % (_FILES[pos & 7], 8 - (pos >> 3))


def _is_white_turn(view: Any) -> bool:
    """Site ``J7`` semantics: white acts in WaitWhite / BlackReqBack / BlackWin."""
    state = getattr(view, "state", STATE_WAIT_WHITE)
    return state in (STATE_WAIT_WHITE, STATE_BLACK_REQ_BACK, STATE_BLACK_WIN)


def _is_finished(view: Any) -> bool:
    state = getattr(view, "state", STATE_WAIT_WHITE)
    return state in (STATE_WHITE_WIN, STATE_BLACK_WIN)


def _is_playing(view: Any) -> bool:
    state = getattr(view, "state", STATE_WAIT_WHITE)
    return state in (STATE_WAIT_WHITE, STATE_WAIT_BLACK)


def _build_board(view: Any) -> list[list[Any]]:
    """Return an 8x8 grid of pieces (or ``None``) indexed by ``[y][x]``."""
    grid: list[list[Any]] = [[None] * 8 for _ in range(8)]
    pieces = getattr(view, "pieces", None) or []
    for piece in pieces:
        pos = getattr(piece, "pos", None)
        if not isinstance(pos, int) or not 0 <= pos < 64:
            continue
        grid[pos >> 3][pos & 7] = piece
    return grid


def _piece_glyph(piece: Any) -> str:
    """Unicode glyph for one piece (white pieces have ``id < 16``)."""
    glyphs = _WHITE_GLYPHS if getattr(piece, "id", 0) < 16 else _BLACK_GLYPHS
    return glyphs.get(getattr(piece, "type", PIECE_PAWN), "?")


def render_status_text(view: Any) -> str:
    """One-line Chinese description of the game state."""
    if view is None:
        return "无对局"
    state = getattr(view, "state", STATE_WAIT_WHITE)
    if state == STATE_WHITE_WIN:
        return "白方获胜"
    if state == STATE_BLACK_WIN:
        return "黑方获胜"
    if state in (STATE_WHITE_REQ_BACK, STATE_BLACK_REQ_BACK):
        return "等待处理悔棋请求"
    if not _is_playing(view):
        return "对局结束"
    return "轮到白方走棋" if _is_white_turn(view) else "轮到黑方走棋"


def _move_squares(last_move: Any) -> Optional[tuple[int, int]]:
    """Best-effort extraction of ``(before, after)`` board positions.

    Accepts ``None``, a ``(before, after)`` pair, a ``Record`` (has ``before`` /
    ``after``) or a session ``MoveInfo`` (has a ``.record`` attribute).
    """
    if last_move is None:
        return None
    if isinstance(last_move, (tuple, list)) and len(last_move) >= 2:
        before, after = last_move[0], last_move[1]
        if isinstance(before, int) and isinstance(after, int):
            return before, after
        return None
    record = getattr(last_move, "record", last_move)
    before = getattr(record, "before", None)
    after = getattr(record, "after", None)
    if isinstance(before, int) and isinstance(after, int):
        return before, after
    return None


def _record_uci(view: Any, record: Any) -> str:
    """UCI-like text for one move record (``e2e4``, ``e7e8q``)."""
    before = _pos_to_square(getattr(record, "before", None))
    after = _pos_to_square(getattr(record, "after", None))
    text = f"{before}{after}"
    if getattr(record, "sp", SP_NONE) == SP_PROMOTION:
        piece_id = getattr(record, "id", None)
        pieces = getattr(view, "pieces", None) or []
        piece = pieces[piece_id] if isinstance(piece_id, int) and 0 <= piece_id < len(pieces) else None
        text += _PROMOTION_LETTERS.get(getattr(piece, "type", PIECE_QUEEN), "q")
    return text


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------


def render_board_text(
    view: Any,
    *,
    my_white: Optional[bool] = None,
    last_move: Any = None,
) -> str:
    """Render a chess position as a fenced, monospaced text board.

    Args:
        view: A ``chess_site.game.View``-like object (or ``None``).
        my_white: ``True`` puts white at the bottom (default/spectator),
            ``False`` flips the board so black plays from the bottom,
            ``None`` keeps the standard white-at-bottom orientation.
        last_move: Last move to highlight in the legend. Accepts a
            ``(before, after)`` position pair, a ``Record`` or a session
            ``MoveInfo``.

    Returns:
        A Markdown-fenced text board. An empty/``None`` view yields a short
        Chinese hint instead of raising.
    """
    if view is None:
        return _NO_VIEW_TEXT
    grid = _build_board(view)
    if not any(cell is not None for row in grid for cell in row):
        return _NO_VIEW_TEXT

    # ``my_white is False`` means the black player is looking at the board:
    # rotate 180 degrees (ranks 1..8 top->bottom, files h..a left->right).
    white_at_bottom = my_white is not False
    rank_rows = range(8) if white_at_bottom else range(7, -1, -1)
    file_cols = range(8) if white_at_bottom else range(7, -1, -1)

    status = render_status_text(view)
    if my_white is True:
        status += "｜白方视角"
    elif my_white is False:
        status += "｜黑方视角"

    header_cells = " ".join(_FILES[col] for col in file_cols)
    file_header = "  " + header_cells

    lines = ["```", status, file_header]
    for y in rank_rows:
        rank_label = str(8 - y)
        cells = []
        for x in file_cols:
            piece = grid[y][x]
            cells.append(_piece_glyph(piece) if piece is not None else _EMPTY_CELL)
        lines.append(rank_label + " " + " ".join(cells))
    lines.append(file_header)

    squares = _move_squares(last_move)
    if squares is not None:
        before, after = squares
        lines.append(f"最后一步：{_pos_to_square(before)} → {_pos_to_square(after)}")
    lines.append("```")
    return "\n".join(lines)


def render_last_move_line(view: Any) -> str:
    """One-line Chinese description of the newest move, or ``""``."""
    records = getattr(view, "records", None) or []
    if not records:
        return ""
    return f"最后一步：{_record_uci(view, records[0])}"


def render_move_list(view: Any, limit: int = 10) -> str:
    """Render recent moves, newest full move first.

    Args:
        view: A ``chess_site.game.View``-like object (or ``None``).
        limit: Maximum number of half-moves (plies) to include. Non-positive
            values fall back to ``10``.

    Returns:
        Lines such as ``"3. g1f3 b8c6"`` ordered from the newest full move to
        the oldest one shown. ``""`` when there are no moves.
    """
    records = getattr(view, "records", None) or []
    if not records:
        return ""
    try:
        plies = int(limit)
    except (TypeError, ValueError):
        plies = 10
    if plies <= 0:
        plies = 10

    # ``records`` is newest-first; pair chronological plies into full moves.
    chronological = list(reversed(records))[-plies:]
    first_index = len(records) - len(chronological)
    buckets: dict[int, list[str]] = {}
    for offset, record in enumerate(chronological):
        index = first_index + offset
        full_move = index // 2 + 1
        bucket = buckets.setdefault(full_move, ["", ""])
        bucket[index % 2] = _record_uci(view, record)

    lines = []
    for full_move in sorted(buckets, reverse=True):
        white_text, black_text = buckets[full_move]
        line = f"{full_move}. {white_text}".rstrip()
        if black_text:
            line += f" {black_text}"
        lines.append(line)
    return "\n".join(lines)


def render_fen_line(view: Any) -> str:
    """Compact single-line FEN for display purposes (``""`` when unavailable).

    Castling rights and the en-passant target are reconstructed from the
    snapshot's ``moved`` flags and newest record, mirroring ``game.view_to_fen``
    closely enough for chat display. The halfmove clock is always ``0`` (the
    site does not track it) and the fullmove number follows the record count.
    """
    if view is None:
        return ""
    grid = _build_board(view)
    if not any(cell is not None for row in grid for cell in row):
        return ""

    rows = []
    for y in range(8):
        empty = 0
        row = []
        for x in range(8):
            piece = grid[y][x]
            if piece is None:
                empty += 1
                continue
            if empty:
                row.append(str(empty))
                empty = 0
            letter = _FEN_LETTERS.get(getattr(piece, "type", PIECE_PAWN), "p")
            row.append(letter.upper() if getattr(piece, "id", 0) < 16 else letter)
        if empty:
            row.append(str(empty))
        rows.append("".join(row))

    color = "w" if _is_white_turn(view) else "b"

    pieces = getattr(view, "pieces", None) or []

    def _piece(index: int) -> Any:
        return pieces[index] if 0 <= index < len(pieces) else None

    castling = ""
    white_king, black_king = _piece(0), _piece(16)
    if white_king is not None and not getattr(white_king, "moved", False) and getattr(white_king, "pos", -1) == 60:
        rook_k, rook_q = _piece(2), _piece(3)
        if rook_k is not None and not getattr(rook_k, "moved", False) and getattr(rook_k, "pos", -1) == 63:
            castling += "K"
        if rook_q is not None and not getattr(rook_q, "moved", False) and getattr(rook_q, "pos", -1) == 56:
            castling += "Q"
    if black_king is not None and not getattr(black_king, "moved", False) and getattr(black_king, "pos", -1) == 4:
        rook_k, rook_q = _piece(19), _piece(18)
        if rook_k is not None and not getattr(rook_k, "moved", False) and getattr(rook_k, "pos", -1) == 7:
            castling += "k"
        if rook_q is not None and not getattr(rook_q, "moved", False) and getattr(rook_q, "pos", -1) == 0:
            castling += "q"
    if not castling:
        castling = "-"

    en_passant = "-"
    records = getattr(view, "records", None) or []
    if records:
        last = records[0]
        last_id = getattr(last, "id", -1)
        last_type = getattr(pieces[last_id], "type", None) if isinstance(last_id, int) and 0 <= last_id < len(pieces) else None
        before = getattr(last, "before", None)
        after = getattr(last, "after", None)
        if (
            last_type == PIECE_PAWN
            and getattr(last, "eat", None) is None
            and getattr(last, "sp", SP_NONE) == SP_NONE
            and isinstance(before, int)
            and isinstance(after, int)
            and abs(after - before) == 16
        ):
            en_passant = _pos_to_square((after + before) >> 1)

    fullmove = max(1, len(records) // 2 + 1)
    return f"{'/'.join(rows)} {color} {castling} {en_passant} 0 {fullmove}"
