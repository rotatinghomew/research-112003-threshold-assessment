"""按计划（plan）做阈值局部审议并给出可切换/退回/保留的裁决。

裁决口径（与业务约定逐条对应）：
1. 计划版本必须能在 rules 表找到同一量表；目标 cutoff 必须等于该版本规则实际 cutoff，
   否则属于"请求范围与有证据邻域无法对应"，退回，不许借显著与否扩张。
2. 订单只取计划版本、且在审议 asof 当天已成熟（day + horizon <= asof）的；
   资格只看下单时 raw_score 相对 cutoff 的位置，复核分数永不改变资格。
3. 每个候选带宽逐档过闸：每侧成熟订单数、每侧未结比例、展示率跳变、效果幅度。
   未结 = 售后状态既非 clear 也非 dispute（appeal 等），不得当无争议，也不删单。
4. 效果幅度按 ITT（规则资格）口径：gain = 不合格侧争议率 - 合格侧争议率，
   仅在已贴标签订单上估计，差值检验按 merchant 聚类稳健 SE。
5. 实际展示差异只作描述性对照输出，不参与因果裁决。
6. 多带宽结论不一致（翻转）、或未知标签极端赋值会让建议翻转时，不得切换。
7. 待生效计划同时在"提交 asof 视图"与"订正后当前视图"重判；只有两个视图都支持、
   且证据订正未推翻结论时才允许切换——只依赖已失效证据的切换不能照常生效。
8. 已执行（executed）计划的旧规则动作一律保留，重算仅作证据留痕，不产生切换或回滚。
9. 每条被挡住的计划都留下"哪条闸口、用什么数字挡住"的审计记录。
"""
from __future__ import annotations

from dataclasses import dataclass

from .loader import AsofView, Order, Plan, Warehouse
from .stats import (
    Comparison,
    covariate_means,
    heaping_diagnostic,
    local_comparison,
    merchant_overlap,
)

LABELED_STATES = ("clear", "dispute")


@dataclass(frozen=True)
class WindowOrder:
    order: Order
    distance: int
    eligible: bool

    @property
    def order_id(self) -> str:
        return self.order.order

    @property
    def merchant(self) -> str:
        return self.order.merchant

    @property
    def raw_score(self) -> int:
        return self.order.raw_score

    @property
    def price_band(self) -> int:
        return self.order.price_band

    @property
    def first_purchase(self) -> int:
        return self.order.first_purchase


@dataclass(frozen=True)
class BandVerdict:
    bandwidth: int
    passed: bool                       # 数值闸口全过
    failures: tuple[str, ...]
    comparison: Comparison
    gate_values: dict[str, float | int]
    worst_case_gain: float             # 未结订单对"合格侧更优"最不利的极端赋值
    best_case_gain: float
    label_flips: bool                  # 未知标签极端赋值是否会让动作建议翻转


@dataclass(frozen=True)
class EvidenceChange:
    order: str
    table: str
    revision_before: int
    revision_after: int
    known_after: int
    description: str
    alters_estimate: bool


@dataclass(frozen=True)
class ViewVerdict:
    asof: int
    n_mature: int
    n_mature_in_window: int
    band_verdicts: tuple[BandVerdict, ...]
    numeric_passed_bands: tuple[int, ...]
    fully_passed_bands: tuple[int, ...]
    view_decision: str                 # switch | hold | return_scope
    view_reason: str
    heaping: dict
    covariate_balance_bw8: dict[str, tuple[float, float]]
    merchant_overlap: tuple[int, int]
    mismatched_receipts_visible: tuple[str, ...]
    score_reviews_excluded: tuple[str, ...]
    audit: tuple[str, ...]


@dataclass(frozen=True)
class PlanVerdict:
    plan: Plan
    final_decision: str                # retained | switch | hold | return_scope
    reason_summary: str
    scope_ok: bool
    scope_detail: dict[str, str]
    submission: ViewVerdict
    current: ViewVerdict
    evidence_corrections: tuple[EvidenceChange, ...]
    late_arrivals: tuple[EvidenceChange, ...]
    cutoff_jump_diagnostic: dict | None
    audit: tuple[str, ...]


def latest_info_day(wh: Warehouse) -> int:
    return max(
        [r.known for r in wh.receipts] + [o.known for o in wh.outcomes]
    )


def _mature_orders(wh: Warehouse, plan: Plan, asof: int) -> list[Order]:
    return [
        o for o in wh.orders
        if o.version == plan.version and o.day + plan.horizon <= asof
    ]


def _window(mature: list[Order], cutoff: int, bandwidth: int) -> list[WindowOrder]:
    out = []
    for o in mature:
        dist = abs(o.raw_score - cutoff)
        if dist <= bandwidth:
            out.append(WindowOrder(o, dist, o.raw_score >= cutoff))
    return out


def _band_gate(plan: Plan, window: list[WindowOrder], view: AsofView,
               bandwidth: int) -> BandVerdict:
    labels = {
        oid: (oc.state if oc is not None else None)
        for oid, oc in view.outcome_of.items()
    }
    comp = local_comparison(window, labels, view.receipt_of, bandwidth)
    failures: list[str] = []
    gain = (
        comp.below.mean - comp.above.mean
        if comp.above.mean == comp.above.mean and comp.below.mean == comp.below.mean
        else float("nan")
    )
    gate_values = {
        "n_above": comp.above.n,
        "n_below": comp.below.n,
        "unknown_rate_above": comp.above.unknown_rate,
        "unknown_rate_below": comp.below.unknown_rate,
        "show_rate_above": comp.above.show_rate,
        "show_rate_below": comp.below.show_rate,
        "show_jump": comp.show_jump,
        "itt_gain": gain,
        "p_value": comp.p_value if comp.p_value == comp.p_value else float("nan"),
    }

    if comp.above.n < plan.min_per_side or comp.below.n < plan.min_per_side:
        failures.append(
            f"min_per_side：每侧需 {plan.min_per_side}，实际合格侧 {comp.above.n} / "
            f"不合格侧 {comp.below.n}"
        )
    max_unknown = max(
        comp.above.unknown_rate if comp.above.n else 0.0,
        comp.below.unknown_rate if comp.below.n else 0.0,
    )
    if max_unknown > plan.max_unknown + 1e-12:
        failures.append(
            f"max_unknown：上限 {plan.max_unknown:.2f}，实际最大未结比例 {max_unknown:.3f}"
        )
    if comp.show_jump < plan.min_jump:
        failures.append(
            f"min_jump：展示率跳变需 >= {plan.min_jump:.2f}，实际 {comp.show_jump:.3f}"
        )

    if gain != gain:
        failures.append("min_gain：存在无已贴标签订单的一侧，无法估计效果")
        worst = best = float("nan")
        flips = True
    else:
        if gain < plan.min_gain:
            failures.append(
                f"min_gain：争议下降幅度需 >= {plan.min_gain:.2f}，ITT 实际 {gain:.3f}"
            )
        a, b = comp.above, comp.below
        a_lab, b_lab = a.n - a.unknown, b.n - b.unknown
        a_disp = a.mean * a_lab if a_lab else 0.0
        b_disp = b.mean * b_lab if b_lab else 0.0
        # 对"合格侧更优（gain 大）"最不利：合格侧未结全争议、不合格侧未结全无争议
        worst = (b_disp / b.n if b.n else float("nan")) - (
            (a_disp + a.unknown) / a.n if a.n else float("nan")
        )
        best = ((b_disp + b.unknown) / b.n if b.n else float("nan")) - (
            a_disp / a.n if a.n else float("nan")
        )
        flips = (worst < plan.min_gain) or (worst <= 0)

    return BandVerdict(
        bandwidth=bandwidth,
        passed=not failures,
        failures=tuple(failures),
        comparison=comp,
        gate_values=gate_values,
        worst_case_gain=worst,
        best_case_gain=best,
        label_flips=flips,
    )


def _evaluate_view(wh: Warehouse, plan: Plan, rule_cutoff: int,
                   asof: int) -> ViewVerdict:
    view = wh.asof_view(asof)
    audit: list[str] = []
    mature = _mature_orders(wh, plan, asof)

    excluded_reviews = tuple(sorted(
        oid for oid in view.review_of
        if wh.order(oid).version == plan.version
    ))
    if excluded_reviews:
        audit.append(
            "事后复核分数 " + "、".join(excluded_reviews)
            + " 仅供后续业务处置，未改变任何订单的资格侧或窗口归属"
        )
    visible_mismatch = tuple(
        oid for oid in view.mismatched_receipts
        if wh.order(oid).version == plan.version
    )
    for oid in visible_mismatch:
        rec = view.receipt_of[oid]
        audit.append(
            f"回执版本异常：{oid} 在 asof={asof} 的有效回执标注为 {rec.version}"
            f"（下单版本 {plan.version}）；按'版本不符≠无展示'保留订单，shown={rec.shown}"
        )

    band_verdicts = []
    for bw in plan.bandwidths:
        window = _window(mature, rule_cutoff, bw)
        bv = _band_gate(plan, window, view, bw)
        band_verdicts.append(bv)
        for f in bv.failures:
            audit.append(f"asof={asof} 带宽 {bw} 未过闸：{f}")
        if bv.passed and bv.label_flips:
            audit.append(
                f"asof={asof} 带宽 {bw} 数值闸口已过，但未结标签极端赋值下增益低至 "
                f"{bv.worst_case_gain:.3f}（门槛 {plan.min_gain:.2f}），未知标签可使建议翻转"
            )
        if (not bv.label_flips and bv.passed
                and bv.comparison.p_value == bv.comparison.p_value
                and bv.comparison.p_value > 0.10):
            audit.append(
                f"asof={asof} 带宽 {bw} 效果幅度达数值门槛但聚类稳健检验 p="
                f"{bv.comparison.p_value:.3f}，统计上不能区分于零，证据强度不足"
            )

    numeric_passed = tuple(bv.bandwidth for bv in band_verdicts if bv.passed)
    fully_passed = tuple(
        bv.bandwidth for bv in band_verdicts if bv.passed and not bv.label_flips
    )
    requested = list(plan.bandwidths)
    if len(fully_passed) == len(requested):
        decision, reason = "switch", f"全部候选带宽 {requested} 过闸且标签敏感性不翻转"
    elif numeric_passed and not fully_passed:
        decision = "hold"
        reason = (
            f"带宽 {list(numeric_passed)} 数值过闸但未结标签敏感性翻转，"
            "其余带宽未过闸；未知标签可使动作建议翻转，不得切换"
        )
    elif fully_passed:
        decision = "hold"
        reason = (
            f"仅带宽 {list(fully_passed)} 完全过闸，"
            f"{[b for b in requested if b not in fully_passed]} 未过闸；合理带宽结论翻转"
        )
    else:
        decision = "hold"
        reason = "没有任何候选带宽同时满足覆盖、未结、跳变、效果幅度与标签敏感性要求"

    heap_diag = heaping_diagnostic(mature, rule_cutoff) if mature else None
    heaping = (
        {
            "even_only": heap_diag.even_only,
            "near_below": heap_diag.near_below,
            "near_above": heap_diag.near_above,
            "near_ratio": (round(heap_diag.ratio, 3)
                           if heap_diag.ratio == heap_diag.ratio else None),
        }
        if heap_diag else {}
    )
    w8 = _window(mature, rule_cutoff, 8)
    balance = covariate_means(w8) if w8 else {}
    overlap = merchant_overlap(w8) if w8 else (0, 0)

    return ViewVerdict(
        asof=asof,
        n_mature=len(mature),
        n_mature_in_window=len(_window(mature, rule_cutoff, max(requested))),
        band_verdicts=tuple(band_verdicts),
        numeric_passed_bands=numeric_passed,
        fully_passed_bands=fully_passed,
        view_decision=decision,
        view_reason=reason,
        heaping=heaping,
        covariate_balance_bw8=balance,
        merchant_overlap=overlap,
        mismatched_receipts_visible=visible_mismatch,
        score_reviews_excluded=excluded_reviews,
        audit=tuple(audit),
    )


def _detect_changes(wh: Warehouse, plan: Plan, current_asof: int):
    """对比提交 asof 与当前 asof 两个视图。

    返回 (corrections, late_arrivals)：
    * corrections = 同一记录被修订（revision 增加，含首次无记录后直接出现 rev>=2 的情形），
      即"补回执/补售后订正"，是重判可用范围的直接依据；
    * late_arrivals = 提交视图之后才首次到达的 rev1 记录，属于资料各自按时到达的正常情形，
      已由成熟度窗口与 asof 视图处理，单独列示但不计为"失效证据"。
    """
    sub = wh.asof_view(plan.asof)
    cur = wh.asof_view(current_asof)
    corrections: list[EvidenceChange] = []
    late_arrivals: list[EvidenceChange] = []
    in_scope = {
        o.order for o in wh.orders
        if o.version == plan.version
        and abs(o.raw_score - plan.cutoff) <= max(plan.bandwidths)
    }
    for oid in sorted(in_scope):
        order = wh.order(oid)
        r_old, r_new = sub.receipt_of.get(oid), cur.receipt_of.get(oid)
        if r_new is not None and (r_old is None or r_new.revision > r_old.revision):
            alters = bool(
                r_old is None
                or r_old.shown != r_new.shown
                or r_old.reason != r_new.reason
            )
            old_ver = r_old.version if r_old else "无记录"
            ch = EvidenceChange(
                order=oid, table="receipts",
                revision_before=r_old.revision if r_old else 0,
                revision_after=r_new.revision, known_after=r_new.known,
                description=(
                    f"回执由 rev{r_old.revision if r_old else 0}"
                    f"（版本 {old_ver}，shown={r_old.shown if r_old else 'NA'}，"
                    f"reason={r_old.reason if r_old else 'NA'}）订正为 rev{r_new.revision}"
                    f"（版本 {r_new.version}，shown={r_new.shown}，reason={r_new.reason}）；"
                    f"订单资格侧不变（{plan.version} 分数 {order.raw_score}"
                    f"{'≥' if order.raw_score >= plan.cutoff else '<'} cutoff {plan.cutoff}）"
                ),
                alters_estimate=alters,
            )
            (corrections if r_new.revision >= 2 else late_arrivals).append(ch)
        o_old, o_new = sub.outcome_of.get(oid), cur.outcome_of.get(oid)
        if o_new is not None and (o_old is None or o_new.revision > o_old.revision):
            was_unknown = o_old is None or o_old.state not in LABELED_STATES
            now_labeled = o_new.state in LABELED_STATES
            ch = EvidenceChange(
                order=oid, table="outcomes",
                revision_before=o_old.revision if o_old else 0,
                revision_after=o_new.revision, known_after=o_new.known,
                description=(
                    f"售后由 rev{o_old.revision if o_old else 0}"
                    f"（state={o_old.state if o_old else '无记录'}）订正为 rev{o_new.revision}"
                    f"（state={o_new.state}）；该订单位于"
                    f"{'合格' if order.raw_score >= plan.cutoff else '不合格'}侧，"
                    + ("从未结转为已贴标签，未结比例与争议率均受影响"
                       if was_unknown and now_labeled
                       else "标签状态发生修订，已按最新口径重算")
                ),
                alters_estimate=was_unknown and now_labeled,
            )
            (corrections if o_new.revision >= 2 else late_arrivals).append(ch)
    return tuple(corrections), tuple(late_arrivals)


def _cutoff_jump_diagnostic(wh: Warehouse, plan: Plan, actual_cutoff: int,
                            asof: int) -> dict:
    """展示率跳变对照：请求 cutoff 与规则真实 cutoff 各自两侧的展示率（当前全量证据）。"""
    view = wh.asof_view(asof)
    mature = _mature_orders(wh, plan, asof)
    out = {"asof": asof, "cutoffs": {}}
    for cut in (plan.cutoff, actual_cutoff):
        rows = {}
        for bw in plan.bandwidths:
            win = _window(mature, cut, bw)
            above = [w for w in win if w.raw_score >= cut]
            below = [w for w in win if w.raw_score < cut]
            sr = lambda grp: (  # noqa: E731
                sum(1 for w in grp
                    if (rc := view.receipt_of.get(w.order_id)) is not None and rc.shown == 1)
                / len(grp) if grp else float("nan")
            )
            rows[bw] = {
                "n_above": len(above), "n_below": len(below),
                "show_above": sr(above), "show_below": sr(below),
                "jump": (sr(above) - sr(below)) if above and below else float("nan"),
            }
        out["cutoffs"][cut] = rows
    out["rule_cutoff"] = actual_cutoff
    out["requested_cutoff"] = plan.cutoff
    return out


def evaluate_plan(wh: Warehouse, plan: Plan) -> PlanVerdict:
    current_asof = latest_info_day(wh)
    audit: list[str] = []
    rule = next((r for r in wh.rules if r.version == plan.version), None)
    scope_detail: dict[str, str] = {}

    if rule is None:
        scope_detail["rule_scale"] = "缺失"
        audit.append(f"范围退回：规则表中不存在版本 {plan.version}，量表口径无法建立")
        empty = ViewVerdict(
            asof=plan.asof, n_mature=0, n_mature_in_window=0, band_verdicts=(),
            numeric_passed_bands=(), fully_passed_bands=(),
            view_decision="return_scope", view_reason="规则版本不存在",
            heaping={}, covariate_balance_bw8={}, merchant_overlap=(0, 0),
            mismatched_receipts_visible=(), score_reviews_excluded=(), audit=(),
        )
        return PlanVerdict(
            plan=plan, final_decision="return_scope",
            reason_summary="规则版本不存在，无法建立共同量表口径",
            scope_ok=False, scope_detail=scope_detail,
            submission=empty, current=empty, evidence_corrections=(),
            late_arrivals=(), cutoff_jump_diagnostic=None, audit=tuple(audit),
        )

    scope_detail["rule_scale"] = rule.scale
    scope_detail["plan_cutoff"] = str(plan.cutoff)
    scope_detail["rule_cutoff"] = str(rule.cutoff)

    # ---- 范围退回：请求 cutoff 与规则真实 cutoff 不一致 ----
    if plan.cutoff != rule.cutoff:
        jump = _cutoff_jump_diagnostic(wh, plan, rule.cutoff, current_asof)
        sub = _evaluate_view(wh, plan, rule.cutoff, plan.asof)
        cur = _evaluate_view(wh, plan, rule.cutoff, current_asof)
        audit.append(
            f"范围退回：计划目标 cutoff={plan.cutoff} 与 {plan.version} 规则实际 "
            f"cutoff={rule.cutoff} 不一致；资格跳变由规则在 {rule.cutoff} 处产生，"
            f"{plan.cutoff} 分附近不存在该规则制造的自然对照。"
        )
        audit.append(
            f"当前全量证据（asof={current_asof}）：真实 cutoff {rule.cutoff} 处展示率跳变 "
            f"{_jump_at(jump, rule.cutoff)}，请求 cutoff {plan.cutoff} 处仅 "
            f"{_jump_at(jump, plan.cutoff)}；请求阈值缺乏规则驱动的展示跳变支持。"
        )
        return PlanVerdict(
            plan=plan, final_decision="return_scope",
            reason_summary=(
                f"请求阈值 {plan.cutoff} 与规则实际阈值 {rule.cutoff} 不一致，"
                "邻域无对应证据，退回缩小范围或补证，不得以无显著差异批准扩张"
            ),
            scope_ok=False, scope_detail=scope_detail,
            submission=sub, current=cur, evidence_corrections=(),
            late_arrivals=(), cutoff_jump_diagnostic=jump, audit=tuple(audit),
        )

    # ---- 同量表、同 cutoff 的正常审议：双视图 + 证据订正 ----
    corrections, late_arrivals = _detect_changes(wh, plan, current_asof)
    for ch in corrections:
        flag = "改变估计口径" if ch.alters_estimate else "不改变展示与资格，仅消除数据质量标记"
        audit.append(f"证据订正（{ch.table}）：{ch.order} {ch.description}；影响：{flag}")
    if late_arrivals:
        audit.append(
            f"另有 {len(late_arrivals)} 条 rev1 记录在提交 asof 之后才到达（资料各自按时到达的"
            "正常情形），已由成熟度窗口与 asof 视图处理，不计为失效证据。"
        )

    submission = _evaluate_view(wh, plan, rule.cutoff, plan.asof)
    current = _evaluate_view(wh, plan, rule.cutoff, current_asof)
    audit.extend(submission.audit)
    audit.extend(current.audit)

    if plan.status == "executed":
        audit.append(
            f"计划 {plan.plan} 已执行（active_on={plan.active_on}）：旧规则动作保留，"
            "双视图重算仅作证据留痕，不产生切换或回滚。"
        )
        return PlanVerdict(
            plan=plan, final_decision="retained",
            reason_summary="已执行的旧规则动作保留；重算证据留痕，不改变既有处置",
            scope_ok=True, scope_detail=scope_detail,
            submission=submission, current=current,
            evidence_corrections=corrections, late_arrivals=late_arrivals,
            cutoff_jump_diagnostic=None, audit=tuple(audit),
        )

    # pending：两个视图都支持切换、且没有推翻结论的订正，才允许切换
    invalidating = [ch for ch in corrections if ch.alters_estimate]
    if (submission.view_decision == "switch"
            and current.view_decision == "switch" and not invalidating):
        decision, reason = "switch", current.view_reason
    else:
        decision = "hold"
        why = []
        if submission.view_decision != "switch":
            why.append(f"提交时点视图结论为 {submission.view_decision}（{submission.view_reason}）")
        if current.view_decision != "switch":
            why.append(f"订正后当前视图结论为 {current.view_decision}（{current.view_reason}）")
        if invalidating:
            why.append("存在改变估计口径的证据订正（"
                       + "、".join(ch.order for ch in invalidating) + "）")
        reason = "；".join(why)
        audit.append(
            "待生效切换未获双视图一致支持，或依赖的证据已被订正，不能照常生效；维持旧规则。"
        )

    return PlanVerdict(
        plan=plan, final_decision=decision, reason_summary=reason,
        scope_ok=True, scope_detail=scope_detail,
        submission=submission, current=current,
        evidence_corrections=corrections, late_arrivals=late_arrivals,
        cutoff_jump_diagnostic=None, audit=tuple(audit),
    )


def _jump_at(jump_diag: dict, cutoff: int) -> str:
    rows = jump_diag["cutoffs"][cutoff]
    return " / ".join(
        f"bw{bw} 跳变 {d['jump']:.3f}" for bw, d in sorted(rows.items())
        if d["jump"] == d["jump"]
    )


def evaluate_all(wh: Warehouse) -> list[PlanVerdict]:
    return [evaluate_plan(wh, p) for p in wh.plans]
