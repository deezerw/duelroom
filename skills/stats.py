"""Exact opening-hand probability and interval estimates.

Pure stdlib -- `math.comb` gives exact integer hypergeometric math, so there
is no reason to pull in scipy. Anything answerable here should never be
simulated: these are exact and instant, and the engine's time is better spent
on questions about lines.
"""

import math
from itertools import product

STANDARD_HAND = 5  # going first
DRAW_HAND = 6      # going second


def pmf(N, K, n, k):
    """P(exactly k successes) drawing n from N with K successes available."""
    if k > K or k > n or (n - k) > (N - K) or k < 0:
        return 0.0
    return math.comb(K, k) * math.comb(N - K, n - k) / math.comb(N, n)


def at_least(N, K, n, k):
    """P(at least k successes)."""
    if k <= 0:
        return 1.0
    return sum(pmf(N, K, n, i) for i in range(k, min(K, n) + 1))


def at_most(N, K, n, k):
    return sum(pmf(N, K, n, i) for i in range(0, min(k, K, n) + 1))


def open_chance(deck_size, copies, hand_size=STANDARD_HAND, want=1):
    """P(opening at least `want` copies of a card run at `copies`)."""
    return at_least(deck_size, copies, hand_size, want)


def group_chance(deck_size, group_size, hand_size=STANDARD_HAND, want=1):
    """P(opening at least `want` of an interchangeable group.

    `group_size` is the total copies across the group -- e.g. 3 Ash + 3 Imperm
    + 2 Veiler is a group of 8.
    """
    return at_least(deck_size, group_size, hand_size, want)


def combo_chance(deck_size, requirements, hand_size=STANDARD_HAND):
    """P(opening at least r of every group simultaneously.

    `requirements` is [(group_size, needed), ...] over *disjoint* groups.
    Exact multivariate hypergeometric by enumeration -- fine for the handful
    of groups a real question ever involves.

        combo_chance(40, [(9, 1), (12, 1)])  # a starter AND a handtrap
    """
    sizes = [g for g, _ in requirements]
    needs = [r for _, r in requirements]
    rest = deck_size - sum(sizes)
    if rest < 0:
        raise ValueError("group sizes exceed deck size (groups must be disjoint)")
    if sum(needs) > hand_size:
        return 0.0

    total = math.comb(deck_size, hand_size)
    acc = 0
    ranges = [range(need, min(size, hand_size) + 1) for size, need in zip(sizes, needs)]
    for draw in product(*ranges):
        spare = hand_size - sum(draw)
        if spare < 0 or spare > rest:
            continue
        ways = math.comb(rest, spare)
        for size, d in zip(sizes, draw):
            ways *= math.comb(size, d)
        acc += ways
    return acc / total


def brick_chance(deck_size, starters, hand_size=STANDARD_HAND):
    """P(opening zero starters) -- the floor on your brick rate.

    This is a lower bound, not the real brick rate: a hand can hold a starter
    and still have no line. Stage 3 measures the real number; this one is
    free and tells you whether the deck can possibly be consistent.
    """
    return pmf(deck_size, starters, hand_size, 0)


def wilson(successes, trials, z=1.96):
    """Wilson score interval. Correct at small n and near 0/1, unlike normal.

    Returns (point, low, high).
    """
    if trials == 0:
        return (0.0, 0.0, 1.0)
    if successes > trials:
        # Swapped arguments reach the sqrt with p > 1 and die on a "math
        # domain error", which says nothing about the cause. Verified
        # 2026-10-24 that the interval itself is correct -- 40/40 gives
        # 0.912-1.000, 15/20 gives 0.531-0.888, 10/12 gives 0.552-0.953 --
        # so the only soft edge left was unreadable input handling.
        raise ValueError(
            f"wilson({successes}, {trials}): more successes than trials "
            f"— arguments are (successes, trials) in that order")
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def trials_needed(effect, baseline=0.5, z=1.96, power=0.84):
    """Rough trials per arm to resolve an absolute `effect` difference.

    With paired seeds (common random numbers) the real requirement is far
    lower -- this is the unpaired worst case, and the gap is the argument for
    always pairing.
    """
    var = baseline * (1 - baseline)
    return math.ceil(2 * ((z + power) ** 2) * var / (effect ** 2))


def crossed_bootstrap(cells, reps=2000, seed=0, level=0.95, bounds=(0.0, 1.0)):
    """Interval for a pooled rate over a crossed design: `cells` maps
    (hand, enemy_hand) -> list of 0/1 outcomes.

    Games are not independent: every game of one hand shares its strength,
    and every game against one enemy hand shares theirs, so a Wilson
    interval over the pooled games is far too narrow. This resamples hands
    and enemy hands independently (with replacement) and recomputes the
    pooled rate -- the standard bootstrap for a two-way random design,
    slightly conservative. Values may be any numbers (a paired difference
    per cell, say); `bounds` is the "know nothing" interval returned when
    there is too little to resample. Returns (point, low, high).
    """
    import random

    cells = {k: v for k, v in cells.items() if v}
    if not cells:
        return (0.0, *bounds)
    hands = sorted({h for h, _e in cells})
    ehands = sorted({e for _h, e in cells})
    sums = {k: (sum(v), len(v)) for k, v in cells.items()}
    point = sum(s for s, _n in sums.values()) / sum(n for _s, n in sums.values())
    if len(hands) == len(ehands) == 1:
        return (point, *bounds)    # one pairing: its replays say nothing about others
    rng = random.Random(seed)
    rates = []
    for _ in range(reps):
        hs = [rng.choice(hands) for _ in hands]
        es = [rng.choice(ehands) for _ in ehands]
        w = n = 0
        for h in hs:
            for e in es:
                c = sums.get((h, e))
                if c:
                    w += c[0]
                    n += c[1]
        if n:
            rates.append(w / n)
    rates.sort()
    lo = rates[int((1 - level) / 2 * len(rates))]
    hi = rates[min(len(rates) - 1, int((1 + level) / 2 * len(rates)))]
    return (point, lo, hi)
