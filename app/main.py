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
    TelemetryAuditRequest,
    TelemetryAuditResponse,
)
from app.solver import InfeasibleError, solve_dispatch

app = FastAPI(
    title="家庭储能时段调度 API",
    description=(
        "输入半小时电价/基础负荷/光伏预测与电池参数，"
        "按 0.1kWh 离散 SOC 做动态规划，求满足功率、SOC 与末端约束的最低总成本计划；"
        "另提供 /telemetry/audit 对半小时实测记录做电芯能量守恒与购电量核对"
        "（只审计标记异常，不修正读数、不生成调度计划）。"
    ),
    version="1.1.0",
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
