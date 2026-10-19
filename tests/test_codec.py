"""Regression tests for the site-compatible GJXQ game codec (``chess_site/game.py``).

The fixture ``fixtures/golden_sample.json`` contains snapshots captured from the
site's own JavaScript encoder (very first moves, castling, en passant, promotion,
captures of unmoved rooks and long random games).  Every case must decode to the
recorded position and re-encode to the exact same bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import chess
import pytest

from chess_site import game

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "golden_sample.json"
GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

INITIAL_HEX = "080010001a18fa2fffe10e2184210208410fd13fff08710c21081042087f220028003000"
INITIAL_LEGAL = {
    4: [45, 47],
    5: [40, 42],
    8: [39, 47],
    9: [38, 46],
    10: [37, 45],
    11: [36, 44],
    12: [35, 43],
    13: [34, 42],
    14: [33, 41],
    15: [32, 40],
}


@pytest.mark.parametrize("case", GOLDEN, ids=lambda case: case["tag"] + ":" + case["hex"][:12])
def test_golden_roundtrip(case):
    data = bytes.fromhex(case["hex"])
    view = game.decode_game_data(data)

    pieces = [[index, piece.pos, piece.type, 1 if piece.moved else 0] for index, piece in enumerate(view.pieces)]
    records = [
        [record.id, record.before, record.after, record.eat, record.sp, 1 if record.eatUnmoved else 0]
        for record in view.records
    ]
    assert pieces == case["pieces"]
    assert records == case["records"]
    assert view.state == case["state"]
    assert view.white_id == case["whiteId"]
    assert game.encode_game_data(view).hex() == case["hex"]


def test_initial_view_encoding():
    view = game.initial_view()
    assert game.encode_game_data(view).hex() == INITIAL_HEX
    assert len(view.pieces) == 32
    assert view.white_id == 0
    assert game.view_to_fen(view) == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def test_initial_legal_moves_match_site():
    view = game.initial_view()
    legal = game.legal_moves(view)
    assert legal is not None
    assert {piece_id: sorted(destinations) for piece_id, destinations in legal.items()} == INITIAL_LEGAL


def test_apply_move_roundtrip():
    view = game.initial_view()
    e2_pawn = view.pieces[11]
    assert game.pos_to_square(e2_pawn.pos) == "e2"
    new_view, error = game.apply_move(view, 11, game.square_to_pos("e4"))
    assert error is None
    assert new_view is not None
    assert game.view_to_fen(new_view) == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    decoded = game.decode_game_data(game.encode_game_data(new_view))
    assert [piece.pos for piece in decoded.pieces] == [piece.pos for piece in new_view.pieces]
    assert [record.before for record in decoded.records] == [record.before for record in new_view.records]


def test_special_moves_are_covered_by_fixture():
    special = {record[4] for case in GOLDEN for record in case["records"]}
    assert {game.SP_IN_PASSING, game.SP_PROMOTION, game.SP_CASTLING, game.SP_INIT_MOVE} <= special
    assert any(record[5] for case in GOLDEN for record in case["records"])  # captured unmoved rook


def test_state_override_and_tolerant_decode():
    view = game.initial_view()
    new_view, error = game.apply_move(view, 11, game.square_to_pos("e4"))
    assert error is None and new_view is not None
    for state in (game.STATE_WHITE_WIN, game.STATE_BLACK_WIN):
        data = game.encode_game_data(new_view, state_override=state)
        decoded = game.decode_game_data(data)
        assert decoded.state == state
        assert [piece.pos for piece in decoded.pieces] == [piece.pos for piece in new_view.pieces]


def test_fen_is_valid_for_python_chess():
    for case in GOLDEN[:10]:
        view = game.decode_game_data(bytes.fromhex(case["hex"]))
        fen = game.view_to_fen(view)
        if fen:
            chess.Board(fen)  # raises when the FEN is invalid
