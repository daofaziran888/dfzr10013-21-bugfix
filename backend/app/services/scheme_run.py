"""方案分析批次管理：负责分析批次的落库、检索、派生（权重调整）与报告端取数。

批次一旦出具即不可变：输入修订只生成新批次，不回写已保存的分析；
排序、敏感性与 run_id 同批绑定，报告端只能引用本批次的结论，杜绝跨批次串用。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services import scheme_analysis as engine
from app.store import store

MODULE = "scheme_analysis"


class SchemeRunService:
    def dry_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只试算不落库：用于前端预检，返回完整结论或错误定位。"""
        return engine.run_analysis(payload)

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发起一次新分析。硬错误时不落库、不发布排名；成功时保存不可变批次。"""
        result = engine.run_analysis(payload)
        if result["status"] == engine.STATUS_FAILED:
            return result

        run_id = self._next_run_id()
        record = {
            "id": self._next_row_id(),
            "run_id": run_id,
            "subject": payload.get("subject"),
            "status": result["status"],
            "rankable": result["rankable"],
            "created_at": date.today().isoformat(),
            "input_snapshot": payload,
            "result": result,
            "derived_from_run_id": None,
            "reweight_of": None,
            "pending": False,
            "abnormal": result["status"] == engine.STATUS_UNRANKABLE,
        }
        result["run_id"] = run_id
        result["created_at"] = record["created_at"]
        store.rows(MODULE).append(record)
        return result

    def list_runs(self) -> list[dict[str, Any]]:
        items = []
        for record in store.rows(MODULE):
            items.append({
                "run_id": record["run_id"],
                "subject": record["subject"],
                "status": record["status"],
                "rankable": record["rankable"],
                "created_at": record["created_at"],
                "derived_from_run_id": record["derived_from_run_id"],
                "reweight_of": record["reweight_of"],
                "top_option": (
                    record["result"]["ranking"][0]["option_id"]
                    if record["result"].get("ranking")
                    else None
                ),
            })
        return items

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        record = self._find(run_id)
        if record is None:
            return None
        return self._present(record)

    def reweight(self, run_id: str, new_weights: dict[str, float]) -> tuple[dict[str, Any] | None, str]:
        """在同一批次输入快照上调整权重，生成派生批次；原批次保持不变。"""
        record = self._find(run_id)
        if record is None:
            return None, f"方案分析批次 {run_id} 不存在或已归档"
        result = engine.reweight(record, new_weights)
        if result["status"] == engine.STATUS_FAILED:
            # 权重非法等：不生成派生批次，返回错误定位由路由层转 422
            return result, ""
        new_run_id = self._next_run_id()
        derived = {
            "id": self._next_row_id(),
            "run_id": new_run_id,
            "subject": record["subject"],
            "status": result["status"],
            "rankable": result["rankable"],
            "created_at": date.today().isoformat(),
            "input_snapshot": result.get("input_snapshot") or record["input_snapshot"],
            "result": result,
            "derived_from_run_id": run_id,
            "reweight_of": run_id,
            "pending": False,
            "abnormal": result["status"] == engine.STATUS_UNRANKABLE,
        }
        result["run_id"] = new_run_id
        result["created_at"] = derived["created_at"]
        result["derived_from_run_id"] = run_id
        store.rows(MODULE).append(derived)
        return result, ""

    def report_dataset(self, run_id: str) -> dict[str, Any] | None:
        """报告端取数：只返回该批次自身的对比结论数据集，附带覆盖、权重、转换依据与敏感区间。"""
        record = self._find(run_id)
        if record is None:
            return None
        result = record["result"]
        return {
            "run_id": record["run_id"],
            "subject": record["subject"],
            "created_at": record["created_at"],
            "derived_from_run_id": record["derived_from_run_id"],
            "status": result["status"],
            "rankable": result["rankable"],
            "base_currency": result["base_currency"],
            "base_period_unit": result["base_period_unit"],
            "weights": result["weights"],
            "weight_labels": result["weight_labels"],
            "coverage_summary": result["coverage_summary"],
            "common_scenarios": result["common_scenarios"],
            "comparison_table": result["comparison_table"],
            "conversion_basis": result["conversion_basis"],
            "ranking": result["ranking"],
            "sensitivity": {
                "baseline_order": result["sensitivity"].get("baseline_order", []),
                "flip_events": result["sensitivity"].get("flip_events", []),
                "fragile_to_small_weight_change": result["sensitivity"].get(
                    "fragile_to_small_weight_change", False
                ),
                "weight_flip_intervals": [
                    {
                        "dimension": s["dimension"],
                        "dimension_label": s["dimension_label"],
                        "flip_interval": s["flip_interval"],
                        "first_flip": s["first_flip"],
                    }
                    for s in result["sensitivity"].get("weight_sweeps", [])
                ],
                "probability_flip_intervals": [
                    {
                        "scenario": s["scenario"],
                        "scenario_label": s["scenario_label"],
                        "flip_interval": s["flip_interval"],
                        "first_flip": s["first_flip"],
                    }
                    for s in result["sensitivity"].get("probability_sweeps", [])
                ],
            },
            "checks": result["checks"],
            "warnings": result["warnings"],
            "conclusion": result["conclusion"],
        }

    # ------------------------------------------------------------------ 内部方法

    def _find(self, run_id: str) -> dict[str, Any] | None:
        for record in store.rows(MODULE):
            if record["run_id"] == run_id:
                return record
        return None

    def _present(self, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record["result"])
        result["run_id"] = record["run_id"]
        result["created_at"] = record["created_at"]
        result["derived_from_run_id"] = record["derived_from_run_id"]
        return result

    def _next_row_id(self) -> int:
        return max((int(r.get("id", 0)) for r in store.rows(MODULE)), default=0) + 1

    def _next_run_id(self) -> str:
        today = date.today().strftime("%Y%m%d")
        prefix = f"SA-{today}-"
        seq = max(
            (
                int(r["run_id"].rsplit("-", 1)[-1])
                for r in store.rows(MODULE)
                if str(r.get("run_id", "")).startswith(prefix)
            ),
            default=0,
        )
        return f"{prefix}{seq + 1:03d}"


scheme_run_service = SchemeRunService()
