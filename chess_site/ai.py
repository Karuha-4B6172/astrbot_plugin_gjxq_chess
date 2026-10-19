"""GJXQ 国际象棋 AI —— 基于 python-chess 的 alpha-beta 走子选择器。

对外接口::

    def choose_move(
        fen: str,
        depth: int = 3,
        time_limit: float = 2.0,
        elo: int | None = None,
    ) -> str | None

* 返回 UCI 走法字符串（如 ``"e2e4"``，升变为 ``"e7e8q"``，小写升变子）。
* 当 FEN 非法、局面已结束（将死/逼和等）或没有合法走法时返回 ``None``。
* 迭代加深 + alpha-beta 剪枝，评估函数为 材料 + 棋子位置表（含中心控制），
  走法排序与同分选择均为确定性的（以 UCI 字符串作为最终 tie-break）。
* 通过 ``time.monotonic()`` 强制 ``time_limit``；超时返回最后一个完整迭代的最佳走法。
  另有节点预算 ``_MAX_NODES`` 作为兜底。
* **Elo 难度**：传入 ``elo``（600-2400，0/None 表示关闭）时，由
  :func:`elo_profile` 推导搜索深度、思考时间、可选走法分差窗口和随机失误率，
  模拟对应水平的棋力。Elo 只是近似难度标签，不等同于真实等级分。
* 仅依赖标准库与 python-chess，不 import astrbot。

导入安全（缺少依赖时不崩溃）::

    AVAILABLE: bool   # 运行环境是否可导入 python-chess

当 ``AVAILABLE`` 为 False 时，导入本模块仍然成功；只有调用 ``choose_move``
才会抛出带中文提示的 ``RuntimeError``（调用方可以捕获后展示给用户）。
"""

from __future__ import annotations

import random
import time

try:  # pragma: no cover - 是否走到 except 取决于运行环境
    import chess  # type: ignore

    AVAILABLE: bool = True
except Exception:  # pragma: no cover - 缺少 python-chess 时走这里
    chess = None  # type: ignore
    AVAILABLE = False


# ---------------------------------------------------------------------------
# 评估常量（数值与 python-chess 的 piece_type 编码一致，模块导入不依赖 chess）
# ---------------------------------------------------------------------------

PAWN = 1
KNIGHT = 2
BISHOP = 3
ROOK = 4
QUEEN = 5
KING = 6

_PIECE_VALUES = {
    PAWN: 100,
    KNIGHT: 320,
    BISHOP: 330,
    ROOK: 500,
    QUEEN: 900,
    KING: 0,
}

# 棋子位置表（白方视角，行序为第 8 行 -> 第 1 行，即 index 0 = a8）。
# 黑方使用镜像：白方 square ^ 56，黑方 square。
_PAWN_PST = [
    0, 0, 0, 0, 0, 0, 0, 0,
    50, 50, 50, 50, 50, 50, 50, 50,
    10, 10, 20, 30, 30, 20, 10, 10,
    5, 5, 10, 25, 25, 10, 5, 5,
    0, 0, 0, 20, 20, 0, 0, 0,
    5, -5, -10, 0, 0, -10, -5, 5,
    5, 10, 10, -20, -20, 10, 10, 5,
    0, 0, 0, 0, 0, 0, 0, 0,
]

_KNIGHT_PST = [
    -50, -40, -30, -30, -30, -30, -40, -50,
    -40, -20, 0, 0, 0, 0, -20, -40,
    -30, 0, 10, 15, 15, 10, 0, -30,
    -30, 5, 15, 20, 20, 15, 5, -30,
    -30, 0, 15, 20, 20, 15, 0, -30,
    -30, 5, 10, 15, 15, 10, 5, -30,
    -40, -20, 0, 5, 5, 0, -20, -40,
    -50, -40, -30, -30, -30, -30, -40, -50,
]

_BISHOP_PST = [
    -20, -10, -10, -10, -10, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 10, 10, 5, 0, -10,
    -10, 5, 5, 10, 10, 5, 5, -10,
    -10, 0, 10, 10, 10, 10, 0, -10,
    -10, 10, 10, 10, 10, 10, 10, -10,
    -10, 5, 0, 0, 0, 0, 5, -10,
    -20, -10, -10, -10, -10, -10, -10, -20,
]

_ROOK_PST = [
    0, 0, 0, 0, 0, 0, 0, 0,
    5, 10, 10, 10, 10, 10, 10, 5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    -5, 0, 0, 0, 0, 0, 0, -5,
    0, 0, 0, 5, 5, 0, 0, 0,
]

_QUEEN_PST = [
    -20, -10, -10, -5, -5, -10, -10, -20,
    -10, 0, 0, 0, 0, 0, 0, -10,
    -10, 0, 5, 5, 5, 5, 0, -10,
    -5, 0, 5, 5, 5, 5, 0, -5,
    0, 0, 5, 5, 5, 5, 0, -5,
    -10, 5, 5, 5, 5, 5, 0, -10,
    -10, 0, 5, 0, 0, 0, 0, -10,
    -20, -10, -10, -5, -5, -10, -10, -20,
]

_KING_PST = [
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -30, -40, -40, -50, -50, -40, -40, -30,
    -20, -30, -30, -40, -40, -30, -30, -20,
    -10, -20, -20, -20, -20, -20, -20, -10,
    20, 20, 0, 0, 0, 0, 20, 20,
    20, 30, 10, 0, 0, 10, 30, 20,
]

_PST = {
    PAWN: _PAWN_PST,
    KNIGHT: _KNIGHT_PST,
    BISHOP: _BISHOP_PST,
    ROOK: _ROOK_PST,
    QUEEN: _QUEEN_PST,
    KING: _KING_PST,
}

# 将死分数：远大于任何材料分；离将死越近分数越高。
_MATE = 100000

# 节点预算兜底（即使调用方给了很大的 time_limit 也不会无限跑）。
_MAX_NODES = 2_000_000

# 深度上限（防止调用方传入离谱的 depth 造成无意义的长搜索；时间限制仍会先生效）。
_MAX_DEPTH = 8

# 每 256 个节点检查一次时钟，避免 time.monotonic() 调用开销。
_TIME_CHECK_MASK = 0xFF


class _SearchAbort(Exception):
    """内部使用：搜索因超时或节点预算耗尽而中止。"""


class _SearchContext:
    __slots__ = ("deadline", "nodes", "node_budget")

    def __init__(self, deadline: float, node_budget: int) -> None:
        self.deadline = deadline
        self.nodes = 0
        self.node_budget = node_budget


# ---------------------------------------------------------------------------
# 评估 & 走法排序
# ---------------------------------------------------------------------------

def _evaluate(board) -> float:
    """静态评估：白方视角分数，返回轮到走子一方的视角分数。"""
    score = 0
    for square, piece in board.piece_map().items():
        piece_type = piece.piece_type
        value = _PIECE_VALUES[piece_type]
        table = _PST[piece_type]
        if piece.color == chess.WHITE:
            score += value + table[square ^ 56]
        else:
            score -= value + table[square]
    return score if board.turn == chess.WHITE else -score


def _move_order_score(board, move) -> int:
    """走法排序分值：升变、吃子（MVV-LVA）优先，其余按 UCI 字符串稳定排序。"""
    score = 0
    if move.promotion:
        score += _PIECE_VALUES[QUEEN] + _PIECE_VALUES.get(move.promotion, 0)
    if board.is_capture(move):
        victim = board.piece_at(move.to_square)
        if victim is None:  # 吃过路兵
            victim_value = _PIECE_VALUES[PAWN]
        else:
            victim_value = _PIECE_VALUES[victim.piece_type]
        attacker = board.piece_at(move.from_square)
        attacker_value = _PIECE_VALUES[attacker.piece_type] if attacker else 0
        score += 10 * victim_value - attacker_value
    return score


def _ordered_moves(board) -> list:
    """确定性地生成并按顺序返回合法走法列表。"""
    scored = [
        (_move_order_score(board, move), move.uci(), move)
        for move in board.legal_moves
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored]


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def _leaf_score(board, ply: int) -> float:
    """depth == 0 的叶子节点：在将军时检测将死，否则静态评估。"""
    if board.is_check() and not any(board.legal_moves):
        return -_MATE + ply
    return _evaluate(board)


def _negamax(board, depth: int, alpha: float, beta: float, ply: int, ctx: _SearchContext) -> float:
    ctx.nodes += 1
    if ctx.nodes >= ctx.node_budget:
        raise _SearchAbort
    if (ctx.nodes & _TIME_CHECK_MASK) == 0 and time.monotonic() >= ctx.deadline:
        raise _SearchAbort

    if depth <= 0:
        return _leaf_score(board, ply)

    moves = _ordered_moves(board)
    if not moves:
        # 无合法走法：被将死，或逼和。
        return -_MATE + ply if board.is_check() else 0

    best = -_MATE - 1
    for move in moves:
        board.push(move)
        try:
            score = -_negamax(board, depth - 1, -beta, -alpha, ply + 1, ctx)
        finally:
            board.pop()
        if score > best:
            best = score
        if best > alpha:
            alpha = best
        if alpha >= beta:  # beta 剪枝
            break
    return best


def _search_root(board, depth: int, ctx: _SearchContext):
    """根节点搜索；返回 (best_move, score)。超时/节点耗尽抛 _SearchAbort。"""
    moves = _ordered_moves(board)
    best_move = moves[0]
    best_score = -_MATE - 1
    alpha = -_MATE - 1
    beta = _MATE + 1
    for move in moves:
        board.push(move)
        try:
            score = -_negamax(board, depth - 1, -beta, -alpha, 1, ctx)
        finally:
            board.pop()
        if score > best_score:
            best_score = score
            best_move = move
        if score > alpha:
            alpha = score
    return best_move, best_score


def _search_root_ranking(board, depth: int, ctx: _SearchContext):
    """根节点每个合法走法的完整得分（Elo 模式用于挑选“次优走法”）。

    Args:
        board: 当前局面（调用方负责 push/pop 平衡，本函数内部完成）。
        depth: 搜索深度。
        ctx: 搜索上下文（时间/节点限制）。

    Returns:
        ``[(move, score), ...]``，按分数降序、UCI 升序稳定排序。

    Raises:
        _SearchAbort: 超时或节点预算耗尽。
    """
    scored = []
    for move in _ordered_moves(board):
        board.push(move)
        try:
            score = -_negamax(board, depth - 1, -_MATE - 1, _MATE + 1, 1, ctx)
        finally:
            board.pop()
        scored.append((move, score))
    scored.sort(key=lambda item: (-item[1], item[0].uci()))
    return scored


# ---------------------------------------------------------------------------
# Elo 难度档位（近似） 
# ---------------------------------------------------------------------------

#: 支持的最低 / 最高近似 Elo；超出的值会被裁剪到区间内。
ELO_MIN = 600
ELO_MAX = 2400

#: 默认 Elo（0 = 关闭 Elo 模式，沿用 depth/time_limit）。
ELO_OFF = 0


def clamp_elo(elo: int | None) -> int:
    """把任意输入裁剪为 ``0``（关闭）或 ``ELO_MIN..ELO_MAX`` 之间的整数。

    Args:
        elo: 用户输入，可能是 None、字符串数字或越界值。

    Returns:
        0 表示关闭 Elo 模式；否则返回裁剪后的近似 Elo 值。
    """
    if elo is None:
        return ELO_OFF
    try:
        value = int(round(float(elo)))
    except (TypeError, ValueError):
        return ELO_OFF
    if value <= 0:
        return ELO_OFF
    return max(ELO_MIN, min(value, ELO_MAX))


def elo_profile(elo: int | None) -> dict:
    """按近似 Elo 推导 AI 的搜索强度参数。

    越高的 Elo：搜索越深、思考越久、可选走法与最佳走法分差窗口越小、
    随机失误率越低。具体数值是可调的近似模型（非严格等级分换算）。

    Args:
        elo: 600-2400 的近似 Elo；0/None 时返回关闭档位。

    Returns:
        包含 ``elo`` / ``depth`` / ``time_limit`` / ``window_cp`` /
        ``blunder_prob`` 的字典。关闭 Elo 时 ``elo == 0`` 且其余字段为 None。
    """
    rating = clamp_elo(elo)
    if rating == 0:
        return {
            "elo": 0,
            "depth": None,
            "time_limit": None,
            "window_cp": None,
            "blunder_prob": None,
        }
    span = ELO_MAX - ELO_MIN
    ratio = (rating - ELO_MIN) / span  # 0.0 (600) .. 1.0 (2400)
    if rating <= 800:
        depth = 1
    elif rating <= 1150:
        depth = 2
    elif rating <= 1500:
        depth = 3
    elif rating <= 1900:
        depth = 4
    else:
        depth = 5
    time_limit = round(0.4 + ratio * 1.6, 2)
    window_cp = int(round(1400 * (1.0 - ratio) ** 2))
    blunder_prob = round(0.35 * (1.0 - ratio) ** 2, 4)
    return {
        "elo": rating,
        "depth": depth,
        "time_limit": time_limit,
        "window_cp": window_cp,
        "blunder_prob": blunder_prob,
    }


def _elo_choose(ranking, legal_moves, profile: dict, seed: str):
    """在根节点候选中按 Elo 档位挑选一步（含随机失误）。

    Args:
        ranking: ``[(move, score), ...]``，按分数从高到低排序。
        legal_moves: 当前全部合法走法（用于随机失误）。
        profile: :func:`elo_profile` 的结果。
        seed: 确定性随机种子（同一局面 + Elo 得到相同选择）。

    Returns:
        选中的 ``chess.Move``；没有候选时返回 ``None``。
    """
    if not ranking:
        return None
    # Never throw away an immediate mate: weak settings should still convert it.
    if ranking[0][1] >= _MATE - 64:
        return ranking[0][0]
    rng = random.Random(seed)
    blunder_prob = float(profile.get("blunder_prob") or 0.0)
    if blunder_prob > 0 and legal_moves and rng.random() < blunder_prob:
        return rng.choice(legal_moves)
    window = int(profile.get("window_cp") or 0)
    best_score = ranking[0][1]
    candidates = [move for move, score in ranking if best_score - score <= window]
    if not candidates:
        candidates = [ranking[0][0]]
    return rng.choice(candidates)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def choose_move(
    fen: str,
    depth: int = 3,
    time_limit: float = 2.0,
    elo: int | None = None,
) -> str | None:
    """为给定 FEN 选择一步走法，返回 UCI 字符串；无法选择时返回 ``None``。

    参数:
        fen: 标准 FEN 字符串（仅走子方需要合法走法）。
        depth: 迭代加深的最大深度（默认 3，内部限制在 1..8）。Elo 模式下忽略。
        time_limit: 搜索时间上限（秒，默认 2.0）。Elo 模式下忽略。
        elo: 近似 Elo 难度（600-2400）；``None`` / ``0`` 表示关闭 Elo 模式，
            沿用 ``depth`` / ``time_limit``。开启后由 :func:`elo_profile`
            决定深度、思考时间、可选走法分差窗口与随机失误率。

    返回:
        UCI 走法字符串，如 ``"e2e4"`` / ``"b1c3"`` / ``"e7e8q"``；FEN 非法、
        局面已终结或没有合法走法时返回 ``None``。

    异常:
        RuntimeError: 缺少 python-chess 依赖（``AVAILABLE is False``）时抛出，
            消息为中文，调用方可直接展示给用户。
    """
    if not AVAILABLE or chess is None:
        raise RuntimeError(
            "缺少 python-chess 依赖，无法使用国际象棋 AI。"
            "请先安装: python -m pip install python-chess"
        )

    if not isinstance(fen, str) or not fen.strip():
        return None

    try:
        board = chess.Board(fen)
    except Exception:
        return None

    try:
        if not board.is_valid():
            return None
        if board.is_game_over():
            return None
        legal = list(board.legal_moves)
    except Exception:
        return None
    if not legal:
        return None

    # 兜底走法：确定性的 UCI 字典序最小者（若第一次迭代都来不及完成）。
    fallback = min(legal, key=lambda m: m.uci())

    profile = elo_profile(elo)
    elo_mode = profile["elo"] > 0
    if elo_mode:
        max_depth = int(profile["depth"])
        limit = float(profile["time_limit"])
        try:
            user_limit = float(time_limit)
        except (TypeError, ValueError):
            user_limit = 0.0
        if user_limit > 0:
            limit = min(limit, user_limit)
    else:
        try:
            max_depth = int(depth)
        except (TypeError, ValueError):
            max_depth = 3
        max_depth = max(1, min(max_depth, _MAX_DEPTH))

        try:
            limit = float(time_limit)
        except (TypeError, ValueError):
            limit = 2.0
        if limit != limit or limit <= 0:  # NaN 或非正数：不做搜索，直接兜底
            limit = 0.0

    deadline = time.monotonic() + limit
    ctx = _SearchContext(deadline, _MAX_NODES)
    best = fallback
    ranking = []

    for current_depth in range(1, max_depth + 1):
        if time.monotonic() >= deadline:
            break
        try:
            if elo_mode:
                ranking = _search_root_ranking(board, current_depth, ctx)
                move, score = ranking[0]
            else:
                move, score = _search_root(board, current_depth, ctx)
        except _SearchAbort:
            break
        best = move
        # 已找到（当前深度内）的将死，继续加深没有意义。
        if score >= _MATE - current_depth:
            break

    if elo_mode and ranking:
        seed = f"{fen}|{profile['elo']}|{max_depth}"
        picked = _elo_choose(ranking, legal, profile, seed)
        if picked is not None:
            return picked.uci()

    return best.uci()
