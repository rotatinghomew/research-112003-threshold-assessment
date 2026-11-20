"""只读数据装载与 as-of 时点视图。

输入表：
  rules / orders / receipts / outcomes / score_reviews / plans
输出是不可变语义的记录对象（dataclass），以及在某个审议日 asof 下
"当时能看到什么"的快照。修订记录按 revision 取 <= asof 的最大版本；
没有任何已知记录时该订单对应字段为 None，由调用方显式处理。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    version: str
    cutoff: int
    scale: str
    live_from: int
    live_until: int


@dataclass(frozen=True)
class Order:
    order: str
    merchant: str
    version: str
    day: int
    raw_score: int
    price_band: int
    first_purchase: int


@dataclass(frozen=True)
class Receipt:
    order: str
    revision: int
    known: int
    version: str
    shown: int
    reason: str


@dataclass(frozen=True)
class Outcome:
    order: str
    revision: int
    known: int
    followup_days: int
    state: str


@dataclass(frozen=True)
class ScoreReview:
    order: str
    known: int
    updated_score: int


@dataclass(frozen=True)
class Plan:
    plan: str
    asof: int
    version: str
    cutoff: int
    bandwidths: tuple[int, ...]
    min_per_side: int
    max_unknown: float
    min_gain: float
    min_jump: float
    horizon: int
    active_on: int
    status: str


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass(frozen=True)
class Warehouse:
    rules: tuple[Rule, ...]
    orders: tuple[Order, ...]
    receipts: tuple[Receipt, ...]
    outcomes: tuple[Outcome, ...]
    reviews: tuple[ScoreReview, ...]
    plans: tuple[Plan, ...]

    def rule(self, version: str) -> Rule:
        for rule in self.rules:
            if rule.version == version:
                return rule
        raise KeyError(f"unknown rule version {version!r}")

    def order(self, order_id: str) -> Order:
        return self._order_index[order_id]

    def __post_init__(self) -> None:
        # frozen dataclass 上用 object.__setattr__ 建索引
        object.__setattr__(self, "_order_index", {o.order: o for o in self.orders})

    def asof_view(self, asof: int) -> "AsofView":
        return AsofView.build(self, asof)


def load(data_dir: str | Path) -> Warehouse:
    data_dir = Path(data_dir)
    rules = tuple(
        Rule(r["version"], int(r["cutoff"]), r["scale"],
             int(r["live_from"]), int(r["live_until"]))
        for r in _read_csv(data_dir / "rules.csv")
    )
    orders = tuple(
        Order(r["order"], r["merchant"], r["version"], int(r["day"]),
              int(r["raw_score"]), int(r["price_band"]), int(r["first_purchase"]))
        for r in _read_csv(data_dir / "orders.csv")
    )
    receipts = tuple(
        Receipt(r["order"], int(r["revision"]), int(r["known"]),
                r["version"], int(r["shown"]), r["reason"])
        for r in _read_csv(data_dir / "receipts.csv")
    )
    outcomes = tuple(
        Outcome(r["order"], int(r["revision"]), int(r["known"]),
                int(r["followup_days"]), r["state"])
        for r in _read_csv(data_dir / "outcomes.csv")
    )
    reviews = tuple(
        ScoreReview(r["order"], int(r["known"]), int(r["updated_score"]))
        for r in _read_csv(data_dir / "score_reviews.csv")
    )
    plans = tuple(
        Plan(r["plan"], int(r["asof"]), r["version"], int(r["cutoff"]),
             tuple(int(x) for x in r["bandwidths"].split("|")),
             int(r["min_per_side"]), float(r["max_unknown"]),
             float(r["min_gain"]), float(r["min_jump"]), int(r["horizon"]),
             int(r["active_on"]), r["status"])
        for r in _read_csv(data_dir / "plans.csv")
    )
    return Warehouse(rules, orders, receipts, outcomes, reviews, plans)


@dataclass(frozen=True)
class AsofView:
    """审议日 asof 当天的信息快照。

    receipt_of / outcome_of 返回 asof 当天最新的已知修订；尚无记录返回 None。
    receipt_versions_ok 标记订单当时有效回执的版本标注是否与下单版本一致，
    版本不符不等同于没有展示，但必须让审议流程看见并单独说明。
    """

    asof: int
    receipt_of: dict[str, Receipt]
    outcome_of: dict[str, Outcome]
    review_of: dict[str, ScoreReview]
    mismatched_receipts: tuple[str, ...]

    @staticmethod
    def _latest(rows, asof: int) -> dict:
        latest: dict[str, object] = {}
        for row in rows:
            if row.known <= asof:
                old = latest.get(row.order)
                if old is None or row.revision > old.revision:
                    latest[row.order] = row
        return latest

    @classmethod
    def build(cls, wh: Warehouse, asof: int) -> "AsofView":
        receipt_of = cls._latest(wh.receipts, asof)
        outcome_of = cls._latest(wh.outcomes, asof)
        review_of = {r.order: r for r in wh.reviews if r.known <= asof}
        mismatched = tuple(
            sorted(
                oid
                for oid, rec in receipt_of.items()
                if rec.version != wh.order(oid).version
            )
        )
        return cls(asof, receipt_of, outcome_of, review_of, mismatched)
