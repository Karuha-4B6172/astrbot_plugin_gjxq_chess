"""Tests for the python-chess based AI (``chess_site/ai.py``)."""

from __future__ import annotations

import time

import chess
import pytest

from chess_site import ai

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
MIDGAME_FEN = "r2q1rk1/ppp2ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPP2PPP/R2Q1RK1 w - - 0 8"
MATE_IN_ONE_FEN = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
STALEMATE_FEN = "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"


def _assert_legal(fen: str, uci: str | None) -> None:
    assert uci is not None
    assert chess.Move.from_uci(uci) in chess.Board(fen).legal_moves


def test_choose_move_from_start_is_legal_and_deterministic():
    first = ai.choose_move(START_FEN, depth=2, time_limit=2.0)
    second = ai.choose_move(START_FEN, depth=2, time_limit=2.0)
    _assert_legal(START_FEN, first)
    assert first == second


def test_choose_move_finds_mate_in_one():
    assert ai.choose_move(MATE_IN_ONE_FEN, depth=2, time_limit=2.0) == "a1a8"


def test_choose_move_handles_invalid_and_terminal_positions():
    assert ai.choose_move("not a fen") is None
    assert ai.choose_move("") is None
    assert ai.choose_move(STALEMATE_FEN) is None


def test_choose_move_promotion():
    fen = "8/P6k/8/8/8/8/8/7K w - - 0 1"
    move = ai.choose_move(fen, depth=2, time_limit=2.0)
    _assert_legal(fen, move)
    assert move is not None and move[4] in "qrbn"


def test_elo_profile_monotonic():
    assert ai.clamp_elo(None) == 0
    assert ai.clamp_elo(100) == ai.ELO_MIN
    assert ai.clamp_elo(9999) == ai.ELO_MAX
    off = ai.elo_profile(0)
    assert off["elo"] == 0 and off["depth"] is None

    low, mid, high = ai.elo_profile(600), ai.elo_profile(1200), ai.elo_profile(2400)
    assert low["elo"] == 600 and high["elo"] == 2400
    assert low["depth"] <= mid["depth"] <= high["depth"]
    assert low["window_cp"] >= mid["window_cp"] >= high["window_cp"] >= 0
    assert low["blunder_prob"] >= mid["blunder_prob"] >= high["blunder_prob"] >= 0
    assert high["window_cp"] == 0 and high["blunder_prob"] == 0


@pytest.mark.parametrize("elo", [600, 800, 1200, 1600, 2400])
def test_elo_moves_are_legal_and_convert_mate(elo):
    move = ai.choose_move(START_FEN, elo=elo)
    _assert_legal(START_FEN, move)
    assert ai.choose_move(MATE_IN_ONE_FEN, elo=elo) == "a1a8"


def test_elo_mode_respects_time_cap():
    started = time.monotonic()
    move = ai.choose_move(MIDGAME_FEN, elo=2400, time_limit=0.3)
    elapsed = time.monotonic() - started
    _assert_legal(MIDGAME_FEN, move)
    assert elapsed < 3.0


def test_elo_off_uses_custom_depth():
    assert ai.choose_move(MATE_IN_ONE_FEN, depth=1, time_limit=1.0, elo=0) == "a1a8"
    assert ai.choose_move(MATE_IN_ONE_FEN, depth=1, time_limit=1.0, elo=None) == "a1a8"
