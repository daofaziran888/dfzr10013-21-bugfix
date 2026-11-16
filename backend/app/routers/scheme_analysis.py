"""方案分析接口：在决策建议推送下新增方案分析能力。

接口分层：
- 试算（dry-run）不落库，用于预检概率闭合、量纲兼容与重复行；
- 正式分析出具不可变批次，计算失败只回错误定位，不发布部分情景的排名；
- 权重调整生成派生批次，原批次不回写；
- 报告数据集接口供经营分析报告模块跨模块取数，排序与敏感性同批绑定。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas import ReweightPayload, SchemeAnalysisPayload
from app.services import scheme_analysis as engine
from app.services.scheme_run import scheme_run_service

router = APIRouter(prefix="/api/decision/scheme-analysis", tags=["方案分析"])


@router.post("/dry-run")
def dry_run(payload: SchemeAnalysisPayload) -> dict:
    """试算方案分析：不落库，返回完整结论数据集或逐项错误定位。"""
    return scheme_run_service.dry_run(payload.model_dump())


@router.post("/runs")
def create_run(payload: SchemeAnalysisPayload) -> dict:
    """发起正式分析并保存批次；硬错误返回 422 且不产生任何批次或排名。"""
    result = scheme_run_service.create_run(payload.model_dump())
    if result["status"] == engine.STATUS_FAILED:
        raise HTTPException(status_code=422, detail={
            "message": "方案分析计算失败，未发布排名，请按错误定位修订输入后重新发起",
            "errors": result["errors"],
            "warnings": result.get("warnings", []),
        })
    return result


@router.get("/runs")
def list_runs() -> dict:
    """列出已出具的分析批次（含派生批次），按存储顺序返回。"""
    items = scheme_run_service.list_runs()
    return {"items": items, "total": len(items)}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    """读取单个批次的完整结论数据集；批次不存在时给出可读错误。"""
    result = scheme_run_service.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"方案分析批次 {run_id} 不存在或已归档")
    return result


@router.post("/runs/{run_id}/reweight")
def reweight(run_id: str, payload: ReweightPayload) -> dict:
    """在同一批次输入上调整有明确含义的权重，返回派生批次与排名是否翻转。

    原批次保持不变；权重非法时返回 422 并说明原因，不生成派生批次。
    """
    result, message = scheme_run_service.reweight(run_id, payload.weights)
    if result is None:
        raise HTTPException(status_code=404, detail=message)
    if result.get("status") == engine.STATUS_FAILED:
        raise HTTPException(status_code=422, detail={
            "message": "权重调整后计算失败，未生成派生批次",
            "errors": result["errors"],
        })
    baseline = scheme_run_service.get_run(run_id)
    result["ranking_flipped"] = (
        baseline is not None
        and baseline.get("sensitivity", {}).get("baseline_order")
        != result.get("sensitivity", {}).get("baseline_order")
    )
    return result


@router.get("/runs/{run_id}/report-dataset")
def report_dataset(run_id: str) -> dict:
    """报告端取数：返回该批次自身的方案对比结论数据集（覆盖、权重、转换依据、敏感区间）。"""
    dataset = scheme_run_service.report_dataset(run_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"方案分析批次 {run_id} 不存在或已归档")
    return dataset
