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


def test_clock_advance_and_roundtrip():
    view = game.initial_view(enable_ct=True)
    assert view.enable_ct is True
    assert view.pt == 0 and view.pt_owner is None

    new_view, error = game.apply_move(view, 11, game.square_to_pos("e4"))
    assert error is None and new_view is not None
    # First move: no previous clock owner, the clock is handed to black.
    clocked = game.advance_clock(new_view, 12_345)
    assert clocked.pt == 12
    assert clocked.pt_owner == 1
    assert clocked.costs == [0, 0]

    legal = game.legal_moves(clocked)
    assert legal is not None
    piece_id = next(iter(legal))
    reply, error = game.apply_move(clocked, piece_id, legal[piece_id][0])
    assert error is None and reply is not None
    # Previous owner (black) gets the 8 s elapsed since pt; clock goes to white.
    reply = game.advance_clock(reply, 20_000)
    assert reply.pt == 20
    assert reply.pt_owner == 0
    assert reply.costs[1] == 8

    decoded = game.decode_game_data(game.encode_game_data(reply))
    assert decoded.enable_ct is True
    assert decoded.costs == reply.costs
    assert decoded.pt == reply.pt
    assert decoded.pt_owner == reply.pt_owner

    # Games without the clock are returned unchanged.
    plain = game.initial_view()
    assert game.advance_clock(plain, 99_999) is plain


def test_undo_last_and_reject_helpers():
    view = game.initial_view(enable_ct=True)
    first, error = game.apply_move(view, 11, game.square_to_pos("e4"))
    assert error is None and first is not None
    legal = game.legal_moves(first)
    assert legal is not None
    piece_id = next(iter(legal))
    second, error = game.apply_move(first, piece_id, legal[piece_id][0])
    assert error is None and second is not None

    undone = game.undo_last(second)
    assert undone is not None
    assert undone.state == game.STATE_WAIT_BLACK
    assert len(undone.records) == 1
    assert undone.back_piece_id == second.records[0].id
    assert undone.pieces[piece_id].pos == first.pieces[piece_id].pos

    request = game.View(
        white_id=second.white_id,
        state=game.STATE_WHITE_REQ_BACK,
        pieces=list(second.pieces),
        records=list(second.records),
    )
    rejected = game.reject_undo(request)
    assert rejected is not None and rejected.state == game.STATE_WAIT_BLACK
    assert game.reject_undo(second) is None  # not a request
    assert game.undo_last(game.initial_view()) is None
