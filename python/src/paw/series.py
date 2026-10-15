"""Finding series that describe a sorted list of positions.

A series is a tuple:
    (ARITH, start, step, count)                      start + i*step
    (NESTED, start, width, stride, rows, inner)      start + r*stride + j*inner

Selection is greedy: generate candidate series, then repeatedly take the one that saves the
most bytes. When a candidate overlaps positions already taken, it is split into the pieces
that are still free and re-queued ("lazy greedy").

A series' *shape* is everything but its start: (type, *parameters). The map body remembers
the shapes a block used recently, and a series that repeats one is stored as a short index
plus its start - so the selection treats such series as cheap.
"""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Callable, Iterator, Sequence

from .gaps import estimate_bytes, float_sum, gap_class, position_weights, to_gaps

ARITH = 0
NESTED = 1

Series = tuple[int, ...]  # (type, start, *parameters)
Shape = tuple[int, ...]  # (type, *parameters): a series without its start
Priority = Callable[[Series, float], tuple[bool | float, ...]]

MIN_CHAIN = 3  # shortest arithmetic chain worth considering
TOP_STEPS = 24  # candidate steps tried per symbol
TOP_STRIDES = 8  # candidate strides / inner steps for nested loops
NEIGHBOURS = 4  # differences to the next N positions feed the step histogram


def series_positions(s: Series) -> Sequence[int]:
    if s[0] == ARITH:
        _, start, step, count = s
        return range(start, start + step * count, step)
    _, start, width, stride, rows, inner = s
    return [start + r * stride + j * inner for r in range(rows) for j in range(width)]


def series_size(s: Series) -> int:
    return len(series_positions(s)) if s[0] == ARITH else s[2] * s[4]


def shape_of(s: Series) -> Shape:
    return (s[0], *s[2:])


def _number_bits(value: int) -> int:
    """Rough cost of one coded number: about 3 bits for its size class (the adaptive
    models learn the common ones) plus its extra bits, which are stored as they are."""
    return 3 + gap_class(value)[1]


def series_cost(s: Series) -> float:
    """Estimated bytes to store a series with a new shape: type, start and parameters."""
    return (1 + sum(_number_bits(v) for v in s[1:])) / 8


def reused_cost(s: Series) -> float:
    """Estimated bytes to store a series whose shape is remembered: an index of about two
    bits, and the start."""
    return (2 + _number_bits(s[1])) / 8


def _cost(s: Series, cheap: frozenset[Shape]) -> float:
    """Estimated bytes to store a series, cheaper if its shape is in ``cheap``."""
    return reused_cost(s) if shape_of(s) in cheap else series_cost(s)


def last_position(s: Series) -> int:
    if s[0] == ARITH:
        _, start, step, count = s
        return start + step * (count - 1)
    _, start, width, stride, rows, inner = s
    return start + stride * (rows - 1) + inner * (width - 1)


# --- candidate generation -----------------------------------------------------------


def _top_diffs(values: Sequence[int], limit: int, exclude: tuple[int, ...] = ()) -> list[int]:
    diffs: Counter[int] = Counter()
    n = len(values)
    for j in range(1, NEIGHBOURS + 1):
        for i in range(n - j):
            diffs[values[i + j] - values[i]] += 1
    picked = [d for d, c in diffs.most_common(limit + len(exclude)) if c >= 2 and d not in exclude]
    return picked[:limit]


def _chains(values: Sequence[int], value_set: set[int], step: int, min_len: int) -> Iterator[tuple[int, int]]:
    """Maximal chains v, v+step, v+2*step, ... inside value_set."""
    for v in values:
        if v - step in value_set:
            continue
        n = 1
        while v + n * step in value_set:
            n += 1
        if n >= min_len:
            yield v, n


def _candidates(pos: Sequence[int], pos_set: set[int], bwt_output: bool) -> Iterator[Series]:
    linked, linked_set = pos, pos_set  # what chains with a step above 1 may link
    if bwt_output:  # only the first positions of runs (see select_series)
        linked = [p for p in pos if p - 1 not in pos_set]
        linked_set = set(linked)
    steps = [1, *_top_diffs(pos, TOP_STEPS, exclude=(1,))]
    for step in steps:
        values, members = (pos, pos_set) if step == 1 else (linked, linked_set)
        for v, n in _chains(values, members, step, MIN_CHAIN):
            yield (ARITH, v, step, n)

    for inner in steps[: TOP_STRIDES + 1]:
        values, members = (pos, pos_set) if inner == 1 else (linked, linked_set)
        by_width: dict[int, list[int]] = {}
        for v, n in _chains(values, members, inner, 2):
            by_width.setdefault(n, []).append(v)
        for width, starts in by_width.items():
            if len(starts) < 2:
                continue
            start_set = set(starts)
            for stride in _top_diffs(starts, TOP_STRIDES):
                if stride <= (width - 1) * inner:
                    continue
                for v, rows in _chains(starts, start_set, stride, 2):
                    yield (NESTED, v, width, stride, rows, inner)


def _free_pieces(s: Series, free: set[int]) -> list[Series]:
    """The parts of series s that only use positions still in `free`."""
    pieces: list[Series] = []
    if s[0] == ARITH:
        _, _, step, _ = s
        run = []
        for p in [*series_positions(s), None]:
            if p is not None and p in free:
                run.append(p)
                continue
            if len(run) >= MIN_CHAIN:
                pieces.append((ARITH, run[0], step, len(run)))
            run = []
        return pieces

    _, start, width, stride, rows, inner = s
    group = []
    for r in range(rows + 1):
        if r < rows and all(start + r * stride + j * inner in free for j in range(width)):
            group.append(r)
            continue
        if len(group) >= 2:
            pieces.append((NESTED, start + group[0] * stride, width, stride, len(group), inner))
        elif len(group) == 1 and width >= MIN_CHAIN:
            pieces.append((ARITH, start + group[0] * stride, inner, width))
        group = []
    return pieces


# --- selection ----------------------------------------------------------------------


def _estimate(series: list[Series], leftovers: Sequence[int], known: frozenset[Shape]) -> float:
    """Estimated bytes for these series and leftovers. Series are stored in order of their
    start, and a shape is cheap once it is remembered: known before, or used earlier."""
    seen = set(known)
    total = 0.0
    for s in sorted(series, key=lambda s: s[1]):
        total += reused_cost(s) if shape_of(s) in seen else series_cost(s)
        seen.add(shape_of(s))
    return total + estimate_bytes(to_gaps(leftovers))


def _is_run(s: Series) -> bool:
    return s[0] == ARITH and s[2] == 1


# Greedy passes: each orders the candidates differently; the cheapest result wins. Taking
# the biggest saving first can break a long run into useless pieces, so one pass protects
# runs by taking them first.
PRIORITIES: tuple[Priority, ...] = (
    lambda s, gain: (-gain,),
    lambda s, gain: (not _is_run(s), -gain),
)


def _greedy(
    positions: Sequence[int],
    scored: list[tuple[Series, float]],
    savings: Callable[[Series], float],
    priority: Priority,
) -> tuple[list[Series], list[int]]:
    heap = [(priority(s, gain), tie, s) for tie, (s, gain) in enumerate(scored)]
    heapq.heapify(heap)
    tie = len(heap)
    free = set(positions)
    chosen = []
    while heap:
        _, _, s = heapq.heappop(heap)
        covered = series_positions(s)
        if all(p in free for p in covered):
            chosen.append(s)
            free.difference_update(covered)
            continue
        for piece in _free_pieces(s, free):  # stale: re-score the surviving pieces
            gain = savings(piece)
            if gain > 0:
                tie += 1
                heapq.heappush(heap, (priority(piece, gain), tie, piece))
    return chosen, sorted(free)


def select_series(
    positions: Sequence[int], known: frozenset[Shape] = frozenset(), bwt_output: bool = False
) -> tuple[list[Series], list[int]]:
    """Split sorted positions into (series sorted by start, leftover positions).

    ``known`` holds the shapes the block remembers, which are cheap to use again. A second
    round also treats the shapes picked in the first one as cheap, because a byte value
    often repeats its own shape (the same rectangle in several places).

    ``bwt_output`` says the positions come from BWT output itself (modes 5 and 6), which is
    made of runs. Chains with a step above 1 then only link the first positions of runs:
    inside a run, the run's own series describes it better, and chains through runs are
    mostly chance there - millions of candidates that slow the search and seldom help. In
    other data such chains can be real structure (records with runs in them), and after
    MTF the runs are gone, so there they stay.
    """
    if len(positions) < MIN_CHAIN:
        return [], list(positions)

    weight = position_weights(positions)
    position_set = set(positions)

    # The savings are estimates; keep series only if they beat plain leftovers overall.
    best: tuple[list[Series], list[int]] = ([], list(positions))
    best_cost = _estimate([], positions, known)
    # The candidates and the weight they cover are the same in both rounds. Keep those that
    # could save something with a remembered shape, the cheapest a series can be.
    weighed = [
        (s, covered)
        for s in _candidates(positions, position_set, bwt_output)
        if (covered := float_sum(weight[p] for p in series_positions(s))) - reused_cost(s) > 0
    ]
    cheap = known
    for _ in range(2):

        def savings(s: Series, cheap: frozenset[Shape] = cheap) -> float:
            return float_sum(weight[p] for p in series_positions(s)) - _cost(s, cheap)

        scored = [(s, gain) for s, covered in weighed if (gain := covered - _cost(s, cheap)) > 0]
        for priority in PRIORITIES:
            chosen, leftovers = _greedy(positions, scored, savings, priority)
            cost = _estimate(chosen, leftovers, known)
            if chosen and cost < best_cost:
                best, best_cost = (sorted(chosen, key=lambda s: s[1]), leftovers), cost
        more = cheap | {shape_of(s) for s in best[0]}
        if more == cheap:  # nothing new to treat as cheap: a second round would repeat this one
            break
        cheap = more
    return best
