"""方案分析引擎与批次服务测试。

运行：backend/.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services import scheme_analysis as engine
from app.services.scheme_run import SchemeRunService


def base_payload() -> dict:
    """两个完整选项、三情景闭合、双风险指标（含一组相关风险）的标准输入。"""
    return {
        "subject": "新产线投资方案对比",
        "base_currency": "CNY",
        "base_period_unit": "month",
        "currency_rates": {"USD": 7.0, "EUR": 7.7},
        "options": [
            {"option_id": "A", "name": "自建产线"},
            {"option_id": "B", "name": "外包合作"},
        ],
        "scenarios": [
            {"option_id": "A", "scenario": "悲观", "probability": 0.2, "benefit": 800, "cost": 700,
             "currency": "万元", "period": 12, "period_unit": "月"},
            {"option_id": "A", "scenario": "中性", "probability": 0.5, "benefit": 1500, "cost": 900,
             "currency": "万元", "period": 12, "period_unit": "月"},
            {"option_id": "A", "scenario": "乐观", "probability": 0.3, "benefit": 2400, "cost": 1100,
             "currency": "万元", "period": 12, "period_unit": "月"},
            {"option_id": "B", "scenario": "pessimistic", "probability": 0.2, "benefit": 500, "cost": 350,
             "currency": "CNY万元", "period": 4, "period_unit": "quarter"},
            {"option_id": "B", "scenario": "neutral", "probability": 0.5, "benefit": 1100, "cost": 600,
             "currency": "CNY万元", "period": 4, "period_unit": "quarter"},
            {"option_id": "B", "scenario": "optimistic", "probability": 0.3, "benefit": 1600, "cost": 800,
             "currency": "CNY万元", "period": 4, "period_unit": "quarter"},
        ],
        "risks": [
            {"option_id": "A", "metric": "交付延期率", "value": 0.3, "measure": "probability",
             "correlation_group": "g1"},
            {"option_id": "A", "metric": "供应中断率", "value": 0.25, "measure": "probability",
             "correlation_group": "g1"},
            {"option_id": "A", "metric": "合规罚没", "value": 80, "measure": "amount", "currency": "万元"},
            {"option_id": "B", "metric": "交付延期率", "value": 0.15, "measure": "probability",
             "correlation_group": "g1"},
            {"option_id": "B", "metric": "供应中断率", "value": 0.12, "measure": "probability",
             "correlation_group": "g1"},
            {"option_id": "B", "metric": "合规罚没", "value": 30, "measure": "amount", "currency": "万元"},
        ],
    }


class EngineValidationTest(unittest.TestCase):
    def test_probability_not_closed_is_localized(self):
        payload = base_payload()
        payload["scenarios"][0]["probability"] = 0.4  # A 的概率和变为 1.2
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        codes = {e["code"] for e in result["errors"]}
        self.assertIn("PROB_NOT_CLOSED", codes)
        located = [e for e in result["errors"] if e["code"] == "PROB_NOT_CLOSED"]
        self.assertEqual(located[0]["option_id"], "A")
        self.assertEqual(result["ranking"], [])

    def test_decimal_rounding_is_warning_not_error(self):
        payload = base_payload()
        payload["scenarios"][0]["probability"] = 0.200005  # 偏差 5e-6，落在舍入提示区间
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_RANKABLE)
        self.assertTrue(any(w["code"] == "PROB_DECIMAL_ROUNDING" for w in result["warnings"]))
        self.assertTrue(any(c["code"] == "PROB_DECIMAL_ROUNDING" for c in result["checks"]))

    def test_currency_incompatible_is_localized(self):
        payload = base_payload()
        payload["scenarios"][0]["currency"] = "JPY"  # 无汇率
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        cur_errors = [e for e in result["errors"] if e["code"] == "CURRENCY_INCOMPATIBLE"]
        self.assertTrue(cur_errors)
        self.assertEqual(cur_errors[0]["option_id"], "A")

    def test_period_incompatible_is_localized(self):
        payload = base_payload()
        payload["scenarios"][1]["period_unit"] = "fortnight"
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        self.assertTrue(any(e["code"] == "PERIOD_INCOMPATIBLE" for e in result["errors"]))

    def test_duplicate_scenario_row_is_localized(self):
        payload = base_payload()
        payload["scenarios"].append(dict(payload["scenarios"][0]))  # A 悲观重复
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        dup = [e for e in result["errors"] if e["code"] == "DUPLICATE_SCENARIO"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["option_id"], "A")

    def test_duplicate_risk_metric_flagged(self):
        payload = base_payload()
        payload["risks"].append(dict(payload["risks"][0]))
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        self.assertTrue(any(e["code"] == "RISK_DUPLICATE" for e in result["errors"]))

    def test_weight_must_be_closed_and_meaningful(self):
        payload = base_payload()
        payload["weights"] = {engine.DIM_EXPECTED: 0.9, engine.DIM_DOWNSIDE: 0.2}
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        self.assertTrue(any(e["code"] == "WEIGHT_NOT_CLOSED" for e in result["errors"]))

        payload["weights"] = {"arbitrary_score": 1.0}
        result = engine.run_analysis(payload)
        self.assertTrue(any(e["code"] == "WEIGHT_UNKNOWN_DIMENSION" for e in result["errors"]))


class EngineCalculationTest(unittest.TestCase):
    def test_currency_and_period_conversion_with_basis(self):
        result = engine.run_analysis(base_payload())
        self.assertEqual(result["status"], engine.STATUS_RANKABLE)
        basis = result["conversion_basis"]
        # 万元 -> 元 ×10000；季度 -> 月 ×3，依据可查
        a_row = next(r for r in result["converted_scenarios"]
                     if r["option_id"] == "A" and r["scenario"] == "neutral")
        self.assertEqual(a_row["converted"]["currency_factor"], 10000.0)
        self.assertEqual(a_row["converted"]["period_months"], 12.0)
        b_row = next(r for r in result["converted_scenarios"]
                     if r["option_id"] == "B" and r["scenario"] == "neutral")
        self.assertEqual(b_row["converted"]["period_factor_to_month"], 3.0)
        self.assertEqual(b_row["converted"]["period_months"], 12.0)
        self.assertTrue(all(b["currency"]["factor"] is not None for b in basis))
        # 中性净收益：A (1500-900)万 / 12 月 = 50 万/月
        self.assertAlmostEqual(
            a_row["converted"]["net_base_per_month"], 500_000.0, places=2
        )

    def test_foreign_currency_unit_combines_fx_rate_and_unit_factor(self):
        # 回归：USD万元 必须同时乘汇率 7.0 与单位倍数 10000，不能漏乘汇率
        payload = base_payload()
        payload["currency_rates"] = {"USD": 7.0}
        for row in payload["scenarios"]:
            if row["option_id"] == "A":
                row["currency"] = "USD万元"
        for row in payload["risks"]:
            if row["option_id"] == "A" and row.get("measure") == "amount":
                row["currency"] = "USD万元"
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_RANKABLE)
        a_row = next(r for r in result["converted_scenarios"]
                     if r["option_id"] == "A" and r["scenario"] == "neutral")
        self.assertEqual(a_row["converted"]["currency_factor"], 70_000.0)
        a_risk = next(r for r in result["converted_risks"]
                      if r["option_id"] == "A" and r["measure"] == "amount")
        self.assertEqual(a_risk["converted"]["value_base"], 80 * 70_000.0)
        self.assertEqual(a_row["conversion_basis"]["currency"]["rate_source"], "payload.currency_rates")

        # 千美元固定写法：隐含 USD，系数 = 1000 × 汇率
        for row in payload["scenarios"]:
            if row["option_id"] == "A":
                row["currency"] = "千美元"
        result2 = engine.run_analysis(payload)
        a_row2 = next(r for r in result2["converted_scenarios"]
                      if r["option_id"] == "A" and r["scenario"] == "neutral")
        self.assertEqual(a_row2["converted"]["currency_factor"], 7_000.0)

    def test_ranking_and_sensitivity_bound_to_same_run(self):
        result = engine.run_analysis(base_payload())
        self.assertEqual(len(result["ranking"]), 2)
        self.assertEqual([r["rank"] for r in result["ranking"]], [1, 2])
        order = result["sensitivity"]["baseline_order"]
        self.assertEqual(set(order), {"A", "B"})
        # 敏感性里的翻转事件必须引用本次的选项与情景，不允许外部批次 id
        for event in result["sensitivity"]["flip_events"]:
            self.assertIn(event["baseline_top"], ("A", "B"))
            self.assertIn(event["new_top"], ("A", "B"))

    def test_correlated_risks_merged_not_double_counted(self):
        result = engine.run_analysis(base_payload())
        calc_a = result["option_calculations"]["A"]
        self.assertTrue(calc_a["risk_merge_trace"])
        trace = calc_a["risk_merge_trace"][0]
        self.assertEqual(trace["members"], ["交付延期率", "供应中断率"])
        # 组内取最大值 0.3
        self.assertAlmostEqual(calc_a["risk_merged"]["group:g1"], 0.3)

    def test_missing_scenario_is_not_zero_and_blocks_full_ranking(self):
        payload = base_payload()
        # B 缺悲观情景，且显式登记为 missing
        payload["scenarios"][3] = {
            "option_id": "B", "scenario": "pessimistic", "status": "missing",
        }
        result = engine.run_analysis(payload)
        # B 情景不完整 → 完整方案不足 2 个 → 不出整体排名
        self.assertEqual(result["status"], engine.STATUS_UNRANKABLE)
        self.assertEqual(result["ranking"], [])
        coverage_b = next(c for c in result["coverage_summary"] if c["option_id"] == "B")
        self.assertFalse(coverage_b["included_in_ranking"])
        uncov = [u for u in coverage_b["uncovered_items"] if u["type"] == "scenario"]
        self.assertTrue(any(u["scenario"] == "pessimistic" for u in uncov))
        # 共同可比情景只剩中性/乐观
        self.assertEqual(set(result["common_scenarios"]), {"neutral", "optimistic"})
        # 缺失检查项
        self.assertTrue(any(c["code"] == "MISSING_WORST_CASE" and c["option_id"] == "B"
                            for c in result["checks"]))

    def test_missing_risk_is_not_treated_as_zero_risk(self):
        payload = base_payload()
        payload["risks"][-1] = {"option_id": "B", "metric": "合规罚没", "status": "missing",
                                "measure": "amount", "currency": "万元"}
        result = engine.run_analysis(payload)
        self.assertEqual(result["status"], engine.STATUS_UNRANKABLE)
        coverage_b = next(c for c in result["coverage_summary"] if c["option_id"] == "B")
        self.assertTrue(any(
            u["type"] == "risk" and u["metric"] == "合规罚没" for u in coverage_b["uncovered_items"]
        ))

    def test_reweight_detects_flip_and_keeps_original_immutable(self):
        service = SchemeRunService()
        run = service.create_run(base_payload())
        run_id = run["run_id"]
        original_top = run["ranking"][0]["option_id"]
        # 把权重全部压到下行保护维度，观察是否翻转
        derived, msg = service.reweight(run_id, {
            engine.DIM_EXPECTED: 0.0,
            engine.DIM_DOWNSIDE: 1.0,
            engine.DIM_RISK: 0.0,
        })
        self.assertEqual(msg, "")
        self.assertNotEqual(derived["run_id"], run_id)
        self.assertEqual(derived["reweight_of"], run_id)
        # 原批次不变
        again = service.get_run(run_id)
        self.assertEqual(again["ranking"][0]["option_id"], original_top)
        self.assertIsNone(again.get("reweight_of"))
        # 翻转事件指明具体维度与权重
        flips = [e for e in derived["sensitivity"]["flip_events"] if e["type"] == "weight_flip"]
        if derived["ranking"][0]["option_id"] != original_top:
            self.assertTrue(flips)

    def test_failed_run_is_not_persisted(self):
        service = SchemeRunService()
        before = len(service.list_runs())
        payload = base_payload()
        payload["scenarios"][0]["probability"] = 0.9
        result = service.create_run(payload)
        self.assertEqual(result["status"], engine.STATUS_FAILED)
        self.assertEqual(len(service.list_runs()), before)

    def test_report_dataset_isolated_per_run(self):
        service = SchemeRunService()
        first = service.create_run(base_payload())
        second_payload = base_payload()
        second_payload["subject"] = "第二次分析"
        second = service.create_run(second_payload)
        ds1 = service.report_dataset(first["run_id"])
        ds2 = service.report_dataset(second["run_id"])
        self.assertEqual(ds1["run_id"], first["run_id"])
        self.assertEqual(ds2["run_id"], second["run_id"])
        self.assertNotIn(second["run_id"], str(ds1["ranking"]))
        # 报告数据集保留四类关键内容
        for key in ("coverage_summary", "weights", "conversion_basis", "sensitivity", "conclusion"):
            self.assertIn(key, ds1)

    def test_input_revision_creates_new_run_never_overwrites(self):
        service = SchemeRunService()
        first = service.create_run(base_payload())
        revised = base_payload()
        revised["scenarios"][0]["benefit"] = 900
        second = service.create_run(revised)
        self.assertNotEqual(first["run_id"], second["run_id"])
        untouched = service.get_run(first["run_id"])
        a_pess = untouched["option_calculations"]["A"]["scenario_results"]["pessimistic"]
        # 首次分析的输入快照保持原值 800 万 → 净收益 100 万 / 12 月
        self.assertAlmostEqual(a_pess["benefit_base"], 8_000_000.0, places=2)


if __name__ == "__main__":
    unittest.main()
