"""请求 / 响应模型与输入合法性校验。"""

from __future__ import annotations

import math
from typing import Annotated, List, Literal

from pydantic import BaseModel, Field, model_validator

MAX_PERIODS = 200
MAX_AUDIT_RECORDS = 2000
MIN_EFFICIENCY_EXCLUSIVE = 0.0
MAX_EFFICIENCY_INCLUSIVE = 1.0

# 价格 / 功率为 0.5h 数据：基础负荷、光伏预测单位 kW，电价单位 元/kWh。
Prices = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]
Loads = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]
Pvs = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]

NonNeg = Field(ge=0.0)
Positive = Field(gt=0.0)


class DispatchRequest(BaseModel):
    prices: Prices = Field(description="各时段电价（元/kWh），长度需与负荷、光伏一致")
    base_load_kw: Loads = Field(description="各时段基础负荷（kW）")
    pv_kw: Pvs = Field(description="各时段光伏预测功率（kW）")

    capacity_kwh: float = Field(gt=0.0, le=100.0, description="电池容量（kWh），须为 0.1 的整数倍")
    max_charge_kw: float = Field(ge=0.0, le=100.0, description="最大充电功率（kW，交流侧）")
    max_discharge_kw: float = Field(ge=0.0, le=100.0, description="最大放电功率（kW，交流侧）")

    charge_efficiency: float = Field(gt=0.0, le=1.0, description="充电效率（0, 1]")
    discharge_efficiency: float = Field(gt=0.0, le=1.0, description="放电效率（0, 1]")

    initial_soc_kwh: float = Field(ge=0.0, description="初始 SOC（kWh），须为 0.1 的整数倍")
    min_terminal_soc_kwh: float = Field(ge=0.0, description="最低末端 SOC（kWh），须为 0.1 的整数倍")
    degradation_cost_per_kwh: float = Field(ge=0.0, description="电芯每吞吐 1kWh 的循环衰减成本（元/kWh）")

    @model_validator(mode="after")
    def _validate(self) -> "DispatchRequest":
        # 1. 等长
        n = len(self.prices)
        if len(self.base_load_kw) != n or len(self.pv_kw) != n:
            raise ValueError(
                f"电价、基础负荷、光伏预测必须等长：当前分别为 {n}、"
                f"{len(self.base_load_kw)}、{len(self.pv_kw)}"
            )

        # 2. 有限且非负（数组字段）
        for name, seq in (
            ("电价", self.prices),
            ("基础负荷", self.base_load_kw),
            ("光伏预测", self.pv_kw),
        ):
            for i, v in enumerate(seq):
                if not math.isfinite(v):
                    raise ValueError(f"{name}第 {i} 个时段不是有限数值：{v}")
                if v < 0:
                    raise ValueError(f"{name}不允许为负值，第 {i} 个时段为 {v}")

        # 3. 标量有限性
        for name in (
            "capacity_kwh",
            "max_charge_kw",
            "max_discharge_kw",
            "charge_efficiency",
            "discharge_efficiency",
            "initial_soc_kwh",
            "min_terminal_soc_kwh",
            "degradation_cost_per_kwh",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name} 必须是有限数值")

        # 4. 0.1kWh 网格
        from app.solver import SOC_STEP_KWH, _snap_steps

        for name, v in (
            ("电池容量", self.capacity_kwh),
            ("初始 SOC", self.initial_soc_kwh),
            ("最低末端 SOC", self.min_terminal_soc_kwh),
        ):
            _snap_steps(v, name)  # 非整数倍会抛中文 ValueError

        cap_steps = round(self.capacity_kwh / SOC_STEP_KWH)
        init_steps = round(self.initial_soc_kwh / SOC_STEP_KWH)
        end_steps = round(self.min_terminal_soc_kwh / SOC_STEP_KWH)
        if init_steps > cap_steps:
            raise ValueError("初始 SOC 不能超过电池容量")
        if end_steps > cap_steps:
            raise ValueError("最低末端 SOC 不能超过电池容量")

        return self


class PeriodPlan(BaseModel):
    index: int
    action: Literal["charge", "discharge", "idle"]
    action_energy_kwh: float
    charge_ac_energy_kwh: float
    discharge_ac_energy_kwh: float
    battery_power_kw: float
    grid_purchase_energy_kwh: float
    pv_curtailed_energy_kwh: float
    soc_start_kwh: float
    soc_end_kwh: float
    energy_cost_yuan: float
    degradation_cost_yuan: float


class DispatchSummary(BaseModel):
    num_periods: int
    period_hours: float
    soc_resolution_kwh: float
    total_energy_cost_yuan: float
    total_degradation_cost_yuan: float
    total_cost_yuan: float
    total_grid_purchase_kwh: float
    peak_grid_purchase_energy_kwh: float
    peak_grid_purchase_power_kw: float
    battery_throughput_kwh: float
    terminal_soc_kwh: float


class DispatchResponse(BaseModel):
    periods: List[PeriodPlan]
    summary: DispatchSummary


class TelemetryRecord(BaseModel):
    """一条半小时实测记录：功率单位 kW，电量与 SOC 单位 kWh。"""

    base_load_kw: float = Field(description="基础负荷功率（kW）")
    pv_kw: float = Field(description="光伏功率（kW）")
    charge_ac_energy_kwh: float = Field(description="交流侧充电量（kWh）")
    discharge_ac_energy_kwh: float = Field(description="交流侧放电量（kWh）")
    grid_purchase_energy_kwh: float = Field(description="电网购电量（kWh）")
    soc_start_kwh: float = Field(description="时段首 SOC（kWh）")
    soc_end_kwh: float = Field(description="时段末 SOC（kWh）")


class TelemetryAuditRequest(BaseModel):
    capacity_kwh: float = Field(gt=0.0, description="电池容量（kWh），须为正")
    charge_efficiency: float = Field(gt=0.0, le=1.0, description="充电效率（0, 1]")
    discharge_efficiency: float = Field(gt=0.0, le=1.0, description="放电效率（0, 1]")
    energy_tolerance_kwh: float = Field(ge=0.0, description="能量容差（kWh），非负")
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_AUDIT_RECORDS)
    ] = Field(description="按时间顺序排列的半小时实测记录")

    @model_validator(mode="after")
    def _validate(self) -> "TelemetryAuditRequest":
        # 1. 标量有限性（gt/ge 比较不拦截 inf）
        for name in (
            "capacity_kwh",
            "charge_efficiency",
            "discharge_efficiency",
            "energy_tolerance_kwh",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name} 必须是有限数值")

        # 2. 逐条记录：有限、非负、SOC 不超容量
        for i, rec in enumerate(self.records):
            for name, label in (
                ("base_load_kw", "基础负荷"),
                ("pv_kw", "光伏功率"),
                ("charge_ac_energy_kwh", "交流充电量"),
                ("discharge_ac_energy_kwh", "交流放电量"),
                ("grid_purchase_energy_kwh", "电网购电量"),
                ("soc_start_kwh", "首态 SOC"),
                ("soc_end_kwh", "末态 SOC"),
            ):
                v = getattr(rec, name)
                if not math.isfinite(v):
                    raise ValueError(f"第 {i} 条记录的{label}不是有限数值：{v}")
                if v < 0:
                    raise ValueError(f"第 {i} 条记录的{label}不允许为负值：{v}")
            if rec.soc_start_kwh > self.capacity_kwh:
                raise ValueError(
                    f"第 {i} 条记录的首态 SOC {rec.soc_start_kwh}kWh "
                    f"超过电池容量 {self.capacity_kwh}kWh"
                )
            if rec.soc_end_kwh > self.capacity_kwh:
                raise ValueError(
                    f"第 {i} 条记录的末态 SOC {rec.soc_end_kwh}kWh "
                    f"超过电池容量 {self.capacity_kwh}kWh"
                )

        return self


class TelemetryRecordAudit(BaseModel):
    index: int
    expected_soc_end_kwh: float = Field(description="按首态 SOC 与交流充放电量核算的期望末态 SOC")
    soc_residual_kwh: float = Field(description="实测末态 SOC − 期望末态 SOC")
    expected_grid_purchase_energy_kwh: float = Field(description="按功率平衡核算的期望购电量")
    grid_residual_kwh: float = Field(description="实测购电量 − 期望购电量")
    soc_continuity_gap_kwh: float = Field(description="首态 SOC − 上一条末态 SOC（首条为 0）")
    anomalous: bool = Field(description="任一分项残差绝对值超容差即为 true")
    reasons: List[str] = Field(description="触发异常的具体原因")


class TelemetryAuditResponse(BaseModel):
    num_records: int
    period_hours: float
    energy_tolerance_kwh: float
    anomalous: bool = Field(description="是否存在异常记录")
    anomalous_records: List[int] = Field(description="异常记录的下标")
    records: List[TelemetryRecordAudit]


class ErrorResponse(BaseModel):
    error: str
    reason: str
