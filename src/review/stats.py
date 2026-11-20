"""局部比较所需的统计量，全部仅用标准库实现。

包含：
* 二分组差值与按 merchant 聚类的 Liang–Zeger 稳健标准误；
* 可选的局部线性（分数一次项 + 分组交互）拟合，作为带宽敏感性的旁证；
* 正态近似的双侧 p 值与置信区间；
* 分数扎堆（heaping）与展示率跳变等 RDD 诊断指标。

不引入 numpy/pandas，保证审计环境可直接复算。
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return 2.0 * (1.0 - _normal_cdf(abs(z)))


@dataclass(frozen=True)
class GroupStats:
    n: int
    mean: float
    unknown: int          # 该组内售后未结（非 clear/dispute）订单数
    unknown_rate: float
    shown: int            # 有效回执 shown=1 的订单数
    show_rate: float
    clusters: int


@dataclass(frozen=True)
class Comparison:
    bandwidth: int
    above: GroupStats     # 规则合格侧 score >= cutoff
    below: GroupStats     # 规则不合格侧 score < cutoff
    diff: float           # above 标签率 - below 标签率（仅已贴标签订单）
    cluster_se: float
    z: float
    p_value: float
    ci_low: float
    ci_high: float
    n_clusters: int
    show_jump: float      # above 展示率 - below 展示率
    # 实际展示口径的描述性差异（非因果）：
    shown_group: GroupStats
    notshown_group: GroupStats
    observed_gap: float   # shown 争议率 - notshown 争议率，仅描述


def _group_stats(orders, labels, receipt_of) -> GroupStats:
    n = len(orders)
    if n == 0:
        return GroupStats(0, float("nan"), 0, float("nan"), 0, float("nan"), 0)
    labeled = [o for o in orders if labels.get(o.order_id) in ("clear", "dispute")]
    unknown = n - len(labeled)
    mean = (
        sum(1.0 for o in labeled if labels.get(o.order_id) == "dispute") / len(labeled)
        if labeled else float("nan")
    )
    shown = sum(
        1 for o in orders
        if (rec := receipt_of.get(o.order_id)) is not None and rec.shown == 1
    )
    return GroupStats(
        n=n,
        mean=mean,
        unknown=unknown,
        unknown_rate=unknown / n,
        shown=shown,
        show_rate=shown / n,
        clusters=len({o.merchant for o in orders}),
    )


def _cluster_robust_diff(orders, labels, receipt_of):
    """返回 (差值, 聚类稳健SE, 聚类数)；标签率只使用 clear/dispute 订单。"""
    labeled = [o for o in orders if labels.get(o.order_id) in ("clear", "dispute")]
    ys = [1.0 if labels.get(o.order_id) == "dispute" else 0.0 for o in labeled]
    ts = [1 if o.eligible else 0 for o in labeled]
    clusters = [o.merchant for o in labeled]
    n, k = len(labeled), 2
    if n < 3 or len(set(ts)) < 2:
        return float("nan"), float("nan"), len(set(clusters))

    x = [[1.0, float(t)] for t in ts]
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for i in range(n):
        for a in range(k):
            xty[a] += x[i][a] * ys[i]
            for b in range(k):
                xtx[a][b] += x[i][a] * x[i][b]
    det = xtx[0][0] * xtx[1][1] - xtx[0][1] * xtx[1][0]
    if det == 0:
        return float("nan"), float("nan"), len(set(clusters))
    inv = [
        [xtx[1][1] / det, -xtx[0][1] / det],
        [-xtx[1][0] / det, xtx[0][0] / det],
    ]
    beta = [sum(inv[a][b] * xty[b] for b in range(k)) for a in range(k)]
    resid = [ys[i] - beta[0] - beta[1] * ts[i] for i in range(n)]

    meat = [[0.0] * k for _ in range(k)]
    by_cluster = defaultdict(list)
    for i, g in enumerate(clusters):
        by_cluster[g].append(i)
    for idxs in by_cluster.values():
        score = [0.0] * k
        for i in idxs:
            for a in range(k):
                score[a] += x[i][a] * resid[i]
        for a in range(k):
            for b in range(k):
                meat[a][b] += score[a] * score[b]
    g = len(by_cluster)
    if g > 1:  # 有限样本常用的 G/(G-1) 校正
        meat = [[m * g / (g - 1) for m in row] for row in meat]
    cov = [
        [
            sum(inv[a][c] * meat[c][d] * inv[b][d] for c in range(k) for d in range(k))
            for b in range(k)
        ]
        for a in range(k)
    ]
    se = math.sqrt(max(cov[1][1], 0.0))
    return beta[1], se, g


def local_comparison(window_orders, labels, receipt_of, bandwidth: int) -> Comparison:
    """window_orders 为已限定在某带宽内、带 eligible 标记的订单。"""
    above = [o for o in window_orders if o.eligible]
    below = [o for o in window_orders if not o.eligible]
    gs_above = _group_stats(above, labels, receipt_of)
    gs_below = _group_stats(below, labels, receipt_of)

    diff, se, g = _cluster_robust_diff(window_orders, labels, receipt_of)
    if math.isnan(se):
        z = p = ci_lo = ci_hi = float("nan")
    else:
        z = diff / se
        p = two_sided_p(z)
        ci_lo, ci_hi = diff - 1.96 * se, diff + 1.96 * se

    shown_orders = [
        o for o in window_orders
        if (rec := receipt_of.get(o.order_id)) is not None and rec.shown == 1
        and labels.get(o.order_id) in ("clear", "dispute")
    ]
    notshown_orders = [
        o for o in window_orders
        if (rec := receipt_of.get(o.order_id)) is not None and rec.shown == 0
        and labels.get(o.order_id) in ("clear", "dispute")
    ]
    # 描述性口径允许出现"无回执"订单：它们归入 notshown 之外单独不计
    gs_shown = _group_stats(shown_orders, labels, receipt_of)
    gs_notshown = _group_stats(notshown_orders, labels, receipt_of)
    gap = (
        gs_shown.mean - gs_notshown.mean
        if not math.isnan(gs_shown.mean) and not math.isnan(gs_notshown.mean)
        else float("nan")
    )
    return Comparison(
        bandwidth=bandwidth,
        above=gs_above,
        below=gs_below,
        diff=diff,
        cluster_se=se,
        z=z,
        p_value=p,
        ci_low=ci_lo,
        ci_high=ci_hi,
        n_clusters=g,
        show_jump=gs_above.show_rate - gs_below.show_rate,
        shown_group=gs_shown,
        notshown_group=gs_notshown,
        observed_gap=gap,
    )


@dataclass(frozen=True)
class Heaping:
    cutoff: int
    score_counts: dict[int, int]
    near_below: int
    near_above: int
    ratio: float
    even_only: bool


def heaping_diagnostic(orders, cutoff: int, nearest: int = 2) -> Heaping:
    counts = Counter(o.raw_score for o in orders)
    lo = sum(counts.get(s, 0) for s in range(cutoff - nearest, cutoff))
    hi = sum(counts.get(s, 0) for s in range(cutoff, cutoff + nearest))
    ratio = hi / lo if lo else float("inf")
    even_only = all(s % 2 == 0 for s in counts) and len(counts) > 0
    return Heaping(
        cutoff=cutoff,
        score_counts=dict(sorted(counts.items())),
        near_below=lo,
        near_above=hi,
        ratio=ratio,
        even_only=even_only,
    )


def covariate_means(window_orders) -> dict[str, tuple[float, float]]:
    """返回每个下单前特征在 (合格侧均值, 不合格侧均值)。"""
    out = {}
    for cov in ("price_band", "first_purchase"):
        above = [float(getattr(o, cov)) for o in window_orders if o.eligible]
        below = [float(getattr(o, cov)) for o in window_orders if not o.eligible]
        out[cov] = (
            sum(above) / len(above) if above else float("nan"),
            sum(below) / len(below) if below else float("nan"),
        )
    return out


def merchant_overlap(window_orders) -> tuple[int, int]:
    """返回 (窗口内商家数, 同时出现在阈值两侧的商家数)。"""
    sides = defaultdict(set)
    for o in window_orders:
        sides[o.merchant].add(o.eligible)
    total = len(sides)
    both = sum(1 for s in sides.values() if len(s) == 2)
    return total, both
