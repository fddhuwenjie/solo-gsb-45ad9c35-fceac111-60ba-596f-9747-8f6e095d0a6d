"""储能计划执行结算。

流程（对应 POST /dispatch/settlement）：
1. 用请求携带的 /dispatch 输入重算最优计划，与计划快照逐字段比对——
   快照由本服务原样返回时应完全相等，任何篡改或与其他输入混用都会暴露；
2. 复用遥测审计（audit_telemetry）逐条核对实测记录；
3. 自首条异常记录（含 SOC 断点）起，该条及后续所有记录只能标为待结算；
4. 对此前的连续有效区间，按实测购电量与电芯吞吐量核算实际费用，
   并与计划费用对比，给出总差额与逐时段偏差。

实际费用口径（与 /dispatch 的费用模型一致）：
- 实际能量费 = 时段电价 × 实测购电量；
- 实际衰减费 = degradation_cost_per_kwh × 实测电芯吞吐量，
  吞吐量 = 交流充电量×充电效率 + 交流放电量÷放电效率（电芯侧，充放各计一次）。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

from app.models import DispatchResponse, TelemetryRecord
from app.solver import PERIOD_HOURS

# 快照核验的数值容差：仅吸收 JSON 序列化等无害的表示差异，
# 任何真实篡改（哪怕 0.0001 元）都远超该量级。
_PLAN_EPS_ABS = 1e-9
_PLAN_EPS_REL = 1e-9


class PlanMismatchError(Exception):
    """计划快照与按所给输入重算的最优计划不一致（快照被篡改或与输入不配套）。"""


class NoValidIntervalError(Exception):
    """首条实测记录即未通过审计：不存在可结算的连续有效区间。"""


def _round6(value: float) -> float:
    """保留 6 位小数；+0.0 把 -0.0 归一为 0.0，避免响应里出现负零。"""
    return round(value, 6) + 0.0


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_PLAN_EPS_REL, abs_tol=_PLAN_EPS_ABS)


def find_plan_mismatches(recomputed: dict, snapshot: DispatchResponse) -> List[str]:
    """逐字段比对重算计划与快照，返回不一致描述列表（空列表 = 快照未被篡改）。"""
    mismatches: List[str] = []

    snap_summary = snapshot.summary.model_dump()
    for key, expected in recomputed["summary"].items():
        actual = snap_summary[key]
        if not _close(float(actual), float(expected)):
            mismatches.append(f"summary.{key} 快照值 {actual} 与重算值 {expected} 不符")

    exp_periods = recomputed["periods"]
    act_periods = snapshot.periods
    if len(act_periods) != len(exp_periods):
        mismatches.append(
            f"periods 长度 {len(act_periods)} 与重算时段数 {len(exp_periods)} 不符"
        )
    else:
        for i, (exp, act) in enumerate(zip(exp_periods, act_periods)):
            snap = act.model_dump()
            for key, expected in exp.items():
                actual = snap[key]
                if isinstance(expected, str):
                    if actual != expected:
                        mismatches.append(
                            f"periods[{i}].{key} 快照值 {actual!r} 与重算值 {expected!r} 不符"
                        )
                elif not _close(float(actual), float(expected)):
                    mismatches.append(
                        f"periods[{i}].{key} 快照值 {actual} 与重算值 {expected} 不符"
                    )
    return mismatches


def settle_execution(
    *,
    prices: Sequence[float],
    degradation_cost_per_kwh: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    recomputed_plan: dict,
    records: Sequence[TelemetryRecord],
    audit_report: dict,
) -> dict:
    """对连续有效区间做结算核算，返回结算报告（dict）。

    audit_report 为 audit_telemetry 的返回；首条异常记录下标即有效区间的终点。
    """
    n = len(records)
    anomalous_records: List[int] = audit_report["anomalous_records"]
    first_anomaly: Optional[int] = anomalous_records[0] if anomalous_records else None
    cutoff = first_anomaly if first_anomaly is not None else n

    if cutoff == 0:
        reasons = "；".join(audit_report["records"][0]["reasons"])
        raise NoValidIntervalError(
            f"首条实测记录即未通过审计（{reasons}），"
            "不存在可结算的连续有效区间，本次结算整体拒绝"
        )

    plan_periods = recomputed_plan["periods"]
    periods: List[dict] = []

    tot_planned_cost = 0.0
    tot_actual_cost = 0.0
    tot_planned_energy = 0.0
    tot_planned_deg = 0.0
    tot_actual_energy = 0.0
    tot_actual_deg = 0.0
    tot_planned_grid = 0.0
    tot_actual_grid = 0.0
    tot_planned_throughput = 0.0
    tot_actual_throughput = 0.0

    for i in range(n):
        if i >= cutoff:
            # 首条异常（含 SOC 断点）起，后续记录只能标为待结算
            periods.append({"index": i, "status": "pending"})
            continue

        rec = records[i]
        plan = plan_periods[i]

        # 实测电芯吞吐量：交流充电量×ηc + 交流放电量÷ηd（充、放各计一次）
        measured_throughput = (
            rec.charge_ac_energy_kwh * charge_efficiency
            + rec.discharge_ac_energy_kwh / discharge_efficiency
        )
        actual_energy_cost = prices[i] * rec.grid_purchase_energy_kwh
        actual_deg_cost = degradation_cost_per_kwh * measured_throughput
        actual_cost = actual_energy_cost + actual_deg_cost

        planned_energy_cost = plan["energy_cost_yuan"]
        planned_deg_cost = plan["degradation_cost_yuan"]
        planned_cost = planned_energy_cost + planned_deg_cost
        planned_grid = plan["grid_purchase_energy_kwh"]
        planned_throughput = abs(plan["action_energy_kwh"])

        tot_planned_cost += planned_cost
        tot_actual_cost += actual_cost
        tot_planned_energy += planned_energy_cost
        tot_planned_deg += planned_deg_cost
        tot_actual_energy += actual_energy_cost
        tot_actual_deg += actual_deg_cost
        tot_planned_grid += planned_grid
        tot_actual_grid += rec.grid_purchase_energy_kwh
        tot_planned_throughput += planned_throughput
        tot_actual_throughput += measured_throughput

        periods.append(
            {
                "index": i,
                "status": "settled",
                "planned_cost_yuan": _round6(planned_cost),
                "actual_cost_yuan": _round6(actual_cost),
                "cost_deviation_yuan": _round6(actual_cost - planned_cost),
                "planned_grid_purchase_energy_kwh": _round6(planned_grid),
                "measured_grid_purchase_energy_kwh": _round6(rec.grid_purchase_energy_kwh),
                "grid_deviation_kwh": _round6(rec.grid_purchase_energy_kwh - planned_grid),
                "planned_throughput_kwh": _round6(planned_throughput),
                "measured_throughput_kwh": _round6(measured_throughput),
                "throughput_deviation_kwh": _round6(measured_throughput - planned_throughput),
            }
        )

    return {
        "status": "settled" if first_anomaly is None else "partially_settled",
        "num_records": n,
        "num_settled_records": cutoff,
        "num_pending_records": n - cutoff,
        "period_hours": PERIOD_HOURS,
        "settlement_tolerance_kwh": audit_report["energy_tolerance_kwh"],
        "first_anomaly_index": first_anomaly,
        "first_anomaly_reasons": (
            audit_report["records"][first_anomaly]["reasons"]
            if first_anomaly is not None
            else []
        ),
        "anomalous_records": anomalous_records,
        "pending_record_indices": list(range(cutoff, n)),
        "planned_cost_yuan": _round6(tot_planned_cost),
        "actual_cost_yuan": _round6(tot_actual_cost),
        "cost_difference_yuan": _round6(tot_actual_cost - tot_planned_cost),
        "planned_energy_cost_yuan": _round6(tot_planned_energy),
        "planned_degradation_cost_yuan": _round6(tot_planned_deg),
        "actual_energy_cost_yuan": _round6(tot_actual_energy),
        "actual_degradation_cost_yuan": _round6(tot_actual_deg),
        "planned_grid_purchase_kwh": _round6(tot_planned_grid),
        "actual_grid_purchase_kwh": _round6(tot_actual_grid),
        "planned_throughput_kwh": _round6(tot_planned_throughput),
        "actual_throughput_kwh": _round6(tot_actual_throughput),
        "periods": periods,
    }
