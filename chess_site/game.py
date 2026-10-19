"""GJXQ (国际象棋) game-data codec and move engine.

Bit-exact stdlib-only Python port of the chess site's own implementation
(webpack modules 3329 / 8234 / 3267 / 1990 / 2732 of the gjxq bundle).

This module is import-safe: no network, no file I/O, no third-party packages.
It exposes the public API described in ``_dev/SPEC.md`` section 5.

Conventions (identical to the site):

* 64 squares: ``pos = y * 8 + x``; ``x = pos & 7``, ``y = pos >> 3``.
* White sits on ``y = 7/6`` (king on e1 = 60), black on ``y = 0/1``
  (king on e8 = 4).  White pawns move towards ``y - 1``, black towards ``y + 1``.
* 32 fixed piece ids 0..31 (0..15 white, 16..31 black); a piece list is
  always indexed by piece id.  ``pos == 127`` means captured.  ``pos == 126``
  is only ever used inside the encoded byte stream ("king/rook still on its
  initial square and never moved").
* Piece ``type``: 0 King, 1 Queen, 2 Rock(rook), 3 Bishop, 4 Knight, 5 Pawn.
* Records are stored newest-first.
* Bit packing is MSB-first; the piece/record streams pad the final low bits
  with 1s, the optional ``ct`` stream pads with 0s (as the site does).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    # states
    "STATE_WAIT_WHITE",
    "STATE_WAIT_BLACK",
    "STATE_WHITE_WIN",
    "STATE_BLACK_WIN",
    "STATE_WHITE_REQ_BACK",
    "STATE_BLACK_REQ_BACK",
    # piece types
    "PIECE_KING",
    "PIECE_QUEEN",
    "PIECE_ROCK",
    "PIECE_BISHOP",
    "PIECE_KNIGHT",
    "PIECE_PAWN",
    # special moves
    "SP_NONE",
    "SP_IN_PASSING",
    "SP_PROMOTION",
    "SP_CASTLING",
    "SP_INIT_MOVE",
    # data classes
    "Piece",
    "Record",
    "View",
    # public API
    "initial_view",
    "encode_game_data",
    "decode_game_data",
    "encode_pieces",
    "decode_pieces",
    "encode_records",
    "decode_records",
    "apply_move",
    "is_white_turn",
    "is_playing",
    "is_finished",
    "piece_at",
    "pos_to_square",
    "square_to_pos",
    "view_to_fen",
    "legal_moves",
    "is_in_check",
]

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

STATE_WAIT_WHITE = 0
STATE_WAIT_BLACK = 1
STATE_WHITE_WIN = 2
STATE_BLACK_WIN = 3
STATE_WHITE_REQ_BACK = 4
STATE_BLACK_REQ_BACK = 5

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

POS_CAPTURED = 127
POS_UNMOVED_SENTINEL = 126

_CT_COST_MAX = 4095

# Fixed piece ids -> type and initial square (site module 8234, list ``s``/``l``).
_INITIAL_POSITIONS: Tuple[int, ...] = (
    60, 59, 63, 56, 62, 57, 61, 58, 55, 54, 53, 52, 51, 50, 49, 48,
    4, 3, 0, 7, 1, 6, 2, 5, 8, 9, 10, 11, 12, 13, 14, 15,
)
_PIECE_TYPES: Tuple[int, ...] = (
    PIECE_KING, PIECE_QUEEN, PIECE_ROCK, PIECE_ROCK, PIECE_KNIGHT, PIECE_KNIGHT,
    PIECE_BISHOP, PIECE_BISHOP, PIECE_PAWN, PIECE_PAWN, PIECE_PAWN, PIECE_PAWN,
    PIECE_PAWN, PIECE_PAWN, PIECE_PAWN, PIECE_PAWN,
    PIECE_KING, PIECE_QUEEN, PIECE_ROCK, PIECE_ROCK, PIECE_KNIGHT, PIECE_KNIGHT,
    PIECE_BISHOP, PIECE_BISHOP, PIECE_PAWN, PIECE_PAWN, PIECE_PAWN, PIECE_PAWN,
    PIECE_PAWN, PIECE_PAWN, PIECE_PAWN, PIECE_PAWN,
)

# Candidate square lists used by the piece bit codec, copied verbatim from
# _dev/board_constants.json (generated from the site bundle).  Index = piece id.
_POSITION_CANDIDATES: Tuple[Tuple[int, ...], ...] = (
    (63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 126, 127),
    (127, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
    (127, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 126),
    (127, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 126),
    (127, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
    (127, 63, 62, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
    (127, 63, 61, 59, 57, 54, 52, 50, 48, 47, 45, 43, 41, 38, 36, 34, 32, 31, 29, 27, 25, 22, 20, 18, 16, 15, 13, 11, 9, 6, 4, 2, 0),
    (127, 62, 60, 58, 56, 55, 53, 51, 49, 46, 44, 42, 40, 39, 37, 35, 33, 30, 28, 26, 24, 23, 21, 19, 17, 14, 12, 10, 8, 7, 5, 3, 1),
    (127, 55, 47, 46, 39, 38, 37, 31, 30, 29, 23, 22, 21, 15, 14, 13, 28, 20, 12, 19, 11, 10),
    (127, 54, 46, 47, 45, 38, 39, 37, 36, 30, 31, 29, 28, 22, 23, 21, 20, 14, 15, 13, 12, 27, 19, 11, 18, 10, 9),
    (127, 53, 45, 46, 44, 37, 38, 36, 39, 35, 29, 30, 28, 31, 27, 21, 22, 20, 23, 19, 13, 14, 12, 15, 11, 26, 18, 10, 17, 9, 8),
    (127, 52, 44, 45, 43, 36, 37, 35, 38, 34, 28, 29, 27, 30, 26, 20, 21, 19, 22, 18, 12, 13, 11, 14, 10, 31, 25, 23, 17, 15, 9, 16, 8),
    (127, 51, 43, 42, 44, 35, 34, 36, 33, 37, 27, 26, 28, 25, 29, 19, 18, 20, 17, 21, 11, 10, 12, 9, 13, 24, 30, 16, 22, 8, 14, 23, 15),
    (127, 50, 42, 41, 43, 34, 33, 35, 32, 36, 26, 25, 27, 24, 28, 18, 17, 19, 16, 20, 10, 9, 11, 8, 12, 29, 21, 13, 22, 14, 15),
    (127, 49, 41, 40, 42, 33, 32, 34, 35, 25, 24, 26, 27, 17, 16, 18, 19, 9, 8, 10, 11, 28, 20, 12, 21, 13, 14),
    (127, 48, 40, 41, 32, 33, 34, 24, 25, 26, 16, 17, 18, 8, 9, 10, 27, 19, 11, 20, 12, 13),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 126, 127),
    (127, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63),
    (127, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 126),
    (127, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 126),
    (127, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63),
    (127, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63),
    (127, 0, 2, 4, 6, 9, 11, 13, 15, 16, 18, 20, 22, 25, 27, 29, 31, 32, 34, 36, 38, 41, 43, 45, 47, 48, 50, 52, 54, 57, 59, 61, 63),
    (127, 1, 3, 5, 7, 8, 10, 12, 14, 17, 19, 21, 23, 24, 26, 28, 30, 33, 35, 37, 39, 40, 42, 44, 46, 49, 51, 53, 55, 56, 58, 60, 62),
    (127, 8, 16, 17, 24, 25, 26, 32, 33, 34, 40, 41, 42, 48, 49, 50, 35, 43, 51, 44, 52, 53),
    (127, 9, 17, 16, 18, 25, 24, 26, 27, 33, 32, 34, 35, 41, 40, 42, 43, 49, 48, 50, 51, 36, 44, 52, 45, 53, 54),
    (127, 10, 18, 17, 19, 26, 25, 27, 24, 28, 34, 33, 35, 32, 36, 42, 41, 43, 40, 44, 50, 49, 51, 48, 52, 37, 45, 53, 46, 54, 55),
    (127, 11, 19, 18, 20, 27, 26, 28, 25, 29, 35, 34, 36, 33, 37, 43, 42, 44, 41, 45, 51, 50, 52, 49, 53, 32, 38, 40, 46, 48, 54, 47, 55),
    (127, 12, 20, 21, 19, 28, 29, 27, 30, 26, 36, 37, 35, 38, 34, 44, 45, 43, 46, 42, 52, 53, 51, 54, 50, 39, 33, 47, 41, 55, 49, 40, 48),
    (127, 13, 21, 22, 20, 29, 30, 28, 31, 27, 37, 38, 36, 39, 35, 45, 46, 44, 47, 43, 53, 54, 52, 55, 51, 34, 42, 50, 41, 49, 48),
    (127, 14, 22, 23, 21, 30, 31, 29, 28, 38, 39, 37, 36, 46, 47, 45, 44, 54, 55, 53, 52, 35, 43, 51, 42, 50, 49),
    (127, 15, 23, 22, 31, 30, 29, 39, 38, 37, 47, 46, 45, 55, 54, 53, 36, 44, 52, 43, 51, 50),
)

# Candidate lists used for *promoted* pawns (site: ``d > 15 ? v : h``).
# ``h`` = white/forward order (127 then 63..0), ``v`` = black order (127 then 0..63).
_CAND_PROMOTED_WHITE = _POSITION_CANDIDATES[4]
_CAND_PROMOTED_BLACK = _POSITION_CANDIDATES[17]


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------


@dataclass
class Piece:
    """One of the 32 fixed piece slots.

    ``id`` is redundant with the slot index but kept for convenience/tests;
    serialisation always uses the slot index.  ``moved`` is only meaningful
    for king/rook (castling rights) and for pawns (double push / en passant);
    on decode it is derived from the encoded form exactly like the site does.
    """

    id: int
    pos: int
    type: int
    moved: bool = False


@dataclass
class Record:
    """One move, newest first in :attr:`View.records`."""

    id: int
    before: int
    after: int
    eat: Optional[int] = None
    sp: int = SP_NONE
    eatUnmoved: bool = False


@dataclass
class View:
    """A complete game-data snapshot.

    ``back_piece_id``/``back_piece_before`` mirror the proto fields 5/6
    (``backPieceId`` is stored on the wire as ``back_piece_id + 1``; 0 means
    "no value").  ``enable_ct``/``costs``/``pt``/``pt_owner`` hold the decoded
    optional chess-timer payload (proto field 7) so a snapshot can be
    re-encoded bit-exactly.
    """

    white_id: int
    state: int
    pieces: List[Piece]
    records: List[Record]
    back_piece_id: Optional[int] = None
    back_piece_before: Optional[int] = None
    enable_ct: bool = False
    costs: List[int] = field(default_factory=lambda: [0, 0])
    pt: int = 0
    pt_owner: Optional[int] = None


# ---------------------------------------------------------------------------
# bit codec (site module 3329)
# ---------------------------------------------------------------------------


class _BitError(ValueError):
    """Raised when reading past the end of an encoded bit stream."""


_BIT_MASKS = (0, 1, 3, 7, 15, 31, 63, 127, 255)


def _ceil_log2(n: int) -> int:
    """``ceil(log2(n))`` for ``n >= 1`` (JavaScript ``Math.ceil(Math.log2(n))``)."""
    if n <= 1:
        return 0
    return (n - 1).bit_length()


def _floor_log2(n: int) -> int:
    return n.bit_length() - 1


class _BitWriter:
    __slots__ = ("acc", "nbits", "out")

    def __init__(self) -> None:
        self.acc = 0
        self.nbits = 0
        self.out: List[int] = []

    def write_bits(self, value: int, bits: int) -> None:
        """Site ``writeBits`` (module 3329 ``c``): MSB-first."""
        if bits <= 0:
            return
        value = min(value, (1 << bits) - 1)
        if self.nbits + bits < 8:
            self.acc |= value << (8 - self.nbits - bits)
            self.nbits += bits
        elif self.nbits + bits == 8:
            self.out.append(self.acc | value)
            self.acc = 0
            self.nbits = 0
        else:
            self.acc |= value >> (self.nbits - 8 + bits)
            self.out.append(self.acc)
            self.acc = (value << (16 - self.nbits - bits)) & 0xFF
            self.nbits = self.nbits - 8 + bits

    def write_index(self, value: int, count: int) -> None:
        """Site ``writeIndex`` (module 3329 ``s``) truncated binary coding."""
        if count < 2:
            return
        lo = _floor_log2(count)
        hi = _ceil_log2(count)
        base = 1 << lo
        extra = count - base
        if lo == hi or value < base - extra:
            self.write_bits(value, lo)
        elif value < base:
            self.write_bits(value, lo)
            self.write_bits(0, 1)
        else:
            self.write_bits(value - extra, lo)
            self.write_bits(1, 1)

    def flush_pad_ones(self) -> bytes:
        """Site pieces/records flush: pad the low bits of the last byte with 1s."""
        if self.nbits:
            self.acc |= (1 << (8 - self.nbits)) - 1
            self.out.append(self.acc)
            self.acc = 0
            self.nbits = 0
        return bytes(self.out)

    def flush_raw(self) -> bytes:
        """Site ``ct`` flush: push the accumulator as-is (low bits stay 0)."""
        if self.nbits:
            self.out.append(self.acc)
            self.acc = 0
            self.nbits = 0
        return bytes(self.out)


class _BitReader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_bits(self, bits: int) -> int:
        """Site ``readBits`` (module 3329 ``l``)."""
        index = self.pos % 8
        byte_index = self.pos // 8
        if index + bits > 8:
            if byte_index + 1 >= len(self.data):
                raise _BitError("readBitsError")
            value = (self.data[byte_index] << 8) | self.data[byte_index + 1]
            value >>= 16 - bits - index
        else:
            if byte_index >= len(self.data):
                raise _BitError("readBitsError")
            value = self.data[byte_index]
            value >>= 8 - bits - index
        self.pos += bits
        if bits < len(_BIT_MASKS):
            value &= _BIT_MASKS[bits]
        elif bits:
            value &= (1 << bits) - 1
        return value

    def read_index(self, count: int) -> int:
        """Site ``readIndex`` (module 3329 ``u``)."""
        if not count:
            raise _BitError("readBitsError")
        if count == 1:
            return 0
        lo = _floor_log2(count)
        hi = _ceil_log2(count)
        base = 1 << lo
        extra = count - base
        value = self.read_bits(lo)
        if lo == hi or value < base - extra:
            return value
        flag = self.read_bits(1)
        return value + extra if flag else value


# ---------------------------------------------------------------------------
# small board helpers
# ---------------------------------------------------------------------------


def _make_initial_pieces() -> List[Piece]:
    return [Piece(i, _INITIAL_POSITIONS[i], _PIECE_TYPES[i], False) for i in range(32)]


def _copy_pieces(pieces: Sequence[Piece]) -> List[Piece]:
    return [Piece(p.id, p.pos, p.type, p.moved) for p in pieces]


def _piece_x(p: Piece) -> int:
    return -1 if p.pos == POS_CAPTURED else (p.pos & 7)


def _piece_y(p: Piece) -> int:
    return -1 if p.pos == POS_CAPTURED else (p.pos >> 3)


def _find_pos(pieces: Sequence[Piece], pos: int) -> int:
    for i, p in enumerate(pieces):
        if p.pos == pos:
            return i
    return -1


def _square_has_piece(pieces: Sequence[Piece], x: int, y: int) -> int:
    """First piece index on ``(x, y)``; captured pieces (x = y = -1) never match."""
    for i, p in enumerate(pieces):
        if p.pos == POS_CAPTURED:
            continue
        if (p.pos & 7) == x and (p.pos >> 3) == y:
            return i
    return -1


def _remove_occupied(moves: List[int], pieces: Sequence[Piece]) -> None:
    """Site ``k``: drop destinations that are occupied by any (alive) piece."""
    if not moves:
        return
    kept = [m for m in moves if _find_pos(pieces, m) < 0]
    moves[:] = kept


def _qv(pieces: Sequence[Piece]) -> int:
    """Site ``qv``: 1 = black king captured, 2 = white king captured, 0 = none."""
    if pieces[16].pos == POS_CAPTURED:
        return 1
    if pieces[0].pos == POS_CAPTURED:
        return 2
    return 0


# ---------------------------------------------------------------------------
# forward pseudo-move generators (site module 8234, used by the legal-move map)
# ---------------------------------------------------------------------------


def _slide_straight(pieces: Sequence[Piece], x: int, y: int) -> List[int]:
    out: List[int] = []
    for xx in range(x + 1, 8):
        out.append(xx + (y << 3))
        if _square_has_piece(pieces, xx, y) >= 0:
            break
    for xx in range(x - 1, -1, -1):
        out.append(xx + (y << 3))
        if _square_has_piece(pieces, xx, y) >= 0:
            break
    for yy in range(y - 1, -1, -1):
        out.append(x + (yy << 3))
        if _square_has_piece(pieces, x, yy) >= 0:
            break
    for yy in range(y + 1, 8):
        out.append(x + (yy << 3))
        if _square_has_piece(pieces, x, yy) >= 0:
            break
    return out


def _slide_diagonal(pieces: Sequence[Piece], x: int, y: int) -> List[int]:
    out: List[int] = []
    xx, yy = x + 1, y + 1
    while xx < 8 and yy < 8:
        out.append(xx + (yy << 3))
        if _square_has_piece(pieces, xx, yy) >= 0:
            break
        xx += 1
        yy += 1
    xx, yy = x + 1, y - 1
    while xx < 8 and yy >= 0:
        out.append(xx + (yy << 3))
        if _square_has_piece(pieces, xx, yy) >= 0:
            break
        xx += 1
        yy -= 1
    xx, yy = x - 1, y + 1
    while xx >= 0 and yy < 8:
        out.append(xx + (yy << 3))
        if _square_has_piece(pieces, xx, yy) >= 0:
            break
        xx -= 1
        yy += 1
    xx, yy = x - 1, y - 1
    while xx >= 0 and yy >= 0:
        out.append(xx + (yy << 3))
        if _square_has_piece(pieces, xx, yy) >= 0:
            break
        xx -= 1
        yy -= 1
    return out


def _king_steps(x: int, y: int) -> List[int]:
    out: List[int] = []
    if x > 0:
        out.append(x - 1 + (y << 3))
        if y > 0:
            out.append(x - 1 + ((y - 1) << 3))
        if y < 7:
            out.append(x - 1 + ((y + 1) << 3))
    if x < 7:
        out.append(x + 1 + (y << 3))
        if y > 0:
            out.append(x + 1 + ((y - 1) << 3))
        if y < 7:
            out.append(x + 1 + ((y + 1) << 3))
    if y > 0:
        out.append(x + ((y - 1) << 3))
    if y < 7:
        out.append(x + ((y + 1) << 3))
    return out


def _knight_steps(pos: int, x: int, y: int) -> List[int]:
    out: List[int] = []
    if x > 0 and y > 1:
        out.append(pos - 17)
    if x < 7 and y > 1:
        out.append(pos - 15)
    if x > 1 and y > 0:
        out.append(pos - 10)
    if x < 6 and y > 0:
        out.append(pos - 6)
    if x > 1 and y < 7:
        out.append(pos + 6)
    if x < 6 and y < 7:
        out.append(pos + 10)
    if x > 0 and y < 6:
        out.append(pos + 15)
    if x < 7 and y < 6:
        out.append(pos + 17)
    return out


# ---------------------------------------------------------------------------
# reverse ("undo") move generators (site module 8234 ``G``/``g``/``y``/``b``/``w``/``j``)
# These generate the *before* squares of a move from the position after it.
# Output may contain sentinels 126 / -126 and negative squares.
# ---------------------------------------------------------------------------


def _rook_rev(e: int, pieces: Sequence[Piece]) -> List[int]:
    """Site ``y``: rook reverse moves + ``126`` sentinel for an unmoved rook's home."""
    p = pieces[e]
    out = _slide_straight(pieces, _piece_x(p), _piece_y(p))
    _remove_occupied(out, pieces)
    if _INITIAL_POSITIONS[e] in out:
        out.append(POS_UNMOVED_SENTINEL)
    return out


def _queen_rev(e: int, pieces: Sequence[Piece]) -> List[int]:
    """Site ``g``: queen reverse moves (straight + diagonal)."""
    p = pieces[e]
    out = _slide_straight(pieces, _piece_x(p), _piece_y(p))
    out.extend(_slide_diagonal(pieces, _piece_x(p), _piece_y(p)))
    _remove_occupied(out, pieces)
    return out


def _bishop_rev(e: int, pieces: Sequence[Piece]) -> List[int]:
    """Site ``b``: bishop reverse moves."""
    p = pieces[e]
    out = _slide_diagonal(pieces, _piece_x(p), _piece_y(p))
    _remove_occupied(out, pieces)
    return out


def _knight_rev(e: int, pieces: Sequence[Piece]) -> List[int]:
    """Site ``w``: knight reverse moves."""
    p = pieces[e]
    out = _knight_steps(p.pos, _piece_x(p), _piece_y(p))
    _remove_occupied(out, pieces)
    return out


def _king_rev(e: int, pieces: Sequence[Piece], flag: bool) -> List[int]:
    """Site ``G``: king reverse moves (incl. castling ``-126`` and home ``126``)."""
    p = pieces[e]
    out = _king_steps(_piece_x(p), _piece_y(p))
    _remove_occupied(out, pieces)
    if _INITIAL_POSITIONS[e] in out:
        out.append(POS_UNMOVED_SENTINEL)
    state = (pieces, flag)
    if flag:
        # The side that just moved is black: forward castling targets are c8/g8.
        if p.pos == 2 and pieces[18].pos == 3:
            if (
                _find_pos(pieces, 0) < 0
                and _find_pos(pieces, 1) < 0
                and _find_pos(pieces, 4) < 0
                and not _attacked(state, False, (2, 3, 4))
            ):
                out.append(-POS_UNMOVED_SENTINEL)
        elif p.pos == 6 and pieces[19].pos == 5:
            if (
                _find_pos(pieces, 4) < 0
                and _find_pos(pieces, 7) < 0
                and not _attacked(state, False, (4, 5, 6))
            ):
                out.append(-POS_UNMOVED_SENTINEL)
    else:
        # The side that just moved is white: forward castling targets are c1/g1.
        if p.pos == 58 and pieces[3].pos == 59:
            if (
                _find_pos(pieces, 56) < 0
                and _find_pos(pieces, 57) < 0
                and _find_pos(pieces, 60) < 0
                and not _attacked(state, False, (58, 59, 60))
            ):
                out.append(-POS_UNMOVED_SENTINEL)
        elif p.pos == 62 and pieces[2].pos == 61:
            if (
                _find_pos(pieces, 60) < 0
                and _find_pos(pieces, 63) < 0
                and not _attacked(state, False, (60, 61, 62))
            ):
                out.append(-POS_UNMOVED_SENTINEL)
    return out


def _pawn_rev(e: int, pieces: Sequence[Piece], captured: Optional[int]) -> List[int]:
    """Site ``j``: pawn reverse moves (pawn ids, possibly promoted)."""
    p = pieces[e]
    x, y = _piece_x(p), _piece_y(p)
    out: List[int] = []
    direction = -1 if e > 15 else 1  # reverse of the normal pawn direction
    step_y = y + direction
    if p.type == PIECE_PAWN:
        if captured is None:
            double_y = y + 2 * direction
            init_x = _INITIAL_POSITIONS[e] & 7
            init_y = _INITIAL_POSITIONS[e] >> 3
            if init_x == x and init_y == double_y and _square_has_piece(pieces, x, step_y) < 0:
                out.append(x + (double_y << 3))
        if (step_y > 0) if e > 15 else (step_y < 7):
            if captured is not None:
                if x > 0:
                    out.append(x - 1 + (step_y << 3))
                if x < 7:
                    out.append(x + 1 + (step_y << 3))
            else:
                out.append(x + (step_y << 3))
        _remove_occupied(out, pieces)
        candidates = _POSITION_CANDIDATES[e]
        out = [m for m in out if m in candidates]
        if captured is not None:
            if pieces[captured].type == PIECE_PAWN and (y == 5 if e > 15 else y == 2):
                out.extend(-m for m in list(out))
    else:
        if p.type == PIECE_QUEEN:
            base = _slide_straight(pieces, x, y) + _slide_diagonal(pieces, x, y)
            _remove_occupied(base, pieces)
        elif p.type == PIECE_ROCK:
            base = _slide_straight(pieces, x, y)
            _remove_occupied(base, pieces)
        elif p.type == PIECE_BISHOP:
            base = _slide_diagonal(pieces, x, y)
            _remove_occupied(base, pieces)
        else:  # PIECE_KNIGHT
            base = _knight_steps(p.pos, x, y)
            _remove_occupied(base, pieces)
        out.extend(base)
        on_promotion_rank = (y > 6) if e > 15 else (y == 0)
        if on_promotion_rank:
            if captured is not None:
                if x > 0 and _square_has_piece(pieces, x - 1, step_y) < 0:
                    out.append(-(x - 1 + (step_y << 3)))
                if x < 7 and _square_has_piece(pieces, x + 1, step_y) < 0:
                    out.append(-(x + 1 + (step_y << 3)))
            else:
                if _square_has_piece(pieces, x, step_y) < 0:
                    out.append(-(x + (step_y << 3)))
    return out


def _reverse_moves(piece_id: int, state: "Tuple[List[Piece], bool]", captured: Optional[int]) -> List[int]:
    """Site ``M``: dispatch on the *fixed* piece type of ``piece_id``."""
    pieces, flag = state
    fixed = _PIECE_TYPES[piece_id]
    if fixed == PIECE_KING:
        return _king_rev(piece_id, pieces, flag)
    if fixed == PIECE_QUEEN:
        return _queen_rev(piece_id, pieces)
    if fixed == PIECE_ROCK:
        return _rook_rev(piece_id, pieces)
    if fixed == PIECE_BISHOP:
        return _bishop_rev(piece_id, pieces)
    if fixed == PIECE_KNIGHT:
        return _knight_rev(piece_id, pieces)
    return _pawn_rev(piece_id, pieces, captured)


# ---------------------------------------------------------------------------
# undo one record (site module 8234 ``H`` / ``VL``)
# ---------------------------------------------------------------------------


def _undo(state: "Tuple[List[Piece], bool]", rec: Record) -> "Tuple[List[Piece], bool]":
    """Undo ``rec``: returns the state *before* that move."""
    pieces, flag = state
    new_pieces = list(pieces)
    c = pieces[rec.id]
    new_c = Piece(c.id, rec.before, c.type, c.moved)
    new_pieces[rec.id] = new_c
    if rec.eat is not None:
        eaten = pieces[rec.eat]
        eaten_pos = rec.after
        if rec.sp == SP_IN_PASSING:
            eaten_y = (eaten_pos >> 3) + (-1 if flag else 1)
            eaten_pos = (eaten_pos & 7) + (eaten_y << 3)
        new_pieces[rec.eat] = Piece(eaten.id, eaten_pos, eaten.type, False if rec.eatUnmoved else eaten.moved)
    if new_c.type == PIECE_PAWN and (new_c.pos >> 3) == (1 if flag else 6):
        new_c.moved = False
    fixed = _PIECE_TYPES[rec.id]
    if fixed == PIECE_KING or fixed == PIECE_ROCK:
        if rec.sp == SP_INIT_MOVE:
            new_c.moved = False
        elif rec.sp == SP_CASTLING:
            new_c.moved = False
            if flag:
                rook_id = 19 if rec.after > 4 else 18
            else:
                rook_id = 2 if rec.after > 60 else 3
            rook = pieces[rook_id]
            new_pieces[rook_id] = Piece(rook.id, _INITIAL_POSITIONS[rook_id], rook.type, False)
    elif rec.sp == SP_PROMOTION and fixed == PIECE_PAWN:
        new_c.type = PIECE_PAWN
    return (new_pieces, not flag)


# ---------------------------------------------------------------------------
# alive / captured piece lists used by record coding (site ``B`` / ``W``)
# ---------------------------------------------------------------------------


def _alive_of_mover(state: "Tuple[List[Piece], bool]") -> List[int]:
    pieces, flag = state
    lo, hi = (16, 32) if flag else (0, 16)
    return [i for i in range(lo, hi) if pieces[i].pos < 127]


def _captured_of_mover(state: "Tuple[List[Piece], bool]") -> List[int]:
    pieces, flag = state
    lo, hi = (0, 16) if flag else (16, 32)
    return [i for i in range(lo, hi) if pieces[i].pos > 126]


# ---------------------------------------------------------------------------
# pieces stream (site module 8234 ``O`` / ``A`` and ``F`` / ``U``)
# ---------------------------------------------------------------------------


def encode_pieces(pieces: Sequence[Piece]) -> bytes:
    """Encode the 32-piece board state (site ``mR`` / ``A``)."""
    if len(pieces) < 32:
        raise ValueError("need 32 pieces")
    writer = _BitWriter()
    for idx in range(32):
        p = pieces[idx]
        fixed = _PIECE_TYPES[idx]
        value = POS_UNMOVED_SENTINEL if (fixed in (PIECE_KING, PIECE_ROCK) and p.pos < 127 and not p.moved) else p.pos
        if fixed == PIECE_PAWN:
            if p.type == PIECE_PAWN:
                writer.write_bits(0, 1)
                candidates = _POSITION_CANDIDATES[idx]
            else:
                writer.write_bits(1, 1)
                writer.write_bits(p.type - 1, 2)
                candidates = _CAND_PROMOTED_WHITE if idx < 16 else _CAND_PROMOTED_BLACK
        else:
            candidates = _POSITION_CANDIDATES[idx]
        writer.write_index(candidates.index(value), len(candidates))
    return writer.flush_pad_ones()


def decode_pieces(data: bytes) -> List[Piece]:
    """Decode the pieces stream (site ``wd`` / ``U``).

    Malformed input falls back to the initial position, like the site.
    """
    fallback = _make_initial_pieces()
    reader = _BitReader(data)
    pieces: List[Piece] = []
    try:
        for idx in range(32):
            fixed = _PIECE_TYPES[idx]
            ptype = fixed
            if fixed == PIECE_PAWN:
                if reader.read_bits(1):
                    ptype = reader.read_bits(2) + 1
                    candidates = _CAND_PROMOTED_WHITE if idx < 16 else _CAND_PROMOTED_BLACK
                else:
                    candidates = _POSITION_CANDIDATES[idx]
            else:
                candidates = _POSITION_CANDIDATES[idx]
            pos = candidates[reader.read_index(len(candidates))]
            if pos == POS_UNMOVED_SENTINEL:
                # Unmoved king/rook: the encoded sentinel stands for its home square.
                pieces.append(Piece(idx, _INITIAL_POSITIONS[idx], ptype, False))
                continue
            moved = False
            if ptype in (PIECE_KING, PIECE_ROCK):
                moved = True
            if ptype == PIECE_PAWN:
                yy = -1 if pos == POS_CAPTURED else (pos >> 3)
                moved = not ((yy == 1) if idx > 15 else (yy == 6))
            pieces.append(Piece(idx, pos, ptype, moved))
    except (_BitError, IndexError):
        return _copy_pieces(fallback)
    return pieces


# ---------------------------------------------------------------------------
# records stream (site module 8234 ``KR`` / ``J`` and ``_x`` / ``z``)
# ---------------------------------------------------------------------------


def encode_records(
    pieces_after: Sequence[Piece],
    is_white_turn_after: bool,
    records: Sequence[Record],
) -> bytes:
    """Encode records newest-first (site ``KR`` / ``$``).

    ``pieces_after`` / ``is_white_turn_after`` describe the position after all
    records have been applied (``is_white_turn_after`` is the site's state turn
    flag, i.e. :func:`is_white_turn` of the snapshot's state).
    """
    writer = _BitWriter()
    state: "Tuple[List[Piece], bool]" = (list(pieces_after), bool(is_white_turn_after))
    for rec in records:
        alive = _alive_of_mover(state)
        writer.write_index(alive.index(rec.id), len(alive))
        if rec.eat is None:
            writer.write_bits(0, 1)
        else:
            writer.write_bits(1, 1)
            captured = _captured_of_mover(state)
            writer.write_index(captured.index(rec.eat), len(captured))
            eat_fixed = _PIECE_TYPES[rec.eat]
            if eat_fixed in (PIECE_KING, PIECE_ROCK) and rec.after == _INITIAL_POSITIONS[rec.eat]:
                writer.write_bits(1 if rec.eatUnmoved else 0, 1)
        moves = _reverse_moves(rec.id, state, rec.eat)
        if rec.sp == SP_INIT_MOVE:
            idx = moves.index(POS_UNMOVED_SENTINEL)
        elif rec.sp == SP_IN_PASSING or rec.sp == SP_PROMOTION:
            idx = moves.index(-rec.before)
        elif rec.sp == SP_CASTLING:
            idx = moves.index(-POS_UNMOVED_SENTINEL)
        else:
            idx = moves.index(rec.before)
        writer.write_index(idx, len(moves))
        state = _undo(state, rec)
    return writer.flush_pad_ones()


def decode_records(
    pieces_after: Sequence[Piece],
    is_white_turn_after: bool,
    data: bytes,
) -> List[Record]:
    """Decode records newest-first (site ``_x`` / ``z``)."""
    records: List[Record] = []
    if not data:
        return records
    reader = _BitReader(data)
    state: "Tuple[List[Piece], bool]" = (list(pieces_after), bool(is_white_turn_after))
    total_bits = 8 * len(data)
    try:
        while reader.pos < total_bits:
            alive = _alive_of_mover(state)
            piece_id = alive[reader.read_index(len(alive))]
            rec = Record(piece_id, 0, state[0][piece_id].pos, None, SP_NONE, False)
            if reader.read_bits(1):
                captured = _captured_of_mover(state)
                if not captured:
                    break
                rec.eat = captured[reader.read_index(len(captured))]
                eat_fixed = _PIECE_TYPES[rec.eat]
                if eat_fixed in (PIECE_KING, PIECE_ROCK) and rec.after == _INITIAL_POSITIONS[rec.eat]:
                    rec.eatUnmoved = bool(reader.read_bits(1))
            moves = _reverse_moves(piece_id, state, rec.eat)
            move = moves[reader.read_index(len(moves))]
            if move == -POS_UNMOVED_SENTINEL:
                rec.sp = SP_CASTLING
                rec.before = _INITIAL_POSITIONS[piece_id]
            elif move == POS_UNMOVED_SENTINEL:
                rec.sp = SP_INIT_MOVE
                rec.before = _INITIAL_POSITIONS[piece_id]
            elif move < 0:
                rec.sp = SP_PROMOTION if (rec.after < 8 or rec.after > 55) else SP_IN_PASSING
                rec.before = -move
            else:
                rec.before = move
            records.append(rec)
            state = _undo(state, rec)
    except Exception:
        # The site's own decoder wraps the whole loop in try/catch and keeps the
        # records decoded so far (a turn/state mismatch can desynchronise the
        # stream).  Stay tolerant: never let a record-stream error escape.
        pass
    return records


# ---------------------------------------------------------------------------
# legal-move machinery (site module 8234 ``Q``/``L``/``N``/``S``/``T``/``K``/``C``)
# ---------------------------------------------------------------------------


def _pawn_attack_squares(e: int, pieces: Sequence[Piece]) -> List[int]:
    p = pieces[e]
    x, y = _piece_x(p), _piece_y(p)
    forward_y = y + (1 if e > 15 else -1)
    out: List[int] = []
    if 0 <= forward_y < 8:
        if x > 0:
            out.append(x - 1 + (forward_y << 3))
        if x < 7:
            out.append(x + 1 + (forward_y << 3))
    return out


def _pseudo_moves(e: int, state: "Tuple[List[Piece], bool]") -> List[int]:
    """Site ``Q``: pseudo moves (no castling / en passant) of any piece."""
    pieces = state[0]
    p = pieces[e]
    if p.pos > 126:
        return []
    kind = p.type
    x, y = _piece_x(p), _piece_y(p)
    if kind == PIECE_KING:
        return _king_steps(x, y)
    if kind == PIECE_QUEEN:
        return _slide_straight(pieces, x, y) + _slide_diagonal(pieces, x, y)
    if kind == PIECE_ROCK:
        return _slide_straight(pieces, x, y)
    if kind == PIECE_BISHOP:
        return _slide_diagonal(pieces, x, y)
    if kind == PIECE_KNIGHT:
        return _knight_steps(p.pos, x, y)
    return _pawn_attack_squares(e, pieces)


def _attacked(state: "Tuple[List[Piece], bool]", check_opponent: bool, squares: Sequence[int]) -> bool:
    """Site ``L``.

    With ``check_opponent=True`` it reports whether the side *not* to move
    attacks any of ``squares``; with ``False`` it reports the side to move
    (the site's quirk: the flag selects the attacker group differently).
    """
    pieces, flag = state
    if ((not flag) if check_opponent else flag):
        ids = range(0, 16)
    else:
        ids = range(16, 32)
    target = set(squares)
    for i in ids:
        if target.intersection(_pseudo_moves(i, state)):
            return True
    return False


def _in_check(state: "Tuple[List[Piece], bool]", check_opponent: bool) -> bool:
    """Site ``N``."""
    pieces, flag = state
    if ((not flag) if check_opponent else flag):
        king_pos = pieces[16].pos
        ids = range(0, 16)
    else:
        king_pos = pieces[0].pos
        ids = range(16, 32)
    for i in ids:
        if king_pos in _pseudo_moves(i, state):
            return True
    return False


def _apply_unchecked(
    state: "Tuple[List[Piece], bool]", piece_id: int, after: int
) -> "Tuple[List[Piece], bool]":
    """Site ``S``: make a move without validation (used only for check tests)."""
    pieces, flag = state
    new_pieces = list(pieces)
    moving = pieces[piece_id]
    before = moving.pos
    is_white = piece_id < 16
    captured = _find_pos(new_pieces, after)
    if captured < 0 and moving.type == PIECE_PAWN and (after & 7) != (before & 7):
        captured = _find_pos(new_pieces, after + (8 if is_white else -8))
    if captured >= 0:
        new_pieces[captured] = Piece(new_pieces[captured].id, POS_CAPTURED, new_pieces[captured].type, new_pieces[captured].moved)
    if moving.type == PIECE_KING and abs(after - before) == 2:
        if is_white:
            rook_id = 2 if after == 62 else 3
        else:
            rook_id = 18 if after == 2 else 19
        rook_x = (after & 7)
        rook_x = rook_x - 1 if rook_x > 4 else 1 + rook_x
        rook_y = after >> 3
        rook = new_pieces[rook_id]
        new_pieces[rook_id] = Piece(rook.id, rook_x + (rook_y << 3), rook.type, rook.moved)
    new_pieces[piece_id] = Piece(moving.id, after, moving.type, moving.moved)
    return (new_pieces, flag)


def _legal_one(state: "Tuple[List[Piece], bool]", piece_id: int, last: Optional[Record]) -> List[int]:
    """Site ``T``: filter ``K`` moves that would leave the mover's king in check."""
    pieces, flag = state
    is_white = piece_id < 16
    king_pos = pieces[16 if is_white else 0].pos
    out: List[int] = []
    for move in _pseudo_legal_moves(piece_id, state, last):
        if move == king_pos:
            out.append(move)
            continue
        trial = _apply_unchecked(state, piece_id, move)
        trial_state = (trial[0], is_white)
        if not _in_check(trial_state, True):
            out.append(move)
    return out


def _pseudo_legal_moves(
    piece_id: int, state: "Tuple[List[Piece], bool]", last: Optional[Record]
) -> List[int]:
    """Site ``K``: forward moves of one piece incl. castling / en passant.

    Own-side occupied squares are removed at the end (exactly like the site).
    """
    pieces, flag = state
    moving = pieces[piece_id]
    if moving.pos > 126:
        return []
    kind = moving.type
    x, y = _piece_x(moving), _piece_y(moving)
    out: List[int] = []
    if kind == PIECE_KING:
        out.extend(_king_steps(x, y))
        if not moving.moved:
            if piece_id > 15:
                if not pieces[18].moved and all(_find_pos(pieces, sq) < 0 for sq in (1, 2, 3)):
                    if not _attacked(state, True, (2, 3, 4)):
                        out.append(2)
                if not pieces[19].moved and all(_find_pos(pieces, sq) < 0 for sq in (5, 6)):
                    if not _attacked(state, True, (4, 5, 6)):
                        out.append(6)
            else:
                if not pieces[3].moved and all(_find_pos(pieces, sq) < 0 for sq in (57, 58, 59)):
                    if not _attacked(state, True, (58, 59, 60)):
                        out.append(58)
                if not pieces[2].moved and all(_find_pos(pieces, sq) < 0 for sq in (61, 62)):
                    if not _attacked(state, True, (60, 61, 62)):
                        out.append(62)
    elif kind == PIECE_QUEEN:
        out.extend(_slide_straight(pieces, x, y))
        out.extend(_slide_diagonal(pieces, x, y))
    elif kind == PIECE_ROCK:
        out.extend(_slide_straight(pieces, x, y))
    elif kind == PIECE_BISHOP:
        out.extend(_slide_diagonal(pieces, x, y))
    elif kind == PIECE_KNIGHT:
        out.extend(_knight_steps(moving.pos, x, y))
    else:  # pawn
        direction = 1 if piece_id > 15 else -1
        double_y = y + 2 * direction
        step_y = y + direction
        if (
            not moving.moved
            and _square_has_piece(pieces, x, double_y) < 0
            and _square_has_piece(pieces, x, step_y) < 0
        ):
            out.append(x + (double_y << 3))
        if 0 <= step_y < 8:
            if _square_has_piece(pieces, x, step_y) < 0:
                out.append(x + (step_y << 3))
            if x > 0 and _square_has_piece(pieces, x - 1, step_y) >= 0:
                out.append(x - 1 + (step_y << 3))
            if x < 7 and _square_has_piece(pieces, x + 1, step_y) >= 0:
                out.append(x + 1 + (step_y << 3))
        if (
            y == (4 if piece_id > 15 else 3)
            and last is not None
            and pieces[last.id].type == PIECE_PAWN
            and abs(last.after - last.before) == 16
            and abs((last.after & 7) - x) == 1
        ):
            out.append((last.after + last.before) >> 1)
    # Remove own-side occupied squares (site's post-filter loop).
    filtered: List[int] = []
    for move in out:
        occupant = _find_pos(pieces, move)
        if occupant >= 0 and ((occupant > 15) if piece_id > 15 else (occupant < 16)):
            continue
        filtered.append(move)
    return filtered


def _legal_map(
    state: "Tuple[List[Piece], bool]", last: Optional[Record]
) -> Dict[int, List[int]]:
    """Site ``C`` / ``nu``: legal moves of the side to move (piece id -> squares)."""
    pieces, flag = state
    result: Dict[int, List[int]] = {}
    start = 0 if flag else 16
    for piece_id in range(start, start + 16):
        if pieces[piece_id].pos > 126:
            continue
        moves = _legal_one(state, piece_id, last)
        if moves:
            result[piece_id] = moves
    return result


def legal_moves(view: View) -> Optional[Dict[int, List[int]]]:
    """Legal moves map of the side to move (site ``nu``), ``None`` if not playing."""
    if not is_playing(view):
        return None
    state: "Tuple[List[Piece], bool]" = (list(view.pieces), is_white_turn(view))
    last = view.records[0] if view.records else None
    return _legal_map(state, last)


def is_in_check(view: View) -> bool:
    """Whether the side to move is in check (site ``DX``)."""
    if not is_playing(view):
        return False
    state: "Tuple[List[Piece], bool]" = (list(view.pieces), is_white_turn(view))
    return _in_check(state, True)


# ---------------------------------------------------------------------------
# move application (site module 3267 ``oe`` / ``P`` and ``B`` / ``wj``)
# ---------------------------------------------------------------------------


def _promotion_needed(view: View, piece_id: int, after: int) -> bool:
    return view.pieces[piece_id].type == PIECE_PAWN and (after < 8 or after > 55)


def _validate_move(view: View, piece_id: int, after: int, promotion_type: Optional[int]) -> Optional[str]:
    if not is_playing(view):
        return "当前不能移动棋子"
    if not isinstance(piece_id, int) or isinstance(piece_id, bool) or piece_id < 0 or piece_id >= 32:
        return "棋子无效"
    if not isinstance(after, int) or isinstance(after, bool) or after < 0 or after >= 64:
        return "目标位置无效"
    if view.pieces[piece_id].pos == POS_CAPTURED:
        return "棋子已被吃"
    if (piece_id < 16) != is_white_turn(view):
        return "还没轮到这一方"
    state: "Tuple[List[Piece], bool]" = (list(view.pieces), is_white_turn(view))
    last = view.records[0] if view.records else None
    moves = _legal_map(state, last).get(piece_id)
    if not moves or after not in moves:
        return "棋子不能移动到这里"
    need_promotion = _promotion_needed(view, piece_id, after)
    if need_promotion:
        if promotion_type not in (PIECE_QUEEN, PIECE_ROCK, PIECE_BISHOP, PIECE_KNIGHT):
            return "请选择升变棋子"
    elif promotion_type is not None:
        return "当前不能升变"
    return None


def apply_move(
    view: View, piece_id: int, after: int, promotion_type: Optional[int] = None
) -> Tuple[Optional[View], Optional[str]]:
    """Apply one move, mirroring the site's ``oe`` (returns ``(view, error)``)."""
    error = _validate_move(view, piece_id, after, promotion_type)
    if error is not None:
        return None, error
    pieces = _copy_pieces(view.pieces)
    moving = view.pieces[piece_id]
    before = moving.pos
    is_white = piece_id < 16
    captured = _find_pos(pieces, after)
    rec = Record(piece_id, before, after, captured if captured >= 0 else None)
    if captured >= 0:
        captured_fixed = _PIECE_TYPES[captured]
        if captured_fixed in (PIECE_KING, PIECE_ROCK) and not pieces[captured].moved:
            rec.eatUnmoved = True
        pieces[captured] = Piece(pieces[captured].id, POS_CAPTURED, pieces[captured].type, pieces[captured].moved)
    pieces[piece_id] = Piece(moving.id, after, moving.type, True)
    fixed = _PIECE_TYPES[piece_id]
    if fixed in (PIECE_KING, PIECE_ROCK):
        if not moving.moved:
            if fixed == PIECE_KING and abs(before - after) == 2:
                rec.sp = SP_CASTLING
                if is_white:
                    rook_id = 2 if after == 62 else 3
                else:
                    rook_id = 18 if after == 2 else 19
                rook_x = (after & 7)
                rook_x = rook_x - 1 if rook_x > 4 else 1 + rook_x
                rook_y = after >> 3
                rook = pieces[rook_id]
                pieces[rook_id] = Piece(rook.id, rook_x + (rook_y << 3), rook.type, True)
            else:
                rec.sp = SP_INIT_MOVE
    elif moving.type == PIECE_PAWN:
        if captured < 0 and abs(after - before) not in (8, 16):
            rec.sp = SP_IN_PASSING
            enemy_pawn = _find_pos(pieces, after + (8 if is_white else -8))
            if enemy_pawn < 0:
                return None, "找不到被吃的过路兵"
            rec.eat = enemy_pawn
            pieces[enemy_pawn] = Piece(pieces[enemy_pawn].id, POS_CAPTURED, pieces[enemy_pawn].type, pieces[enemy_pawn].moved)
        if promotion_type is not None:
            rec.sp = SP_PROMOTION
            pieces[piece_id] = Piece(pieces[piece_id].id, pieces[piece_id].pos, promotion_type, pieces[piece_id].moved)
    king_result = _qv(pieces)
    if king_result:
        new_state = STATE_WHITE_WIN if is_white else STATE_BLACK_WIN
    else:
        new_state = STATE_WAIT_BLACK if is_white else STATE_WAIT_WHITE
    new_view = View(
        white_id=view.white_id,
        state=new_state,
        pieces=pieces,
        records=[rec] + list(view.records),
        back_piece_id=None,
        back_piece_before=None,
        enable_ct=view.enable_ct,
        costs=list(view.costs),
        pt=view.pt,
        pt_owner=view.pt_owner,
    )
    return new_view, None


# ---------------------------------------------------------------------------
# optional chess-timer payload (site module 1990: ``HV``/``N6``/``cZ``)
# ---------------------------------------------------------------------------


def _ct_cost_bits(n_costs: int) -> int:
    return _ceil_log2(n_costs + 1)


def _decode_ct(data: bytes, n_costs: int = 2) -> Dict[str, object]:
    result: Dict[str, object] = {
        "enable_ct": bool(data),
        "costs": [0] * n_costs,
        "pt": 0,
        "pt_owner": None,
    }
    needed = (20 + 12 * n_costs + _ct_cost_bits(n_costs) + 7) // 8
    if not data or len(data) < needed:
        return result
    reader = _BitReader(data)
    pt = (reader.read_bits(4) << 16) | (reader.read_bits(8) << 8) | reader.read_bits(8)
    costs = [(reader.read_bits(4) << 8) | reader.read_bits(8) for _ in range(n_costs)]
    owner_bits = _ct_cost_bits(n_costs)
    raw_owner = reader.read_bits(owner_bits) if owner_bits else 0
    result["enable_ct"] = True
    result["pt"] = pt
    result["costs"] = costs
    result["pt_owner"] = raw_owner - 1 if 0 < raw_owner <= n_costs else None
    return result


def _encode_ct(view: View) -> Optional[bytes]:
    if not view.enable_ct:
        return None
    costs = list(view.costs) if view.costs is not None else [0, 0]
    writer = _BitWriter()
    pt = view.pt or 0
    writer.write_bits((pt >> 16) & 15, 4)
    writer.write_bits((pt >> 8) & 255, 8)
    writer.write_bits(pt & 255, 8)
    for cost in costs:
        cost = max(0, min(_CT_COST_MAX, int(cost)))
        writer.write_bits((cost >> 8) & 15, 4)
        writer.write_bits(cost & 255, 8)
    owner_bits = _ct_cost_bits(len(costs))
    owner = 0 if view.pt_owner is None else view.pt_owner + 1
    if owner_bits:
        writer.write_bits(owner, owner_bits)
    return writer.flush_raw()


# ---------------------------------------------------------------------------
# GJXQGameData protobuf wrapper (site module 2732 + 3267 ``qA`` / ``my``)
# ---------------------------------------------------------------------------

_PROTO_FIELD_WHITE_ID = 1
_PROTO_FIELD_STATE = 2
_PROTO_FIELD_PIECES = 3
_PROTO_FIELD_RECORDS = 4
_PROTO_FIELD_BACK_PIECE_ID = 5
_PROTO_FIELD_BACK_PIECE_BEFORE = 6
_PROTO_FIELD_CT = 7


def _write_varint(out: List[int], value: int) -> None:
    value &= 0xFFFFFFFFFFFFFFFF
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return


def _write_tag(out: List[int], field_number: int, wire_type: int) -> None:
    _write_varint(out, (field_number << 3) | wire_type)


def _write_uint32_field(out: List[int], field_number: int, value: int) -> None:
    _write_tag(out, field_number, 0)
    _write_varint(out, value)


def _write_bytes_field(out: List[int], field_number: int, value: bytes) -> None:
    _write_tag(out, field_number, 2)
    _write_varint(out, len(value))
    out.extend(value)


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    if wire_type == 0:
        _, pos = _read_varint(data, pos)
        return pos
    if wire_type == 1:
        return pos + 8
    if wire_type == 2:
        length, pos = _read_varint(data, pos)
        return pos + length
    if wire_type == 5:
        return pos + 4
    raise ValueError("unsupported protobuf wire type: %d" % wire_type)


def _parse_game_data(data: bytes) -> Dict[int, object]:
    fields: Dict[int, object] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field_number = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            value = data[pos:pos + length]
            pos += length
        else:
            pos = _skip_field(data, pos, wire_type)
            continue
        fields[field_number] = value
    return fields


def initial_view(white_id: int = 0, enable_ct: bool = False) -> View:
    """Initial position (site ``GL``)."""
    return View(
        white_id=1 if white_id == 1 else 0,
        state=STATE_WAIT_WHITE,
        pieces=_make_initial_pieces(),
        records=[],
        back_piece_id=None,
        back_piece_before=None,
        enable_ct=bool(enable_ct),
        costs=[0, 0],
        pt=0,
        pt_owner=None,
    )


def _record_turn_flag(view: View) -> bool:
    """Turn flag (``is_white_turn``-shaped) implied by the move history.

    The side that made the newest record is the side that moved last, so the
    state's turn flag must be "last mover is black" == ``records[0].id > 15``.
    This is independent of ``view.state`` and is used when a caller patches the
    winner state (resign / claim) onto a position without touching the records.
    """
    if view.records:
        return view.records[0].id > 15
    return is_white_turn(view)


def encode_game_data(view: View, *, state_override: Optional[int] = None) -> bytes:
    """Encode a :class:`View` to GJXQGameData protobuf bytes (site ``qA``).

    ``state_override`` is an optional protocol escape hatch: pieces and records
    are encoded from the move history (so the record turn always matches the
    records), while field 2 on the wire is written as ``state_override`` instead
    of ``view.state``.  This is used to publish a winner state (e.g. out-of-turn
    resign / claim) without flipping the turn implied by the record stream.
    """
    wire_state = view.state if state_override is None else int(state_override)
    if state_override is None:
        record_flag = is_white_turn(view)
        record_bytes = encode_records(view.pieces, record_flag, view.records)
    else:
        record_flag = _record_turn_flag(view)
        try:
            record_bytes = encode_records(view.pieces, record_flag, view.records)
        except ValueError:
            # Defensive: a view that was decoded tolerantly may carry records
            # that disagree with its pieces; retry with the state's own flag.
            record_bytes = encode_records(view.pieces, is_white_turn(view), view.records)
    out: List[int] = []
    _write_uint32_field(out, _PROTO_FIELD_WHITE_ID, view.white_id)
    _write_uint32_field(out, _PROTO_FIELD_STATE, wire_state)
    _write_bytes_field(out, _PROTO_FIELD_PIECES, encode_pieces(view.pieces))
    _write_bytes_field(out, _PROTO_FIELD_RECORDS, record_bytes)
    back_piece_id = 0 if view.back_piece_id is None else view.back_piece_id + 1
    back_piece_before = 0 if view.back_piece_before is None else view.back_piece_before
    _write_uint32_field(out, _PROTO_FIELD_BACK_PIECE_ID, back_piece_id)
    _write_uint32_field(out, _PROTO_FIELD_BACK_PIECE_BEFORE, back_piece_before)
    ct = _encode_ct(view)
    if ct is not None:
        _write_bytes_field(out, _PROTO_FIELD_CT, ct)
    return bytes(out)


def _castling_rook_id(rec: Record) -> int:
    """Rook piece id that moved during a castling record (derived from the king target)."""
    if rec.id == 0:  # white king
        return 2 if rec.after == 62 else 3
    return 19 if rec.after == 6 else 18


def _reconstruct_live_moved(pieces: List[Piece], records: Sequence[Record]) -> None:
    """Best-effort recovery of the site's *live* ``moved`` flags.

    The wire format only stores ``moved`` for king/rook (via the 126 sentinel);
    every other piece's flag is implied by the move history.  The golden vectors
    snapshot the live view, so we replay that inference here.  Re-encoding is
    unaffected (non king/rook ``moved`` flags are never serialised).
    """
    moved_ids = {rec.id for rec in records}
    forced_unmoved = set()
    for rec in records:
        if rec.sp == SP_CASTLING:
            moved_ids.add(_castling_rook_id(rec))
        if rec.eat is not None and rec.eatUnmoved:
            forced_unmoved.add(rec.eat)
    for idx, p in enumerate(pieces):
        fixed = _PIECE_TYPES[idx]
        if fixed in (PIECE_KING, PIECE_ROCK):
            if idx in forced_unmoved:
                p.moved = False
            continue
        p.moved = idx in moved_ids


def decode_game_data(data: bytes) -> View:
    """Decode GJXQGameData protobuf bytes into a :class:`View` (site ``my``)."""
    fields = _parse_game_data(data)
    white_id = 1 if fields.get(_PROTO_FIELD_WHITE_ID) == 1 else 0
    state = int(fields.get(_PROTO_FIELD_STATE, 0))
    pieces_bytes = fields.get(_PROTO_FIELD_PIECES, b"")
    if not isinstance(pieces_bytes, (bytes, bytearray)):
        pieces_bytes = b""
    pieces = decode_pieces(bytes(pieces_bytes)) if pieces_bytes else _make_initial_pieces()
    records_bytes = fields.get(_PROTO_FIELD_RECORDS, b"")
    if not isinstance(records_bytes, (bytes, bytearray)):
        records_bytes = b""
    try:
        records = decode_records(pieces, _is_white_turn_state(state), bytes(records_bytes))
    except Exception:  # pragma: no cover - defensive, mirrors the site's try/catch
        records = []
    _reconstruct_live_moved(pieces, records)
    raw_back_piece_id = int(fields.get(_PROTO_FIELD_BACK_PIECE_ID, 0) or 0)
    raw_back_piece_before = int(fields.get(_PROTO_FIELD_BACK_PIECE_BEFORE, 0) or 0)
    back_piece_id = raw_back_piece_id - 1 if raw_back_piece_id else None
    back_piece_before = raw_back_piece_before if raw_back_piece_id else None
    ct_bytes = fields.get(_PROTO_FIELD_CT)
    try:
        ct_info = _decode_ct(bytes(ct_bytes) if isinstance(ct_bytes, (bytes, bytearray)) else b"")
    except Exception:  # pragma: no cover - opaque payload, never fail the view
        ct_info = {"enable_ct": bool(ct_bytes), "costs": [0, 0], "pt": 0, "pt_owner": None}
    return View(
        white_id=white_id,
        state=state,
        pieces=pieces,
        records=records,
        back_piece_id=back_piece_id,
        back_piece_before=back_piece_before,
        enable_ct=bool(ct_info["enable_ct"]),
        costs=list(ct_info["costs"]),  # type: ignore[arg-type]
        pt=int(ct_info["pt"]),  # type: ignore[arg-type]
        pt_owner=ct_info["pt_owner"],  # type: ignore[assignment]
    )


# ---------------------------------------------------------------------------
# state helpers (site module 3267 ``J7`` / ``jV`` / ``Hk`` / ``q7`` ...)
# ---------------------------------------------------------------------------


def _is_white_turn_state(state: int) -> bool:
    """Site ``J7``: the side that "acts as white" in the given state enum.

    Verbatim port (quirky on purpose): true for WaitWhite, BlackReqBack and
    BlackWin.  Used for record coding and for "whose turn is it".
    """
    return state == STATE_WAIT_WHITE or state == STATE_BLACK_REQ_BACK or state == STATE_BLACK_WIN


def is_white_turn(view: View) -> bool:
    """True when white is the side to act (site ``J7``)."""
    return _is_white_turn_state(view.state)


def is_playing(view: View) -> bool:
    """True while the game is running (WaitWhite / WaitBlack)."""
    return view.state == STATE_WAIT_WHITE or view.state == STATE_WAIT_BLACK


def is_finished(view: View) -> bool:
    """True when either side has won (WhiteWin / BlackWin)."""
    return view.state == STATE_WHITE_WIN or view.state == STATE_BLACK_WIN


def piece_at(view: View, pos: int) -> Optional[Piece]:
    """Piece occupying ``pos`` or ``None``."""
    for p in view.pieces:
        if p.pos == pos:
            return p
    return None


_FILE_CHARS = "abcdefgh"


def pos_to_square(pos: int) -> str:
    """``52 -> "e2"``."""
    if not 0 <= pos < 64:
        return ""
    return "%s%d" % (_FILE_CHARS[pos & 7], 8 - (pos >> 3))


def square_to_pos(square: str) -> int:
    """``"e2" -> 52``; raises ``ValueError`` for invalid squares."""
    if len(square) != 2:
        raise ValueError("invalid square: %r" % (square,))
    file_ch = square[0].lower()
    rank_ch = square[1]
    if file_ch not in _FILE_CHARS or rank_ch not in "12345678":
        raise ValueError("invalid square: %r" % (square,))
    return _FILE_CHARS.index(file_ch) + ((8 - int(rank_ch)) << 3)


_FEN_LETTERS = {PIECE_KING: "k", PIECE_QUEEN: "q", PIECE_ROCK: "r", PIECE_BISHOP: "b", PIECE_KNIGHT: "n", PIECE_PAWN: "p"}


def view_to_fen(view: View) -> str:
    """Best-effort standard FEN for the position; ``""`` when not playing.

    Castling rights come from the ``moved`` flags plus the pieces' squares;
    the en-passant target is emitted only for a last-record double pawn push.
    Halfmove clock is always 0 (not tracked by the site's snapshots).
    """
    if not is_playing(view):
        return ""
    board: List[List[Optional[Piece]]] = [[None] * 8 for _ in range(8)]
    for p in view.pieces:
        if p.pos == POS_CAPTURED:
            continue
        x, y = p.pos & 7, p.pos >> 3
        board[y][x] = p
    rows: List[str] = []
    for y in range(8):
        empty = 0
        row: List[str] = []
        for x in range(8):
            p = board[y][x]
            if p is None:
                empty += 1
                continue
            if empty:
                row.append(str(empty))
                empty = 0
            letter = _FEN_LETTERS[p.type]
            row.append(letter.upper() if p.id < 16 else letter)
        if empty:
            row.append(str(empty))
        rows.append("".join(row))
    color = "w" if is_white_turn(view) else "b"
    castling = ""
    white_king, black_king = view.pieces[0], view.pieces[16]
    if not white_king.moved and white_king.pos == 60:
        if not view.pieces[2].moved and view.pieces[2].pos == 63:
            castling += "K"
        if not view.pieces[3].moved and view.pieces[3].pos == 56:
            castling += "Q"
    if not black_king.moved and black_king.pos == 4:
        if not view.pieces[19].moved and view.pieces[19].pos == 7:
            castling += "k"
        if not view.pieces[18].moved and view.pieces[18].pos == 0:
            castling += "q"
    if not castling:
        castling = "-"
    en_passant = "-"
    if view.records:
        last = view.records[0]
        if (
            _PIECE_TYPES[last.id] == PIECE_PAWN
            and last.eat is None
            and last.sp == SP_NONE
            and abs(last.after - last.before) == 16
        ):
            en_passant = pos_to_square((last.after + last.before) >> 1)
    halfmove = 0
    fullmove = max(1, len(view.records) // 2 + 1)
    return "%s %s %s %s %d %d" % ("/".join(rows), color, castling, en_passant, halfmove, fullmove)
