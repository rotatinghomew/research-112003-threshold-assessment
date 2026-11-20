"""把审议裁决渲染成可回看的审计报告（Markdown）与机器可读 JSON。"""
from __future__ import annotations

import json
from dataclasses import asdict

from .evaluator import PlanVerdict, ViewVerdict


def _fmt(x, nd=3):
    if x is None:
        return "NA"
    if isinstance(x, float):
        if x != x:  # NaN
            return "NA"
        return f"{x:.{nd}f}"
    return str(x)


def _view_block(vv: ViewVerdict, title: str) -> list[str]:
    lines = [f"#### {title}（asof={vv.asof}）"]
    lines.append(
        f"- 成熟订单 {vv.n_mature}，最大候选带宽内 {vv.n_mature_in_window}；"
        f"商家两侧重叠 {vv.merchant_overlap[1]}/{vv.merchant_overlap[0]}"
    )
    if vv.heaping:
        lines.append(
            f"- 分数扎堆：近阈两侧计数 下={vv.heaping['near_below']} / "
            f"上={vv.heaping['near_above']}（比值 {_fmt(vv.heaping['near_ratio'])}），"
            f"仅偶数分数={vv.heaping['even_only']}"
        )
    if vv.covariate_balance_bw8:
        bal = "，".join(
            f"{cov} 合格={_fmt(a)} / 不合格={_fmt(b)}"
            for cov, (a, b) in vv.covariate_balance_bw8.items()
        )
        lines.append(f"- 下单前特征平衡（bw=8）：{bal}")
    if vv.mismatched_receipts_visible:
        lines.append(f"- 该视图可见的回执版本异常：{', '.join(vv.mismatched_receipts_visible)}")
    if vv.score_reviews_excluded:
        lines.append(f"- 排除的事后复核分数（不改资格）：{', '.join(vv.score_reviews_excluded)}")
    lines.append(f"- 视图结论：**{vv.view_decision.upper()}** — {vv.view_reason}")

    if vv.band_verdicts:
        lines.append("")
        lines.append("| 带宽 | 每侧 n(合格/不合格) | 未结比例(上/下) | 展示率(上/下) | 跳变 | ITT增益 | 聚类SE | z | p | 未结极端增益 | 数值过闸 | 敏感性不翻转 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for bv in vv.band_verdicts:
            c = bv.comparison
            gain = (c.below.mean - c.above.mean
                    if c.above.mean == c.above.mean and c.below.mean == c.below.mean
                    else float("nan"))
            lines.append(
                "| {bw} | {na}/{nb} | {ua}/{ub} | {sa}/{sb} | {jump} | {gain} | {se} | {z} | {pv} | {worst}..{best} | {ok} | {fl} |".format(
                    bw=bv.bandwidth,
                    na=c.above.n, nb=c.below.n,
                    ua=_fmt(c.above.unknown_rate), ub=_fmt(c.below.unknown_rate),
                    sa=_fmt(c.above.show_rate), sb=_fmt(c.below.show_rate),
                    jump=_fmt(c.show_jump), gain=_fmt(gain),
                    se=_fmt(c.cluster_se), z=_fmt(c.z, 2), pv=_fmt(c.p_value, 3),
                    worst=_fmt(bv.worst_case_gain), best=_fmt(bv.best_case_gain),
                    ok="是" if bv.passed else "否",
                    fl="是" if not bv.label_flips else "否",
                )
            )
        lines.append("")
        lines.append("描述性对照（实际展示差异，非因果，不能当作提醒的处置效果）：")
        for bv in vv.band_verdicts:
            c = bv.comparison
            gain = (c.below.mean - c.above.mean
                    if c.above.mean == c.above.mean and c.below.mean == c.below.mean
                    else float("nan"))
            lines.append(
                f"- bw={bv.bandwidth}：shown n={c.shown_group.n} 争议率={_fmt(c.shown_group.mean)}；"
                f"not-shown n={c.notshown_group.n} 争议率={_fmt(c.notshown_group.mean)}；"
                f"观察差异={_fmt(c.observed_gap)}；规则资格 ITT 增益={_fmt(gain)}"
            )
    lines.append("")
    return lines


def render_markdown(verdicts: list[PlanVerdict]) -> str:
    lines: list[str] = []
    lines.append("# 风险阈值规则切换审议报告\n")
    lines.append("**口径约定**")
    lines.append("- 一个订单是一项观察；未结（appeal 等）不计为无争议、也不删单，")
    lines.append("  争议率仅基于 clear/dispute 已贴标签订单，未结比例单独设闸。")
    lines.append("- 资格只由下单时不可变的版本与 raw_score 相对规则 cutoff 决定；")
    lines.append("  事后复核分数仅供后续处置，不改资格、不移动订单的阈值侧。")
    lines.append("- r1(raw-v1) 与 r2(composite-v2) 量表不可互换，绝不混入同一局部比较。")
    lines.append("- 差值检验按 merchant 聚类稳健 SE（同店订单不独立）。")
    lines.append("- ITT = 按规则资格的局部效果；实际展示差异仅为描述性对照，不作因果解释。")
    lines.append("- 待生效计划需提交视图与订正后当前视图双视图一致支持才可切换；")
    lines.append("  已执行计划的旧规则动作保留，重算仅留痕。\n")

    for v in verdicts:
        p = v.plan
        lines.append(f"## 计划 {p.plan}（登记状态 status={p.status}）")
        lines.append(
            f"- 请求：版本 `{p.version}`，目标 cutoff={p.cutoff}，"
            f"带宽 {list(p.bandwidths)}，min_per_side={p.min_per_side}，"
            f"max_unknown={p.max_unknown}，min_gain={p.min_gain}，"
            f"min_jump={p.min_jump}，horizon={p.horizon}，"
            f"提交 asof={p.asof}，拟 active_on={p.active_on}"
        )
        lines.append("- 范围检查：" + "，".join(f"{k}={val}" for k, val in v.scope_detail.items()))

        if v.cutoff_jump_diagnostic is not None:
            diag = v.cutoff_jump_diagnostic
            lines.append("")
            lines.append(f"### 展示率跳变诊断（asof={diag['asof']} 全量证据）")
            lines.append("| cutoff | 带宽 | n(合格/不合格) | 展示率(合格/不合格) | 跳变 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for cut, rows in diag["cutoffs"].items():
                for bw, d in sorted(rows.items()):
                    tag = "（规则真实阈值）" if cut == diag["rule_cutoff"] else "（计划请求阈值）"
                    lines.append(
                        f"| {cut}{tag} | {bw} | {d['n_above']}/{d['n_below']} | "
                        f"{_fmt(d['show_above'])}/{_fmt(d['show_below'])} | {_fmt(d['jump'])} |"
                    )

        if v.evidence_corrections or v.late_arrivals:
            lines.append("")
            lines.append("### 证据订正与晚到记录（重判可用范围的依据）")
            for ch in v.evidence_corrections:
                lines.append(
                    f"- 【订正】`{ch.order}` / {ch.table} rev{ch.revision_before}→rev{ch.revision_after}"
                    f"（known={ch.known_after}）：{ch.description}；alters_estimate={ch.alters_estimate}"
                )
            if v.late_arrivals:
                lines.append(
                    f"- 【晚到 rev1，非订正】共 {len(v.late_arrivals)} 条："
                    + "、".join(f"{ch.order}/{ch.table}" for ch in v.late_arrivals)
                    + "；按各自到达时间纳入对应 asof 视图，未删单，也不计为失效证据。"
                )

        lines.append("")
        lines.append("### 提交时点视图 vs 订正后当前视图")
        lines.extend(_view_block(v.submission, "提交时点"))
        lines.extend(_view_block(v.current, "订正后当前时点"))

        lines.append(f"### 最终裁决：**{v.final_decision.upper()}**")
        lines.append(v.reason_summary)
        lines.append("")
        lines.append("审计轨迹：")
        for item in v.audit:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)


def render_json(verdicts: list[PlanVerdict]) -> str:
    def clean(obj):
        if isinstance(obj, float):
            return None if obj != obj else obj
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean(v) for v in obj]
        return obj

    return json.dumps(
        [clean(asdict(v)) for v in verdicts], ensure_ascii=False, indent=2
    )
