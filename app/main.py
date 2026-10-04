"""FastAPI 应用：家庭储能时段调度。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.audit import audit_telemetry
from app.models import (
    DispatchRequest,
    DispatchResponse,
    ErrorResponse,
    SettlementRequest,
    SettlementResponse,
    TelemetryAuditRequest,
    TelemetryAuditResponse,
)
from app.settlement import (
    NoValidIntervalError,
    PlanMismatchError,
    find_plan_mismatches,
    settle_execution,
)
from app.solver import InfeasibleError, solve_dispatch

app = FastAPI(
    title="家庭储能时段调度 API",
    description=(
        "输入半小时电价/基础负荷/光伏预测与电池参数，"
        "按 0.1kWh 离散 SOC 做动态规划，求满足功率、SOC 与末端约束的最低总成本计划；"
        "另提供 /telemetry/audit 对半小时实测记录做电芯能量守恒与购电量核对"
        "（只审计标记异常，不修正读数、不生成调度计划）；"
        "/dispatch/settlement 对计划执行做结算：重算核验计划快照、复用遥测审计"
        "划定连续有效区间，按实测购电与吞吐量核算实际费用并与计划对比。"
    ),
    version="1.2.0",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """把 Pydantic 校验错误整理成一条可读的中文原因。"""
    reasons = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
        msg = err.get("msg", "参数不合法")
        reasons.append(f"{loc + ': ' if loc else ''}{msg}")
    reason = "；".join(reasons) if reasons else "请求参数不合法"
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_input", "reason": reason},
    )


@app.exception_handler(InfeasibleError)
async def infeasible_exception_handler(request: Request, exc: InfeasibleError):
    return JSONResponse(
        status_code=422,
        content={"error": "infeasible", "reason": str(exc)},
    )


@app.exception_handler(PlanMismatchError)
async def plan_mismatch_exception_handler(request: Request, exc: PlanMismatchError):
    return JSONResponse(
        status_code=422,
        content={"error": "plan_mismatch", "reason": str(exc)},
    )


@app.exception_handler(NoValidIntervalError)
async def no_valid_interval_exception_handler(request: Request, exc: NoValidIntervalError):
    return JSONResponse(
        status_code=422,
        content={"error": "no_valid_interval", "reason": str(exc)},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_input", "reason": str(exc)},
    )


@app.get("/healthz", tags=["meta"])
async def healthz():
    return {"status": "ok"}


@app.post(
    "/dispatch",
    response_model=DispatchResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入或问题无解"}},
    tags=["dispatch"],
)
async def dispatch(req: DispatchRequest) -> DispatchResponse:
    result = solve_dispatch(
        prices=req.prices,
        base_load_kw=req.base_load_kw,
        pv_kw=req.pv_kw,
        capacity_kwh=req.capacity_kwh,
        max_charge_kw=req.max_charge_kw,
        max_discharge_kw=req.max_discharge_kw,
        charge_efficiency=req.charge_efficiency,
        discharge_efficiency=req.discharge_efficiency,
        initial_soc_kwh=req.initial_soc_kwh,
        min_terminal_soc_kwh=req.min_terminal_soc_kwh,
        degradation_cost=req.degradation_cost_per_kwh,
    )
    return DispatchResponse.model_validate(result)


@app.post(
    "/telemetry/audit",
    response_model=TelemetryAuditResponse,
    responses={422: {"model": ErrorResponse, "description": "非法输入"}},
    tags=["telemetry"],
)
async def telemetry_audit(req: TelemetryAuditRequest) -> TelemetryAuditResponse:
    """核对实测记录的能量守恒与购电量一致性；异常记录照常返回 200 与分项残差。"""
    result = audit_telemetry(
        charge_efficiency=req.charge_efficiency,
        discharge_efficiency=req.discharge_efficiency,
        energy_tolerance_kwh=req.energy_tolerance_kwh,
        records=req.records,
    )
    return TelemetryAuditResponse.model_validate(result)


@app.post(
    "/dispatch/settlement",
    response_model=SettlementResponse,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "非法输入、计划快照不一致、无可结算区间或调度问题无解",
        }
    },
    tags=["settlement"],
)
async def dispatch_settlement(req: SettlementRequest) -> SettlementResponse:
    """计划执行结算：重算核验快照，复用遥测审计划定有效区间，按实测核算费用。"""
    # 1. 用同一输入重算最优计划，确认快照未被篡改
    recomputed = solve_dispatch(
        prices=req.dispatch.prices,
        base_load_kw=req.dispatch.base_load_kw,
        pv_kw=req.dispatch.pv_kw,
        capacity_kwh=req.dispatch.capacity_kwh,
        max_charge_kw=req.dispatch.max_charge_kw,
        max_discharge_kw=req.dispatch.max_discharge_kw,
        charge_efficiency=req.dispatch.charge_efficiency,
        discharge_efficiency=req.dispatch.discharge_efficiency,
        initial_soc_kwh=req.dispatch.initial_soc_kwh,
        min_terminal_soc_kwh=req.dispatch.min_terminal_soc_kwh,
        degradation_cost=req.dispatch.degradation_cost_per_kwh,
    )
    mismatches = find_plan_mismatches(recomputed, req.plan)
    if mismatches:
        shown = "；".join(mismatches[:3])
        suffix = f" 等共 {len(mismatches)} 处" if len(mismatches) > 3 else ""
        raise PlanMismatchError(
            f"计划快照与按所给输入重算的最优计划不一致（疑似被篡改或与输入不配套）：{shown}{suffix}"
        )

    # 2. 复用遥测审计逐条核对实测记录（结算容差即审计能量容差）
    audit_report = audit_telemetry(
        charge_efficiency=req.dispatch.charge_efficiency,
        discharge_efficiency=req.dispatch.discharge_efficiency,
        energy_tolerance_kwh=req.settlement_tolerance_kwh,
        records=req.records,
    )

    # 3. 对首条异常前的连续有效区间做结算核算
    result = settle_execution(
        prices=req.dispatch.prices,
        degradation_cost_per_kwh=req.dispatch.degradation_cost_per_kwh,
        charge_efficiency=req.dispatch.charge_efficiency,
        discharge_efficiency=req.dispatch.discharge_efficiency,
        recomputed_plan=recomputed,
        records=req.records,
        audit_report=audit_report,
    )
    return SettlementResponse.model_validate(result)
