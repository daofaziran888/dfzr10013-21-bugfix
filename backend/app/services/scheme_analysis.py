"""方案分析引擎：读取决策选项的情景收益、成本与风险明细，统一量纲后计算情景结果，
生成方案对比结论数据集（覆盖、排序、敏感性与稳健结论）。

设计约束（对应业务规则）：
- 尚无可靠估计的项以 status=missing 单独留痕，绝不按零值参与计算；
- 只在「共同可比情景」与「共同风险指标」内打分，不对缺失项重新归一权重，
  避免资料不全的方案反而得分更高；
- 币种、周期转换依据逐条留痕，可复核；
- 概率不闭合、币种/周期不兼容、同情景重复行分别定位到具体选项与情景；
- 计算失败不产出部分情景的完整排名；输入修订只生成新批次，不回写已出具的分析；
- 排序与敏感性同批绑定，敏感性结果指出导致名次翻转的具体情景与权重。
"""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

# --------------------------------------------------------------------------- 常量

SCENARIO_CANONICAL = {
    "pessimistic": "pessimistic",
    "neutral": "neutral",
    "optimistic": "optimistic",
    "悲观": "pessimistic",
    "悲观情景": "pessimistic",
    "中性": "neutral",
    "中性情景": "neutral",
    "乐观": "optimistic",
    "乐观情景": "optimistic",
}
SCENARIO_LABELS = {
    "pessimistic": "悲观情景",
    "neutral": "中性情景",
    "optimistic": "乐观情景",
}

PERIOD_FACTORS_TO_MONTH = {
    "month": 1.0,
    "months": 1.0,
    "月": 1.0,
    "月数": 1.0,
    "quarter": 3.0,
    "quarters": 3.0,
    "季度": 3.0,
    "year": 12.0,
    "years": 12.0,
    "年": 12.0,
}

# 纯金额单位（1 单位 = 多少基本单位），不带币种时默认按基准币种；币种间汇率走 currency_rates。
CURRENCY_UNIT_FACTOR = {
    "CNY": 1.0,
    "RMB": 1.0,
    "元": 1.0,
    "USD": 1.0,
    "EUR": 1.0,
}
# 金额单位后缀 → 基本单位倍数
UNIT_SUFFIX_FACTOR = {"万元": 10_000.0}
# 固定写法 → (隐含币种, 倍数)
NAMED_UNIT = {"千美元": ("USD", 1_000.0)}

DIM_EXPECTED = "expected_return"
DIM_DOWNSIDE = "downside_protection"
DIM_RISK = "risk_control"
DIMENSIONS = [DIM_EXPECTED, DIM_DOWNSIDE, DIM_RISK]
DEFAULT_WEIGHTS = {
    DIM_EXPECTED: 0.5,
    DIM_DOWNSIDE: 0.25,
    DIM_RISK: 0.25,
}
DIMENSION_LABELS = {
    DIM_EXPECTED: "期望净收益（月均）",
    DIM_DOWNSIDE: "下行保护（悲观情景净收益）",
    DIM_RISK: "风险控制（风险指数反向）",
}

PROB_HARD_TOL = 0.01        # 偏差超过 1% 视为概率不闭合硬错误
PROB_ROUND_TOL = 1e-6       # 1e-6 以内视为浮点误差，不提示
WEIGHT_TOL = 1e-6
EPS = 1e-12
SWEEP_STEPS = 21            # 权重/概率扫描格点

STATUS_RANKABLE = "rankable"        # 存在共同可比情景下的完整排名
STATUS_UNRANKABLE = "unrankable"    # 完整方案不足，只能出缺口说明，不出整体排名
STATUS_FAILED = "failed"            # 硬错误：未发布任何结论数据集


# --------------------------------------------------------------------------- 工具函数


def canonical_scenario(raw: str) -> str | None:
    text = str(raw or "").strip()
    return SCENARIO_CANONICAL.get(text.lower()) or SCENARIO_CANONICAL.get(text)


def period_factor(unit: str | None) -> float | None:
    if unit is None:
        return 1.0
    text = str(unit).strip()
    return PERIOD_FACTORS_TO_MONTH.get(text.lower()) or PERIOD_FACTORS_TO_MONTH.get(text)


def fx_rate(code: str, base_currency: str, rates: dict[str, float]) -> float | None:
    code = code.strip()
    if code == base_currency:
        return 1.0
    rate = rates.get(code)
    return float(rate) if rate is not None else None


def currency_factor(
    currency: str | None, base_currency: str, rates: dict[str, float]
) -> tuple[float | None, str | None]:
    """返回 (折算到基准币基本单位的系数, 错误码)。

    支持三种写法：
    - 纯币种（CNY/USD/EUR）：按 currency_rates 折算；
    - 固定金额单位（千美元）：按隐含币种结合汇率，再乘单位倍数；
    - 币种+单位后缀（USD万元/CNY万元/万元）：拆出币种后乘汇率与单位倍数，
      纯单位「万元」不带币种，按基准币种处理。
    """
    raw = str(currency or base_currency).strip()
    if raw in NAMED_UNIT:
        code, unit_factor = NAMED_UNIT[raw]
        rate = fx_rate(code, base_currency, rates)
        if rate is None:
            return None, "CURRENCY_INCOMPATIBLE"
        return rate * unit_factor, None
    if raw in CURRENCY_UNIT_FACTOR:
        return fx_rate(raw, base_currency, rates), None
    for suffix, unit_factor in UNIT_SUFFIX_FACTOR.items():
        if raw.endswith(suffix):
            code = raw[: -len(suffix)].strip() or base_currency
            rate = fx_rate(code, base_currency, rates)
            if rate is None:
                return None, "CURRENCY_INCOMPATIBLE"
            return rate * unit_factor, None
    rate = fx_rate(raw, base_currency, rates)
    if rate is None:
        return None, "CURRENCY_INCOMPATIBLE"
    return rate, None


def is_missing(row: dict[str, Any]) -> bool:
    return str(row.get("status", "estimated")).strip().lower() == "missing"


def round6(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return value
    return round(float(value), 6)


# --------------------------------------------------------------------------- 输入校验


def validate_input(
    payload: dict[str, Any], rates: dict[str, float]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, float]]:
    """校验原始输入，返回 (硬错误, 提示项, 归一权重)。

    硬错误定位到具体选项/情景/风险指标；提示项不阻断计算，但会进入结论数据集留痕。
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    options = payload.get("options") or []
    option_ids = {str(o.get("option_id")) for o in options}
    scenarios = payload.get("scenarios") or []
    risks = payload.get("risks") or []
    base_currency = str(payload.get("base_currency") or "CNY")

    # 权重：只允许有明确含义的三个维度，且必须归一
    weights = dict(DEFAULT_WEIGHTS)
    raw_weights = payload.get("weights")
    if raw_weights:
        unknown = [k for k in raw_weights if k not in DIMENSIONS]
        if unknown:
            errors.append({
                "code": "WEIGHT_UNKNOWN_DIMENSION",
                "message": f"权重维度 {unknown} 无明确业务含义，允许的维度为 {DIMENSIONS}",
                "keys": unknown,
            })
        weight_invalid = False
        for key, value in raw_weights.items():
            if key not in DIMENSIONS:
                continue
            try:
                fv = float(value)
            except (TypeError, ValueError):
                errors.append({"code": "WEIGHT_INVALID", "message": f"权重 {key} 不是数值", "key": key})
                weight_invalid = True
                continue
            if fv < 0:
                errors.append({"code": "WEIGHT_INVALID", "message": f"权重 {key} 不能为负", "key": key})
                weight_invalid = True
            weights[key] = fv
        if not weight_invalid:
            total = sum(weights.values())
            if abs(total - 1.0) > WEIGHT_TOL:
                errors.append({
                    "code": "WEIGHT_NOT_CLOSED",
                    "message": f"权重之和为 {round6(total)}，不归一（容差 {WEIGHT_TOL}）",
                    "sum": round6(total),
                })

    # 情景行：重复行、未知选项、非法情景、概率闭合、币种周期兼容
    seen_scenario: set[tuple[str, str]] = set()
    prob_by_option: dict[str, list[float]] = {}
    estimated_scenarios_by_option: dict[str, set[str]] = {}
    missing_options: set[str] = set()
    for idx, row in enumerate(scenarios):
        oid = str(row.get("option_id"))
        loc = {"index": idx, "option_id": oid, "scenario": row.get("scenario")}
        if oid not in option_ids:
            errors.append({"code": "UNKNOWN_OPTION", "message": f"情景行引用了不存在的选项 {oid}", **loc})
            continue
        canonical = canonical_scenario(str(row.get("scenario") or ""))
        if canonical is None:
            errors.append({
                "code": "INVALID_SCENARIO",
                "message": f"情景名称「{row.get('scenario')}」无法识别，应为悲观/中性/乐观",
                **loc,
            })
            continue
        if (oid, canonical) in seen_scenario:
            errors.append({
                "code": "DUPLICATE_SCENARIO",
                "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}存在重复行",
                **loc,
            })
            continue
        seen_scenario.add((oid, canonical))
        if is_missing(row):
            missing_options.add(oid)
            continue
        estimated_scenarios_by_option.setdefault(oid, set()).add(canonical)
        factor, err = currency_factor(row.get("currency"), base_currency, rates)
        if err:
            errors.append({
                "code": "CURRENCY_INCOMPATIBLE",
                "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}币种「{row.get('currency')}」无折算依据",
                **loc,
            })
        pfactor = period_factor(row.get("period_unit"))
        if pfactor is None:
            errors.append({
                "code": "PERIOD_INCOMPATIBLE",
                "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}周期单位「{row.get('period_unit')}」不兼容",
                **loc,
            })
        if row.get("period") is not None:
            try:
                if float(row["period"]) <= 0:
                    errors.append({
                        "code": "PERIOD_INCOMPATIBLE",
                        "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}周期长度必须为正数",
                        **loc,
                    })
            except (TypeError, ValueError):
                errors.append({
                    "code": "PERIOD_INCOMPATIBLE",
                    "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}周期长度不是数值",
                    **loc,
                })
        prob = row.get("probability")
        if prob is not None:
            try:
                fprob = float(prob)
                if fprob < 0 or fprob > 1:
                    errors.append({
                        "code": "PROB_NOT_CLOSED",
                        "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}概率 {fprob} 超出 [0,1]",
                        **loc,
                    })
                else:
                    prob_by_option.setdefault(oid, []).append(fprob)
            except (TypeError, ValueError):
                errors.append({
                    "code": "PROB_NOT_CLOSED",
                    "message": f"选项 {oid} 的{SCENARIO_LABELS[canonical]}概率不是数值",
                    **loc,
                })

    # 概率闭合：逐选项判断。存在缺失/未登记情景时闭合性无法核验，按覆盖缺口提示而非硬错误；
    # 只有情景全部已填却不闭合才是数据质量硬错误（防止把资料不全误判为计算失败）。
    all_canonical = set(SCENARIO_LABELS)
    for oid, probs in prob_by_option.items():
        total = sum(probs)
        gap = abs(total - 1.0)
        has_gap = oid in missing_options or estimated_scenarios_by_option.get(oid, set()) != all_canonical
        if has_gap:
            if gap > PROB_ROUND_TOL:
                warnings.append({
                    "code": "PROB_CLOSURE_UNVERIFIABLE",
                    "message": f"选项 {oid} 存在缺失或未登记情景，概率闭合性无法核验（已填概率和 {round6(total)}），"
                               "缺失项未按零值处理，该选项不参与整体排名",
                    "option_id": oid,
                    "sum": round6(total),
                })
            continue
        if gap > PROB_HARD_TOL:
            errors.append({
                "code": "PROB_NOT_CLOSED",
                "message": f"选项 {oid} 情景已填齐但概率之和为 {round6(total)}，与 1 的偏差 {round6(gap)} 超过 {PROB_HARD_TOL}",
                "option_id": oid,
                "sum": round6(total),
                "deviation": round6(gap),
            })
        elif gap > PROB_ROUND_TOL:
            warnings.append({
                "code": "PROB_DECIMAL_ROUNDING",
                "message": f"选项 {oid} 概率之和为 {round6(total)}，存在小数舍入误差（偏差 {round6(gap)}），按原值保留不归一化",
                "option_id": oid,
                "sum": round6(total),
                "deviation": round6(gap),
            })

    # 风险行：未知选项、重复计入、量度类型、金额币种兼容
    seen_risk: set[tuple[str, str]] = set()
    for idx, row in enumerate(risks):
        oid = str(row.get("option_id"))
        metric = str(row.get("metric") or "").strip()
        loc = {"index": idx, "option_id": oid, "metric": metric}
        if oid not in option_ids:
            errors.append({"code": "UNKNOWN_OPTION", "message": f"风险行引用了不存在的选项 {oid}", **loc})
            continue
        if not metric:
            errors.append({"code": "RISK_METRIC_EMPTY", "message": f"选项 {oid} 存在未命名的风险指标", **loc})
            continue
        if is_missing(row):
            continue
        if (oid, metric) in seen_risk:
            errors.append({
                "code": "RISK_DUPLICATE",
                "message": f"选项 {oid} 的风险指标「{metric}」重复登记，存在相关风险重复计入风险",
                **loc,
            })
            continue
        seen_risk.add((oid, metric))
        measure = str(row.get("measure", "score")).strip().lower()
        if measure not in ("probability", "score", "amount"):
            errors.append({
                "code": "RISK_MEASURE_UNKNOWN",
                "message": f"风险量度类型「{measure}」不支持，应为 probability/score/amount",
                **loc,
            })
        if measure == "amount":
            _, err = currency_factor(row.get("currency"), base_currency, rates)
            if err:
                errors.append({
                    "code": "CURRENCY_INCOMPATIBLE",
                    "message": f"选项 {oid} 的风险指标「{metric}」金额币种「{row.get('currency')}」无折算依据",
                    **loc,
                })

    if not options:
        errors.append({"code": "NO_OPTIONS", "message": "至少需要一个决策选项"})
    return errors, warnings, weights


# --------------------------------------------------------------------------- 量纲转换与情景计算


def convert_scenario_row(
    row: dict[str, Any], base_currency: str, rates: dict[str, float]
) -> dict[str, Any]:
    """把单条情景行转换为基准币种、月均口径，转换依据全程留痕。"""
    canonical = canonical_scenario(str(row.get("scenario") or ""))
    out: dict[str, Any] = {
        "option_id": str(row.get("option_id")),
        "scenario": canonical,
        "status": "missing" if is_missing(row) else "estimated",
        "raw": {
            "probability": row.get("probability"),
            "benefit": row.get("benefit"),
            "cost": row.get("cost"),
            "currency": row.get("currency"),
            "period": row.get("period"),
            "period_unit": row.get("period_unit"),
        },
    }
    if out["status"] == "missing":
        out["converted"] = None
        out["conversion_basis"] = None
        return out

    c_factor, _ = currency_factor(row.get("currency"), base_currency, rates)
    pfactor = period_factor(row.get("period_unit")) or 1.0
    months = max(float(row.get("period") or 1.0) * pfactor, EPS)

    benefit = row.get("benefit")
    cost = row.get("cost")
    benefit_base = float(benefit) * c_factor if benefit is not None and c_factor else None
    cost_base = float(cost) * c_factor if cost is not None and c_factor else None
    net_base = None
    if benefit_base is not None or cost_base is not None:
        net_base = (benefit_base or 0.0) - (cost_base or 0.0)

    out["converted"] = {
        "currency_factor": round6(c_factor),
        "period_factor_to_month": round6(pfactor),
        "period_months": round6(months),
        "benefit_base": round6(benefit_base),
        "cost_base": round6(cost_base),
        "net_base_total": round6(net_base),
        "net_base_per_month": round6(net_base / months) if net_base is not None else None,
        "base_currency": base_currency,
        "base_period_unit": "month",
    }
    out["conversion_basis"] = {
        "currency": {
            "from": row.get("currency") or base_currency,
            "to": base_currency,
            "factor": round6(c_factor),
            "rate_source": "payload.currency_rates"
            if str(row.get("currency") or base_currency) != base_currency
            else "base",
        },
        "period": {
            "from_unit": row.get("period_unit") or "month",
            "to_unit": "month",
            "factor": round6(pfactor),
            "period_input": row.get("period"),
            "period_months": round6(months),
        },
    }
    return out


def convert_risk_row(row: dict[str, Any], base_currency: str, rates: dict[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "option_id": str(row.get("option_id")),
        "metric": str(row.get("metric") or "").strip(),
        "measure": str(row.get("measure", "score")).strip().lower(),
        "correlation_group": row.get("correlation_group"),
        "status": "missing" if is_missing(row) else "estimated",
        "raw": {"value": row.get("value"), "currency": row.get("currency")},
    }
    if out["status"] == "missing":
        out["converted"] = None
        return out
    value = row.get("value")
    converted: dict[str, Any] = {"value": float(value) if value is not None else None}
    if out["measure"] == "amount":
        factor, _ = currency_factor(row.get("currency"), base_currency, rates)
        converted["currency_factor"] = round6(factor)
        converted["value_base"] = round6(float(value) * factor) if value is not None and factor else None
    out["converted"] = converted
    return out


# --------------------------------------------------------------------------- 主计算


def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次完整方案分析，返回可序列化的结论数据集。"""
    rates = dict(payload.get("currency_rates") or {})
    base_currency = str(payload.get("base_currency") or "CNY")
    base_period_unit = str(payload.get("base_period_unit") or "month")
    if period_factor(base_period_unit) is None:
        base_period_unit = "month"

    errors, warnings, weights = validate_input(payload, rates)
    options = [
        {"option_id": str(o.get("option_id")), "name": o.get("name")}
        for o in payload.get("options") or []
    ]

    if errors:
        # 计算失败：不发布任何带排名的结论数据集，只回错误定位，调用方不落库
        return {
            "status": STATUS_FAILED,
            "rankable": False,
            "subject": payload.get("subject"),
            "base_currency": base_currency,
            "base_period_unit": base_period_unit,
            "errors": errors,
            "warnings": warnings,
            "ranking": [],
        }

    converted_scenarios = [
        convert_scenario_row(dict(row), base_currency, rates)
        for row in (payload.get("scenarios") or [])
    ]
    converted_risks = [
        convert_risk_row(dict(row), base_currency, rates)
        for row in (payload.get("risks") or [])
    ]

    by_option: dict[str, list[dict[str, Any]]] = {o["option_id"]: [] for o in options}
    for row in converted_scenarios:
        by_option.setdefault(row["option_id"], []).append(row)

    union_scenarios = sorted({
        row["scenario"] for rows in by_option.values() for row in rows if row["scenario"]
    })
    # 共同可比情景：每个选项都有「已估计」行的情景
    common_scenarios = [
        s for s in union_scenarios
        if all(
            any(r["scenario"] == s and r["status"] == "estimated"
                for r in by_option.get(o["option_id"], []))
            for o in options
        )
    ]

    option_calc: dict[str, dict[str, Any]] = {}
    for opt in options:
        oid = opt["option_id"]
        rows = by_option.get(oid, [])
        estimated = [r for r in rows if r["status"] == "estimated"]
        missing_rows = [r for r in rows if r["status"] == "missing"]
        covered = [r["scenario"] for r in estimated]
        uncovered: list[dict[str, Any]] = [
            {
                "type": "scenario",
                "scenario": r["scenario"],
                "label": SCENARIO_LABELS.get(r["scenario"], r["scenario"]),
                "reason": "尚无可靠估计（missing），未按零值处理",
            }
            for r in missing_rows
        ]
        for s in union_scenarios:
            if s not in covered:
                uncovered.append({
                    "type": "scenario",
                    "scenario": s,
                    "label": SCENARIO_LABELS.get(s, s),
                    "reason": "未登记该情景数据，未按零值处理",
                })

        scenario_results: dict[str, dict[str, Any]] = {}
        prob_sum = 0.0
        expected_per_month = 0.0
        expected_total = 0.0
        for r in estimated:
            conv = r["converted"]
            prob_raw = r["raw"]["probability"]
            prob = float(prob_raw) if prob_raw is not None else None
            if prob is not None:
                prob_sum += prob
            scenario_results[r["scenario"]] = {
                "probability": prob,
                "net_base_total": conv["net_base_total"],
                "net_base_per_month": conv["net_base_per_month"],
                "benefit_base": conv["benefit_base"],
                "cost_base": conv["cost_base"],
                "period_months": conv["period_months"],
            }
            if prob is not None and conv["net_base_per_month"] is not None:
                expected_per_month += prob * conv["net_base_per_month"]
                expected_total += prob * conv["net_base_total"]

        complete = (
            bool(union_scenarios)
            and sorted(covered) == sorted(union_scenarios)
            and abs(prob_sum - 1.0) <= PROB_HARD_TOL
        )
        option_calc[oid] = {
            "option_id": oid,
            "name": opt["name"],
            "covered_scenarios": covered,
            "uncovered": uncovered,
            "scenario_results": scenario_results,
            "probability_sum": round6(prob_sum),
            "expected_per_month": round6(expected_per_month) if estimated else None,
            "expected_total": round6(expected_total) if estimated else None,
            "scenario_complete": complete,
        }

    # 风险归集：相关风险（同 correlation_group）按组内最大值合并，不重复计入
    risk_by_option: dict[str, list[dict[str, Any]]] = {o["option_id"]: [] for o in options}
    for r in converted_risks:
        risk_by_option.setdefault(r["option_id"], []).append(r)
    risk_metric_union = sorted({
        r["metric"] for rows in risk_by_option.values() for r in rows if r["metric"]
    })
    common_risk_metrics = [
        m for m in risk_metric_union
        if all(
            any(r["metric"] == m and r["status"] == "estimated"
                for r in risk_by_option.get(o["option_id"], []))
            for o in options
        )
    ]

    for opt in options:
        oid = opt["option_id"]
        rows = [r for r in risk_by_option.get(oid, []) if r["status"] == "estimated"]
        missing_risk = [r for r in risk_by_option.get(oid, []) if r["status"] == "missing"]
        groups: dict[str, list[tuple[str, float]]] = {}
        standalone: list[tuple[str, float]] = []
        for r in rows:
            val = r["converted"].get("value_base") if r["measure"] == "amount" else r["converted"].get("value")
            if val is None:
                continue
            g = r.get("correlation_group")
            if g:
                groups.setdefault(str(g), []).append((r["metric"], float(val)))
            else:
                standalone.append((r["metric"], float(val)))
        merged: dict[str, float] = {metric: val for metric, val in standalone}
        merge_trace: list[dict[str, Any]] = []
        for g, members in groups.items():
            chosen_metric, chosen_val = max(members, key=lambda x: x[1])
            merged[f"group:{g}"] = chosen_val
            merge_trace.append({
                "correlation_group": g,
                "members": [m for m, _ in members],
                "rule": "相关风险按组内最大值合并，不重复计入",
                "selected_metric": chosen_metric,
                "selected_value": round6(chosen_val),
            })
        option_calc[oid]["risk_merged"] = {k: round6(v) for k, v in merged.items()}
        option_calc[oid]["risk_merge_trace"] = merge_trace
        uncovered_risk = [
            {"type": "risk", "metric": r["metric"],
             "reason": "风险量度尚无可靠估计（missing），未按零风险处理"}
            for r in missing_risk
        ]
        for m in risk_metric_union:
            if not any(r["metric"] == m for r in rows):
                uncovered_risk.append({
                    "type": "risk", "metric": m,
                    "reason": "未登记该风险指标，未按零风险处理",
                })
        option_calc[oid]["uncovered"].extend(uncovered_risk)
        option_calc[oid]["risk_complete"] = bool(risk_metric_union) and all(
            any(r["metric"] == m and r["status"] == "estimated"
                for r in risk_by_option.get(oid, []))
            for m in risk_metric_union
        )

    complete_options = [
        oid for oid, calc in option_calc.items()
        if calc["scenario_complete"] and calc["risk_complete"]
    ]

    ranking: list[dict[str, Any]] = []
    sensitivity: dict[str, Any] = {
        "weight_sweeps": [], "probability_sweeps": [], "flip_events": [],
        "baseline_order": [],
    }
    rankable = len(complete_options) >= 2 and bool(common_scenarios)

    if rankable:
        # 逐共同指标 min-max 归一（amount 已折基准币；probability/score 同口径并列归一）
        metric_values: dict[str, list[float]] = {}
        for oid in complete_options:
            merged = option_calc[oid]["risk_merged"]
            for m in common_risk_metrics:
                val = _risk_metric_value(m, oid, merged, risk_by_option)
                if val is not None:
                    metric_values.setdefault(m, []).append(float(val))
        normalized_risk: dict[str, float] = {}
        for oid in complete_options:
            merged = option_calc[oid]["risk_merged"]
            parts: list[float] = []
            for m, vals in metric_values.items():
                val = _risk_metric_value(m, oid, merged, risk_by_option)
                if val is None:
                    continue
                lo, hi = min(vals), max(vals)
                parts.append(0.0 if hi - lo <= EPS else (float(val) - lo) / (hi - lo))
            normalized_risk[oid] = sum(parts) / len(parts) if parts else 0.0

        raw_dims = {
            oid: {
                DIM_EXPECTED: option_calc[oid]["expected_per_month"] or 0.0,
                DIM_DOWNSIDE: (
                    option_calc[oid]["scenario_results"]["pessimistic"]["net_base_per_month"]
                    if "pessimistic" in common_scenarios
                    else min(
                        v["net_base_per_month"] or 0.0
                        for v in option_calc[oid]["scenario_results"].values()
                    )
                ),
                DIM_RISK: -normalized_risk[oid],
            }
            for oid in complete_options
        }

        def minmax(values: list[float]) -> dict[float, float]:
            lo, hi = min(values), max(values)
            if hi - lo <= EPS:
                return {v: 0.5 for v in values}
            return {v: (v - lo) / (hi - lo) for v in values}

        dim_norm: dict[str, dict[str, float]] = {dim: {} for dim in DIMENSIONS}
        for dim in DIMENSIONS:
            mapped = minmax([raw_dims[oid][dim] for oid in complete_options])
            for oid in complete_options:
                dim_norm[dim][oid] = mapped[raw_dims[oid][dim]]

        def composite(oid: str, w: dict[str, float]) -> float:
            return sum(w[dim] * dim_norm[dim][oid] for dim in DIMENSIONS)

        scores = {oid: composite(oid, weights) for oid in complete_options}
        ordered = sorted(complete_options, key=lambda o: (-scores[o], o))
        for rank, oid in enumerate(ordered, start=1):
            calc = option_calc[oid]
            ranking.append({
                "rank": rank,
                "option_id": oid,
                "name": calc["name"],
                "composite_score": round6(scores[oid]),
                "dimension_scores": {dim: round6(dim_norm[dim][oid]) for dim in DIMENSIONS},
                "dimension_raw": {
                    DIM_EXPECTED: round6(raw_dims[oid][DIM_EXPECTED]),
                    DIM_DOWNSIDE: round6(raw_dims[oid][DIM_DOWNSIDE]),
                    DIM_RISK: round6(-raw_dims[oid][DIM_RISK]),
                },
                "expected_per_month": calc["expected_per_month"],
                "expected_total": calc["expected_total"],
                "coverage": {
                    "scenarios_covered": calc["covered_scenarios"],
                    "scenarios_uncovered": calc["uncovered"],
                    "risk_complete": calc["risk_complete"],
                },
            })
        sensitivity = build_sensitivity(
            complete_options, ordered, weights, dim_norm, option_calc, common_scenarios
        )

    comparison_table = []
    for s in common_scenarios:
        row: dict[str, Any] = {"scenario": s, "label": SCENARIO_LABELS.get(s, s)}
        for opt in options:
            row[opt["option_id"]] = option_calc[opt["option_id"]]["scenario_results"].get(s)
        comparison_table.append(row)

    coverage_summary = []
    for opt in options:
        calc = option_calc[opt["option_id"]]
        coverage_summary.append({
            "option_id": opt["option_id"],
            "name": opt["name"],
            "scenario_complete": calc["scenario_complete"],
            "risk_complete": calc["risk_complete"],
            "covered_scenarios": calc["covered_scenarios"],
            "uncovered_items": calc["uncovered"],
            "included_in_ranking": opt["option_id"] in complete_options and rankable,
        })

    checks = build_checks(options, option_calc, warnings, sensitivity, rankable)
    conclusion = build_conclusion(ranking, rankable, coverage_summary, sensitivity, weights)

    return {
        "status": STATUS_RANKABLE if rankable else STATUS_UNRANKABLE,
        "rankable": rankable,
        "subject": payload.get("subject"),
        "base_currency": base_currency,
        "base_period_unit": base_period_unit,
        "weights": weights,
        "weight_labels": DIMENSION_LABELS,
        "options": options,
        "common_scenarios": common_scenarios,
        "union_scenarios": union_scenarios,
        "common_risk_metrics": common_risk_metrics,
        "converted_scenarios": converted_scenarios,
        "converted_risks": converted_risks,
        "option_calculations": option_calc,
        "comparison_table": comparison_table,
        "coverage_summary": coverage_summary,
        "ranking": ranking,
        "sensitivity": sensitivity,
        "checks": checks,
        "warnings": warnings,
        "errors": [],
        "conversion_basis": [
            r["conversion_basis"] for r in converted_scenarios if r.get("conversion_basis")
        ],
        "conclusion": conclusion,
    }


def _risk_metric_value(
    metric: str,
    oid: str,
    merged: dict[str, float],
    risk_by_option: dict[str, list[dict[str, Any]]],
) -> float | None:
    if metric in merged:
        return merged[metric]
    for r in risk_by_option.get(oid, []):
        if r["metric"] == metric and r.get("correlation_group"):
            gk = f"group:{r['correlation_group']}"
            if gk in merged:
                return merged[gk]
    return None


# --------------------------------------------------------------------------- 敏感性分析


def build_sensitivity(
    complete_options: list[str],
    baseline_order: list[str],
    weights: dict[str, float],
    dim_norm: dict[str, dict[str, float]],
    option_calc: dict[str, dict[str, Any]],
    common_scenarios: list[str],
) -> dict[str, Any]:
    def order_for(w: dict[str, float]) -> list[str]:
        scores = {
            oid: sum(w[dim] * dim_norm[dim][oid] for dim in DIMENSIONS)
            for oid in complete_options
        }
        return sorted(complete_options, key=lambda o: (-scores[o], o))

    baseline_top = baseline_order[0]
    weight_sweeps: list[dict[str, Any]] = []
    flip_events: list[dict[str, Any]] = []

    # 单维度权重扫描：目标维度在 [0,1] 变化，其余两维按原比例分配剩余权重
    for target in DIMENSIONS:
        others = [d for d in DIMENSIONS if d != target]
        others_sum = sum(weights[d] for d in others)
        grid: list[dict[str, Any]] = []
        first_flip: dict[str, Any] | None = None
        for step in range(SWEEP_STEPS):
            t = step / (SWEEP_STEPS - 1)
            w = {target: t}
            if others_sum > EPS:
                for d in others:
                    w[d] = (1.0 - t) * weights[d] / others_sum
            else:
                for d in others:
                    w[d] = (1.0 - t) / len(others)
            order = order_for(w)
            top = order[0]
            grid.append({
                "weight": round6(t),
                "weights": {k: round6(v) for k, v in w.items()},
                "order": order,
                "top_option": top,
                "flipped": top != baseline_top,
            })
            if top != baseline_top and first_flip is None:
                first_flip = {
                    "type": "weight_flip",
                    "dimension": target,
                    "dimension_label": DIMENSION_LABELS[target],
                    "at_weight": round6(t),
                    "weights": {k: round6(v) for k, v in w.items()},
                    "baseline_top": baseline_top,
                    "new_top": top,
                    "message": f"{DIMENSION_LABELS[target]}权重调整到 {round6(t)} 时，"
                               f"第一名由 {baseline_top} 变为 {top}",
                }
        flip_points = [g["weight"] for g in grid if g["flipped"]]
        weight_sweeps.append({
            "dimension": target,
            "dimension_label": DIMENSION_LABELS[target],
            "grid": grid,
            "flip_interval": [min(flip_points), max(flip_points)] if flip_points else None,
            "first_flip": first_flip,
        })
        if first_flip:
            flip_events.append(first_flip)

    # 情景概率扫描：目标情景概率在 ±10% 绝对扰动，其余情景等比缩放保持闭合
    probability_sweeps: list[dict[str, Any]] = []
    for scenario in common_scenarios:
        grid = []
        first_flip = None
        for step in range(SWEEP_STEPS):
            delta = -0.10 + 0.20 * step / (SWEEP_STEPS - 1)
            trial_expected: dict[str, float] = {}
            for oid in complete_options:
                sr = option_calc[oid]["scenario_results"]
                probs = {s: sr[s]["probability"] for s in common_scenarios}
                if probs.get(scenario) is None:
                    continue
                p_new = min(max(probs[scenario] + delta, 0.0), 1.0)
                rest = [s for s in common_scenarios if s != scenario]
                rest_sum = sum(probs[s] for s in rest)
                adj = dict(probs)
                adj[scenario] = p_new
                if rest_sum > EPS:
                    for s in rest:
                        adj[s] = probs[s] * (1.0 - p_new) / rest_sum
                trial_expected[oid] = sum(
                    adj[s] * (sr[s]["net_base_per_month"] or 0.0) for s in common_scenarios
                )
            if len(trial_expected) != len(complete_options):
                continue
            order = sorted(complete_options, key=lambda o: (-trial_expected[o], o))
            top = order[0]
            grid.append({
                "delta": round6(delta),
                "order": order,
                "top_option": top,
                "flipped": top != baseline_top,
            })
            if top != baseline_top and first_flip is None:
                first_flip = {
                    "type": "probability_flip",
                    "scenario": scenario,
                    "scenario_label": SCENARIO_LABELS.get(scenario, scenario),
                    "at_delta": round6(delta),
                    "baseline_top": baseline_top,
                    "new_top": top,
                    "message": f"{SCENARIO_LABELS.get(scenario, scenario)}概率扰动 {round6(delta):+.2f} 时，"
                               f"第一名由 {baseline_top} 变为 {top}",
                }
        flip_points = [g["delta"] for g in grid if g["flipped"]]
        probability_sweeps.append({
            "scenario": scenario,
            "scenario_label": SCENARIO_LABELS.get(scenario, scenario),
            "grid": grid,
            "flip_interval": [min(flip_points), max(flip_points)] if flip_points else None,
            "first_flip": first_flip,
        })
        if first_flip:
            flip_events.append(first_flip)

    fragile = any(
        sweep["first_flip"] is not None
        and abs(sweep["first_flip"]["at_weight"] - weights[sweep["dimension"]]) <= 0.05
        for sweep in weight_sweeps
    )
    return {
        "weight_sweeps": weight_sweeps,
        "probability_sweeps": probability_sweeps,
        "flip_events": flip_events,
        "fragile_to_small_weight_change": fragile,
        "baseline_order": baseline_order,
    }


# --------------------------------------------------------------------------- 核对项与结论


def build_checks(
    options: list[dict[str, Any]],
    option_calc: dict[str, dict[str, Any]],
    warnings: list[dict[str, Any]],
    sensitivity: dict[str, Any],
    rankable: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for opt in options:
        oid = opt["option_id"]
        calc = option_calc[oid]
        if "pessimistic" not in calc["covered_scenarios"]:
            checks.append({
                "code": "MISSING_WORST_CASE",
                "level": "warning",
                "option_id": oid,
                "message": f"选项 {oid}（{calc['name']}）缺少悲观情景，下行风险无法完整评估，结论需标注假设依赖",
            })
        for trace in calc["risk_merge_trace"]:
            checks.append({
                "code": "CORRELATED_RISK_MERGED",
                "level": "info",
                "option_id": oid,
                "message": f"选项 {oid} 相关风险组 {trace['correlation_group']} 已按最大值合并"
                           f"（{trace['members']}），未重复计入",
                "detail": trace,
            })
    for w in warnings:
        if w["code"] == "PROB_DECIMAL_ROUNDING":
            checks.append({
                "code": "PROB_DECIMAL_ROUNDING",
                "level": "warning",
                "option_id": w.get("option_id"),
                "message": w["message"],
            })
        if w["code"] == "PROB_CLOSURE_UNVERIFIABLE":
            checks.append({
                "code": "PROB_CLOSURE_UNVERIFIABLE",
                "level": "warning",
                "option_id": w.get("option_id"),
                "message": w["message"],
            })
    if rankable and sensitivity.get("fragile_to_small_weight_change"):
        checks.append({
            "code": "FLIP_ON_SLIGHT_WEIGHT_CHANGE",
            "level": "critical",
            "message": "权重轻微变动（±0.05 以内）即出现名次翻转，推荐结论依赖权重假设，不能视为稳健结论",
            "flip_events": sensitivity["flip_events"],
        })
    return checks


def build_conclusion(
    ranking: list[dict[str, Any]],
    rankable: bool,
    coverage_summary: list[dict[str, Any]],
    sensitivity: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    if not rankable:
        gaps = [
            {"option_id": c["option_id"], "name": c["name"], "uncovered_items": c["uncovered_items"]}
            for c in coverage_summary
            if not c["included_in_ranking"]
        ]
        return {
            "recommendation_type": "unrankable",
            "headline": "共同可比情景或完整方案不足，本次不出具整体排名，仅提供缺口说明",
            "robust": False,
            "assumption_dependent": True,
            "gaps": gaps,
            "note": "缺失项未按零值/零风险处理；补齐共同情景与共同风险指标后重新发起分析方可排名",
        }
    top = ranking[0]
    flip_events = sensitivity.get("flip_events", [])
    fragile = sensitivity.get("fragile_to_small_weight_change", False)
    robust = not flip_events
    return {
        "recommendation_type": "robust" if robust else "assumption_dependent",
        "headline": (
            f"推荐方案 {top['option_id']}（{top['name']}），综合得分 {top['composite_score']}；"
            + (
                "在全部权重与概率扫描区间内名次未翻转，属稳健结论"
                if robust
                else "名次在敏感性扫描中发生翻转，属依赖假设的推荐"
            )
        ),
        "robust": robust,
        "assumption_dependent": not robust,
        "fragile_to_small_weight_change": fragile,
        "recommended_option_id": top["option_id"],
        "ranking_basis": {
            "weights": weights,
            "dimensions": DIMENSION_LABELS,
            "baseline_order": sensitivity.get("baseline_order", []),
        },
        "flip_sources": flip_events,
        "note": "综合得分是归一化加权评分，仅用于方案排序，不代表确定收益；期望净收益另行列示",
    }


def reweight(original: dict[str, Any], new_weights: dict[str, float]) -> dict[str, Any]:
    """基于同批次输入快照重算，生成派生批次；原批次不可变。"""
    payload = deepcopy(original["input_snapshot"])
    payload["weights"] = new_weights
    result = run_analysis(payload)
    result["derived_from_run_id"] = original.get("run_id")
    result["reweight_of"] = original.get("run_id")
    return result
