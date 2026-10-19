"""Tests for ``chess_site/render.py``."""

from __future__ import annotations

import types

from chess_site import game, render

INITIAL_WHITE_BOARD = "\n".join(
    [
        "```",
        "轮到白方走棋",
        "  a b c d e f g h",
        "8 ♜ ♞ ♝ ♛ ♚ ♝ ♞ ♜",
        "7 ♟ ♟ ♟ ♟ ♟ ♟ ♟ ♟",
        "6 · · · · · · · ·",
        "5 · · · · · · · ·",
        "4 · · · · · · · ·",
        "3 · · · · · · · ·",
        "2 ♙ ♙ ♙ ♙ ♙ ♙ ♙ ♙",
        "1 ♖ ♘ ♗ ♕ ♔ ♗ ♘ ♖",
        "  a b c d e f g h",
        "```",
    ]
)


def _play(view: game.View, uci: str) -> game.View:
    """Apply a UCI move (``e2e4``) and return the resulting view."""
    piece = game.piece_at(view, game.square_to_pos(uci[:2]))
    assert piece is not None, f"no piece on {uci[:2]}"
    promotion = None
    if len(uci) == 5:
        promotion = {
            "q": game.PIECE_QUEEN,
            "r": game.PIECE_ROCK,
            "b": game.PIECE_BISHOP,
            "n": game.PIECE_KNIGHT,
        }[uci[4]]
    new_view, error = game.apply_move(
        view, piece.id, game.square_to_pos(uci[2:4]), promotion
    )
    assert new_view is not None, f"move {uci} rejected: {error}"
    return new_view


def test_initial_board_white_at_bottom():
    view = game.initial_view()
    assert render.render_board_text(view) == INITIAL_WHITE_BOARD
    # ``my_white=True`` uses the same orientation and labels the perspective.
    explicit = render.render_board_text(view, my_white=True)
    assert "白方视角" in explicit
    assert explicit.splitlines()[2:] == INITIAL_WHITE_BOARD.splitlines()[2:]


def test_board_is_a_fenced_code_block():
    text = render.render_board_text(game.initial_view())
    lines = text.splitlines()
    assert lines[0] == "```"
    assert lines[-1] == "```"


def test_initial_board_flipped_for_black():
    text = render.render_board_text(game.initial_view(), my_white=False)
    lines = text.splitlines()
    assert "黑方视角" in lines[1]
    # Black's own side (rank 1, plus the white pieces) is at the top now.
    assert lines[2] == "  h g f e d c b a"
    assert lines[3] == "1 ♖ ♘ ♗ ♔ ♕ ♗ ♘ ♖"
    assert lines[4] == "2 ♙ ♙ ♙ ♙ ♙ ♙ ♙ ♙"
    assert lines[10] == "8 ♜ ♞ ♝ ♚ ♛ ♝ ♞ ♜"
    assert lines[-2] == "  h g f e d c b a"


def test_black_orientation_places_black_king_on_file_e():
    lines = render.render_board_text(game.initial_view(), my_white=False).splitlines()
    header = lines[2].split()
    rank_8 = lines[10].split()
    # Header files are h..a and the black king (pos 4 -> e8) must line up with "e".
    assert header == list("hgfedcba")
    assert rank_8[1 + header.index("e")] == "♚"
    assert rank_8[1 + header.index("d")] == "♛"


def test_last_move_legend_accepts_record_pair_and_move_info():
    view = _play(game.initial_view(), "e2e4")
    record = view.records[0]

    text = render.render_board_text(view, my_white=True, last_move=record)
    assert "最后一步：e2 → e4" in text

    assert "最后一步：e2 → e4" in render.render_board_text(view, last_move=(52, 36))
    # Session ``MoveInfo`` wraps the record in a ``.record`` attribute.
    info = types.SimpleNamespace(record=record)
    assert "最后一步：e2 → e4" in render.render_board_text(view, last_move=info)


def test_no_legend_without_last_move():
    assert "最后一步" not in render.render_board_text(game.initial_view())


def test_move_list_newest_full_move_first():
    view = game.initial_view()
    for uci in ("e2e4", "e7e5", "g1f3", "b8c6"):
        view = _play(view, uci)
    assert render.render_move_list(view) == "\n".join(
        ["2. g1f3 b8c6", "1. e2e4 e7e5"]
    )


def test_move_list_limit_keeps_most_recent_plies():
    view = game.initial_view()
    for uci in ("e2e4", "e7e5", "g1f3", "b8c6"):
        view = _play(view, uci)
    assert render.render_move_list(view, limit=2) == "2. g1f3 b8c6"
    assert render.render_move_list(view, limit=1) == "2. b8c6"
    # Invalid/zero limits fall back to the default instead of raising.
    assert render.render_move_list(view, limit=0) == render.render_move_list(view, limit=10)


def test_move_list_empty_for_initial_position():
    assert render.render_move_list(game.initial_view()) == ""
    assert render.render_move_list(None) == ""


def test_fen_line_initial_position():
    assert (
        render.render_fen_line(game.initial_view())
        == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )


def test_fen_line_matches_game_view_to_fen():
    view = game.initial_view()
    assert render.render_fen_line(view) == game.view_to_fen(view)
    for uci in ("e2e4", "e7e5", "g1f3"):
        view = _play(view, uci)
        assert render.render_fen_line(view) == game.view_to_fen(view)


def test_fen_line_empty_for_none():
    assert render.render_fen_line(None) == ""


def test_none_and_empty_views_are_graceful():
    assert render.render_board_text(None)
    assert isinstance(render.render_board_text(None), str)
    empty = game.View(white_id=0, state=game.STATE_WAIT_WHITE, pieces=[], records=[])
    assert "暂无" in render.render_board_text(empty)
    assert render.render_move_list(empty) == ""
    assert render.render_fen_line(empty) == ""
    assert render.render_last_move_line(empty) == ""
    assert render.render_status_text(None) == "无对局"


def test_captured_pieces_are_not_rendered():
    view = game.initial_view()
    # Simulate a captured piece: position 127 must be skipped, not drawn.
    view.pieces[15].pos = game.POS_CAPTURED
    text = render.render_board_text(view)
    assert text.count("♙") == 7


def test_status_text_win_states():
    white_win = game.View(
        white_id=0, state=game.STATE_WHITE_WIN, pieces=game.initial_view().pieces, records=[]
    )
    black_win = game.View(
        white_id=0, state=game.STATE_BLACK_WIN, pieces=game.initial_view().pieces, records=[]
    )
    assert render.render_status_text(white_win) == "白方获胜"
    assert render.render_status_text(black_win) == "黑方获胜"
    assert "白方获胜" in render.render_board_text(white_win)


def test_render_last_move_line():
    view = _play(game.initial_view(), "e2e4")
    assert render.render_last_move_line(view) == "最后一步：e2e4"
