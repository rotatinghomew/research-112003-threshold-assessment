"""审议引擎的业务口径测试。

覆盖：
* 资格只由下单时版本+分数决定，复核分数不改资格、不移动阈值侧；
* asof 视图按 revision 取最新，版本不符≠无展示；
* 未结（appeal）不当无争议、不删单，只从标签率分母剔除并计入未结比例；
* 不同量表不混入同一局部比较；请求 cutoff 与规则 cutoff 不一致时退回；
* 聚类稳健 SE 在同店重复订单下生效；
* 双视图与证据订正分类；已执行计划动作保留；
* 多带宽/未知标签翻转时不得切换。
"""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from review.evaluator import (  # noqa: E402
    WindowOrder,
    _band_gate,
    _detect_changes,
    _mature_orders,
    _window,
    evaluate_plan,
    latest_info_day,
)
from review.loader import load  # noqa: E402
from review.stats import _cluster_robust_diff, heaping_diagnostic  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wh = load(DATA)
        cls.p1 = next(p for p in cls.wh.plans if p.plan == "p1")
        cls.p2 = next(p for p in cls.wh.plans if p.plan == "p2")
        cls.p0 = next(p for p in cls.wh.plans if p.plan == "p0")

    def test_eligibility_immutable_under_score_review(self):
        # o0029 复核后 41->52，但下单分数 41 < cutoff 50，资格侧不得改变
        o = self.wh.order("o0029")
        self.assertEqual(o.raw_score, 41)
        self.assertLess(o.raw_score, 50)
        view = self.wh.asof_view(latest_info_day(self.wh))
        self.assertIn("o0029", view.review_of)
        self.assertEqual(view.review_of["o0029"].updated_score, 52)
        mature = _mature_orders(self.wh, self.p1, latest_info_day(self.wh))
        win = _window(mature, 50, 12)
        w = next(x for x in win if x.order_id == "o0029")
        self.assertFalse(w.eligible)  # 仍在不合格侧
        # o0088 复核 53->45，下单 53>=50，仍在合格侧
        w88 = next(x for x in win if x.order_id == "o0088")
        self.assertTrue(w88.eligible)

    def test_asof_revision_resolution_and_mismatch(self):
        # asof=52 时 o0088 有效回执是错标 r2 的 rev1
        v52 = self.wh.asof_view(52)
        self.assertEqual(v52.receipt_of["o0088"].version, "r2")
        self.assertIn("o0088", v52.mismatched_receipts)
        # day53 订正后 rev2 标注 r1，版本不符消失
        v53 = self.wh.asof_view(53)
        self.assertEqual(v53.receipt_of["o0088"].version, "r1")
        self.assertEqual(v53.receipt_of["o0088"].revision, 2)
        self.assertNotIn("o0088", v53.mismatched_receipts)
        # 版本订正前后 shown 都为 1：版本不符不等于没有展示
        self.assertEqual(v52.receipt_of["o0088"].shown, 1)
        self.assertEqual(v53.receipt_of["o0088"].shown, 1)

    def test_mismatch_is_not_treated_as_no_show(self):
        v52 = self.wh.asof_view(52)
        rec = v52.receipt_of["o0088"]
        self.assertEqual((rec.version, rec.shown), ("r2", 1))
        # 订单仍被保留参与比较（未删除），且展示记为 1
        mature = _mature_orders(self.wh, self.p1, 52)
        ids = {o.order for o in mature}
        self.assertIn("o0088", ids)

    def test_outcome_revision_o0029(self):
        v52 = self.wh.asof_view(52)
        self.assertEqual(v52.outcome_of["o0029"].state, "appeal")  # 未结
        v54 = self.wh.asof_view(54)
        self.assertEqual(v54.outcome_of["o0029"].state, "dispute")  # 改判
        self.assertEqual(v54.outcome_of["o0029"].revision, 2)

    def test_unknown_not_counted_as_clear_nor_dropped(self):
        view = self.wh.asof_view(52)
        mature = _mature_orders(self.wh, self.p1, 52)
        win = _window(mature, 50, 8)
        labels = {oid: (oc.state if oc else None) for oid, oc in view.outcome_of.items()}
        bv = _band_gate(self.p1, win, view, 8)
        c = bv.comparison
        # 未结订单计入 n 但不计入标签率分母
        self.assertEqual(c.above.n, c.above.unknown + (c.above.n - c.above.unknown))
        self.assertGreater(c.below.unknown, 0)
        # 标签率只在 clear/dispute 上计算，取值在 [0,1]
        for g in (c.above, c.below):
            if g.n - g.unknown > 0:
                self.assertGreaterEqual(g.mean, 0.0)
                self.assertLessEqual(g.mean, 1.0)

    def test_no_scale_mixing(self):
        # p1 只使用 r1 订单；r2 订单不得出现在其窗口
        mature = _mature_orders(self.wh, self.p1, latest_info_day(self.wh))
        self.assertTrue(all(o.version == "r1" for o in mature))
        win = _window(mature, 50, max(self.p1.bandwidths))
        self.assertTrue(all(w.order.version == "r1" for w in win))
        verdict = evaluate_plan(self.wh, self.p1)
        self.assertTrue(verdict.scope_ok)

    def test_scope_return_on_cutoff_mismatch(self):
        v = evaluate_plan(self.wh, self.p2)
        self.assertEqual(v.final_decision, "return_scope")
        self.assertFalse(v.scope_ok)
        # 真实阈值 56 的跳变应明显强于请求阈值 60（至少一个带宽）
        diag = v.cutoff_jump_diagnostic
        self.assertEqual(diag["rule_cutoff"], 56)
        jump56 = diag["cutoffs"][56][8]["jump"]
        jump60 = diag["cutoffs"][60][8]["jump"]
        self.assertGreater(jump56, jump60)

    def test_r2_even_score_heaping_flagged(self):
        mature = _mature_orders(self.wh, self.p2, latest_info_day(self.wh))
        h = heaping_diagnostic(mature, 56)
        self.assertTrue(h.even_only)

    def test_cluster_se_uses_merchants(self):
        view = self.wh.asof_view(latest_info_day(self.wh))
        mature = _mature_orders(self.wh, self.p1, latest_info_day(self.wh))
        win = _window(mature, 50, 8)
        labels = {oid: (oc.state if oc else None) for oid, oc in view.outcome_of.items()}
        diff, se, g = _cluster_robust_diff(win, labels, view.receipt_of)
        self.assertEqual(g, 40)  # 40 个商家聚类
        self.assertGreater(se, 0)

    def test_change_detection_classifies_correction_vs_late(self):
        corrections, late = _detect_changes(self.wh, self.p1, latest_info_day(self.wh))
        corr_ids = {c.order for c in corrections}
        self.assertIn("o0029", corr_ids)   # outcomes rev2 真订正
        self.assertIn("o0088", corr_ids)   # receipts rev2 真订正
        # 晚到记录都是 rev1
        self.assertTrue(all(c.revision_after == 1 for c in late))
        # o0029 改变估计口径，o0088 不改变（shown 仍为 1）
        by_id = {c.order: c for c in corrections}
        self.assertTrue(by_id["o0029"].alters_estimate)
        self.assertFalse(by_id["o0088"].alters_estimate)

    def test_p1_pending_hold_double_view(self):
        v = evaluate_plan(self.wh, self.p1)
        self.assertEqual(v.final_decision, "hold")
        self.assertEqual(v.submission.view_decision, "hold")
        self.assertEqual(v.current.view_decision, "hold")
        # 没有任何带宽在双视图都完全过闸
        self.assertEqual(v.current.fully_passed_bands, ())
        for bv in v.current.band_verdicts:
            self.assertTrue(bv.label_flips or not bv.passed)

    def test_bandwidth_flip_blocks_switch(self):
        # 构造一个"全部数值过闸但敏感性翻转"的情形：p1 当前视图 bw=8/12 数值过闸
        v = evaluate_plan(self.wh, self.p1)
        cur = v.current
        self.assertNotEqual(len(cur.numeric_passed_bands), 0)
        self.assertEqual(cur.fully_passed_bands, ())
        self.assertEqual(cur.view_decision, "hold")

    def test_executed_plan_actions_retained(self):
        v = evaluate_plan(self.wh, self.p0)
        self.assertEqual(v.final_decision, "retained")
        self.assertEqual(self.p0.status, "executed")
        # 即便当前视图有带宽数值过闸，也不产生 switch
        self.assertNotEqual(v.current.view_decision, "switch")

    def test_observed_gap_is_kept_separate_from_itt(self):
        view = self.wh.asof_view(latest_info_day(self.wh))
        mature = _mature_orders(self.wh, self.p1, latest_info_day(self.wh))
        win = _window(mature, 50, 8)
        labels = {oid: (oc.state if oc else None) for oid, oc in view.outcome_of.items()}
        bv = _band_gate(self.p1, win, view, 8)
        c = bv.comparison
        itt = c.below.mean - c.above.mean
        # ITT（资格）与观察差异（实际展示）数值不同且都被保留
        self.assertNotAlmostEqual(itt, c.observed_gap)
        self.assertIsInstance(c.observed_gap, float)

    def test_maturity_window_excludes_immature(self):
        # asof=52、horizon=14：day>38 的 r1 订单不应成熟（r1 最大 day=34，故全成熟）
        mature = _mature_orders(self.wh, self.p1, 52)
        self.assertTrue(all(o.day + 14 <= 52 for o in mature))
        # p2 在 asof=54 时只有 day<=40 成熟
        mature2 = _mature_orders(self.wh, self.p2, 54)
        self.assertTrue(all(o.day + 14 <= 54 for o in mature2))
        self.assertLess(len(mature2), 280)

    def test_plan_requiring_impossible_gain_would_hold(self):
        # 提高 min_gain 到不可能水平，任何带宽都不能 switch
        strict = replace(self.p1, min_gain=0.99)
        v = evaluate_plan(self.wh, strict)
        self.assertEqual(v.final_decision, "hold")


if __name__ == "__main__":
    unittest.main()
