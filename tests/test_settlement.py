"""POST /dispatch/settlement 行为测试：pytest tests/ -q"""

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/dispatch/settlement", json=payload)


def _post_raw(payload):
    """用宽松 JSON 序列化发送（httpx 的 json= 拒绝 NaN/Inf，而服务端必须能拦截它们）。"""
    return client.post(
        "/dispatch/settlement",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )


# 4 个半小时时段：负荷恒 0.2kW（每时段 0.1kWh），谷时(0元)充、峰时(1元)放。
# 计划：t0 idle(购电0.1,费0.1) t1 charge 0.1(购电0.2,费0) t2 discharge 0.1(购电0,费0) t3 idle(购电0.1,费0)
BASE_DISPATCH = dict(
    prices=[1.0, 0.0, 1.0, 0.0],
    base_load_kw=[0.2, 0.2, 0.2, 0.2],
    pv_kw=[0.0, 0.0, 0.0, 0.0],
    capacity_kwh=0.2,
    max_charge_kw=1.0,
    max_discharge_kw=1.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    initial_soc_kwh=0.0,
    min_terminal_soc_kwh=0.0,
    degradation_cost_per_kwh=0.0,
)

# 与计划完全一致的实测记录（效率均为 1.0）
CLEAN_RECORDS = [
    dict(base_load_kw=0.2, pv_kw=0.0, charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
         grid_purchase_energy_kwh=0.1, soc_start_kwh=0.0, soc_end_kwh=0.0),
    dict(base_load_kw=0.2, pv_kw=0.0, charge_ac_energy_kwh=0.1, discharge_ac_energy_kwh=0.0,
         grid_purchase_energy_kwh=0.2, soc_start_kwh=0.0, soc_end_kwh=0.1),
    dict(base_load_kw=0.2, pv_kw=0.0, charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.1,
         grid_purchase_energy_kwh=0.0, soc_start_kwh=0.1, soc_end_kwh=0.0),
    dict(base_load_kw=0.2, pv_kw=0.0, charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
         grid_purchase_energy_kwh=0.1, soc_start_kwh=0.0, soc_end_kwh=0.0),
]


def _plan_snapshot(dispatch=None):
    r = client.post("/dispatch", json=dispatch or BASE_DISPATCH)
    assert r.status_code == 200, r.text
    return r.json()


def _payload(records, plan=None, dispatch=None, tol=0.05):
    return dict(
        dispatch=dispatch or BASE_DISPATCH,
        plan=plan if plan is not None else _plan_snapshot(dispatch),
        settlement_tolerance_kwh=tol,
        records=records,
    )


def test_clean_execution_fully_settled():
    """实测与计划完全一致：全部结算，差额为 0。"""
    r = _post(_payload(CLEAN_RECORDS))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    assert data["num_records"] == 4
    assert data["num_settled_records"] == 4
    assert data["num_pending_records"] == 0
    assert data["first_anomaly_index"] is None
    assert data["first_anomaly_reasons"] == []
    assert data["anomalous_records"] == []
    assert data["pending_record_indices"] == []

    assert data["planned_cost_yuan"] == 0.1
    assert data["actual_cost_yuan"] == 0.1
    assert data["cost_difference_yuan"] == 0.0
    assert data["actual_grid_purchase_kwh"] == 0.4
    assert data["planned_grid_purchase_kwh"] == 0.4
    assert data["actual_throughput_kwh"] == 0.2
    assert data["planned_throughput_kwh"] == 0.2

    for p in data["periods"]:
        assert p["status"] == "settled"
        assert p["cost_deviation_yuan"] == 0.0
        assert p["grid_deviation_kwh"] == 0.0


def test_measured_deviation_settles_with_difference():
    """t0 实测购电 0.12（残差 0.02 未超容差）：正常结算并给出 +0.02 元偏差。"""
    records = [dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=0.12)] + CLEAN_RECORDS[1:]
    r = _post(_payload(records))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    assert data["actual_cost_yuan"] == 0.12
    assert data["planned_cost_yuan"] == 0.1
    assert data["cost_difference_yuan"] == 0.02
    p0 = data["periods"][0]
    assert p0["actual_cost_yuan"] == 0.12
    assert p0["cost_deviation_yuan"] == 0.02
    assert p0["grid_deviation_kwh"] == 0.02


def test_anomaly_cuts_off_suffix_to_pending():
    """t2 购电实测 0.3（核算 0.0，残差 0.3 超容差）→ t2 起全部待结算。"""
    records = [
        dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=0.12),
        CLEAN_RECORDS[1],
        dict(CLEAN_RECORDS[2], grid_purchase_energy_kwh=0.3),
        CLEAN_RECORDS[3],
    ]
    r = _post(_payload(records))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "partially_settled"
    assert data["num_settled_records"] == 2
    assert data["num_pending_records"] == 2
    assert data["first_anomaly_index"] == 2
    assert data["anomalous_records"] == [2]
    assert data["pending_record_indices"] == [2, 3]
    assert any("购电量残差" in reason for reason in data["first_anomaly_reasons"])

    # 汇总只覆盖有效区间 [t0, t1]
    assert data["planned_cost_yuan"] == 0.1
    assert data["actual_cost_yuan"] == 0.12
    assert data["cost_difference_yuan"] == 0.02

    statuses = [p["status"] for p in data["periods"]]
    assert statuses == ["settled", "settled", "pending", "pending"]
    # 待结算时段不核算费用
    for p in data["periods"][2:]:
        assert p["actual_cost_yuan"] is None
        assert p["cost_deviation_yuan"] is None
        assert p["measured_grid_purchase_energy_kwh"] is None


def test_soc_discontinuity_cuts_off():
    """t1 首态 SOC 与 t0 末态不衔接（断点）→ 仅 t0 可结算。"""
    records = [
        CLEAN_RECORDS[0],                                        # t0: 0→0
        dict(CLEAN_RECORDS[1], soc_start_kwh=0.1, soc_end_kwh=0.2),  # t1: 0.1→0.2，自洽但与 t0 断档 0.1
        dict(CLEAN_RECORDS[2], soc_start_kwh=0.2, soc_end_kwh=0.1),  # t2: 0.2→0.1
        dict(CLEAN_RECORDS[3], soc_start_kwh=0.1, soc_end_kwh=0.1),  # t3: 0.1→0.1
    ]
    r = _post(_payload(records))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "partially_settled"
    assert data["first_anomaly_index"] == 1
    assert data["anomalous_records"] == [1]
    assert data["num_settled_records"] == 1
    assert data["pending_record_indices"] == [1, 2, 3]
    assert any("不衔接" in reason for reason in data["first_anomaly_reasons"])


def test_first_record_anomalous_rejected():
    """首条记录即异常 → 没有有效区间，整体拒绝。"""
    records = [dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=0.5)] + CLEAN_RECORDS[1:]
    r = _post(_payload(records))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "no_valid_interval"
    assert "首条" in body["reason"]


def test_tampered_plan_rejected():
    """快照任一字段被改动 → 计划不一致，整体拒绝。"""
    plan = _plan_snapshot()
    plan["periods"][2]["grid_purchase_energy_kwh"] = 0.5
    r = _post(_payload(CLEAN_RECORDS, plan=plan))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "plan_mismatch"
    assert "periods[2].grid_purchase_energy_kwh" in body["reason"]

    plan = _plan_snapshot()
    plan["summary"]["total_cost_yuan"] = 0.01
    r = _post(_payload(CLEAN_RECORDS, plan=plan))
    assert r.status_code == 422
    assert r.json()["error"] == "plan_mismatch"
    assert "summary.total_cost_yuan" in r.json()["reason"]

    plan = _plan_snapshot()
    plan["periods"][1]["action"] = "idle"
    r = _post(_payload(CLEAN_RECORDS, plan=plan))
    assert r.status_code == 422
    assert r.json()["error"] == "plan_mismatch"


def test_plan_from_other_input_rejected():
    """快照本身合法但属于另一组输入 → 与重算结果不一致。"""
    other_dispatch = dict(BASE_DISPATCH, prices=[0.0, 0.0, 0.0, 0.0])
    foreign_plan = _plan_snapshot(other_dispatch)
    r = _post(_payload(CLEAN_RECORDS, plan=foreign_plan))
    assert r.status_code == 422
    assert r.json()["error"] == "plan_mismatch"


def test_length_mismatch_rejected():
    """实测记录数与调度时段数不符 → 整体拒绝。"""
    r = _post(_payload(CLEAN_RECORDS[:3]))
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_input"
    assert "等长" in body["reason"]

    r = _post(_payload(CLEAN_RECORDS + [CLEAN_RECORDS[0]]))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_invalid_records_rejected():
    """实测记录非法值沿用审计的校验规则。"""
    bad_records = [
        dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=-0.1),     # 负测量值
        dict(CLEAN_RECORDS[0], grid_purchase_energy_kwh=float("nan")),  # 非有限
        dict(CLEAN_RECORDS[0], soc_end_kwh=0.3),                   # SOC 超容量 0.2
    ]
    for bad in bad_records:
        records = [bad] + CLEAN_RECORDS[1:]
        r = _post_raw(_payload(records))
        assert r.status_code == 422, bad
        body = r.json()
        assert body["error"] == "invalid_input"
        assert "第 0 条记录" in body["reason"]

    r = _post_raw(_payload(CLEAN_RECORDS, tol=-0.1))
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"


def test_infeasible_dispatch_input_rejected():
    """dispatch 输入本身无解 → 沿用 infeasible 错误（重算在快照核验之前）。"""
    dispatch = dict(BASE_DISPATCH, max_charge_kw=0.0, min_terminal_soc_kwh=0.1)
    r = _post(_payload(CLEAN_RECORDS, plan=_plan_snapshot(), dispatch=dispatch))
    assert r.status_code == 422
    assert r.json()["error"] == "infeasible"


def test_settlement_with_efficiency_and_degradation():
    """效率 <1 且计衰减费：实际吞吐量按实测交流量折算，费用与计划一致。"""
    dispatch = dict(
        BASE_DISPATCH,
        prices=[0.0, 10.0, 0.0],
        base_load_kw=[0.0, 0.2, 0.0],
        pv_kw=[0.0, 0.0, 0.0],
        charge_efficiency=0.8,
        discharge_efficiency=0.8,
        degradation_cost_per_kwh=0.5,
    )
    # 计划：t0 充 0.125 交流(0.1 电芯)，t1 放 0.08 交流并补购 0.02，t2 静置
    records = [
        dict(base_load_kw=0.0, pv_kw=0.0, charge_ac_energy_kwh=0.125,
             discharge_ac_energy_kwh=0.0, grid_purchase_energy_kwh=0.125,
             soc_start_kwh=0.0, soc_end_kwh=0.1),
        dict(base_load_kw=0.2, pv_kw=0.0, charge_ac_energy_kwh=0.0,
             discharge_ac_energy_kwh=0.08, grid_purchase_energy_kwh=0.02,
             soc_start_kwh=0.1, soc_end_kwh=0.0),
        dict(base_load_kw=0.0, pv_kw=0.0, charge_ac_energy_kwh=0.0,
             discharge_ac_energy_kwh=0.0, grid_purchase_energy_kwh=0.0,
             soc_start_kwh=0.0, soc_end_kwh=0.0),
    ]
    r = _post(_payload(records, dispatch=dispatch))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "settled"
    # 实测吞吐量 = 0.125*0.8 + 0.08/0.8 = 0.2；衰减费 = 0.2*0.5 = 0.1
    assert data["actual_throughput_kwh"] == 0.2
    assert data["actual_degradation_cost_yuan"] == 0.1
    # 实际能量费 = 10*0.02 = 0.2；总实际费用 = 0.3 = 计划费用
    assert data["actual_energy_cost_yuan"] == 0.2
    assert data["actual_cost_yuan"] == 0.3
    assert data["planned_cost_yuan"] == 0.3
    assert data["cost_difference_yuan"] == 0.0


def test_existing_endpoints_untouched():
    """新增结算接口不影响既有 /dispatch 与 /telemetry/audit。"""
    r = client.post("/dispatch", json=BASE_DISPATCH)
    assert r.status_code == 200, r.text
    assert len(r.json()["periods"]) == 4

    r = client.post(
        "/telemetry/audit",
        json=dict(
            capacity_kwh=0.2,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            energy_tolerance_kwh=0.05,
            records=CLEAN_RECORDS[:1],
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["anomalous"] is False
