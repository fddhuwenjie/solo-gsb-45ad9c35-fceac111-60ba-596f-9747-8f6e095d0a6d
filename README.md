# 家庭储能时段调度 API

基于 **Python + FastAPI + Pydantic** 的家庭储能调度服务。输入等长的半小时电价、基础负荷、光伏预测序列以及电池参数，用**动态规划（SOC 离散到 0.1 kWh）**求出满足功率、SOC 和末端 SOC 约束的**最低总成本**充放电计划；另提供 `/telemetry/audit` 对半小时实测记录做能量守恒与购电量**审计**（只核对标记，不修正读数、不生成调度计划），以及 `/dispatch/settlement` 对计划执行做**结算**：重算核验计划快照、复用遥测审计划定连续有效区间，按实测购电与吞吐量核算实际费用并与计划对比。

## 一条命令启动

需要 Docker 20.10+（带 Compose v2）：

```bash
docker compose up --build
```

服务启动后：

- 健康检查：<http://localhost:8000/healthz>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用 `docker compose down`）。

本地裸机运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

### `GET /healthz`

```json
{"status": "ok"}
```

### `POST /dispatch`

请求体（每个时段固定 **0.5 小时**，三个数组必须等长，长度 1–200）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `prices` | 元/kWh | 各时段购电电价（非负） |
| `base_load_kw` | kW | 各时段基础负荷 |
| `pv_kw` | kW | 各时段光伏预测功率 |
| `capacity_kwh` | kWh | 电池容量，必须为 0.1 的整数倍 |
| `max_charge_kw` / `max_discharge_kw` | kW | 最大充 / 放电**交流侧**功率 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `initial_soc_kwh` | kWh | 初始 SOC（0.1 整数倍，≤ 容量） |
| `min_terminal_soc_kwh` | kWh | 最低末端 SOC（0.1 整数倍，≤ 容量） |
| `degradation_cost_per_kwh` | 元/kWh | 按电芯吞吐量计的循环衰减成本 |

示例：

```bash
curl -s -X POST http://localhost:8000/dispatch \
  -H 'Content-Type: application/json' \
  -d '{
    "prices": [1.0, 0.0, 1.0],
    "base_load_kw": [0.2, 0.2, 0.2],
    "pv_kw": [0.0, 0.0, 0.0],
    "capacity_kwh": 0.2,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 1.0,
    "discharge_efficiency": 1.0,
    "initial_soc_kwh": 0.0,
    "min_terminal_soc_kwh": 0.0,
    "degradation_cost_per_kwh": 0.0
  }'
```

响应：`periods` 为逐时段计划，`summary` 为汇总。

```json
{
  "periods": [
    {
      "index": 0,
      "action": "idle",                 // charge / discharge / idle
      "action_energy_kwh": 0.0,         // 电芯侧能量变化：正=充，负=放，0.1 整数倍
      "charge_ac_energy_kwh": 0.0,      // 该时段电池吸收的交流电量
      "discharge_ac_energy_kwh": 0.0,   // 该时段电池放出的交流电量
      "battery_power_kw": 0.0,          // 正=充电功率，负=放电功率
      "grid_purchase_energy_kwh": 0.1,  // 购电量
      "pv_curtailed_energy_kwh": 0.0,   // 弃光量
      "soc_start_kwh": 0.0,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.1,          // 该时段能量费
      "degradation_cost_yuan": 0.0      // 该时段衰减费
    }
  ],
  "summary": {
    "num_periods": 3,
    "period_hours": 0.5,
    "soc_resolution_kwh": 0.1,
    "total_energy_cost_yuan": 0.1,
    "total_degradation_cost_yuan": 0.0,
    "total_cost_yuan": 0.1,
    "total_grid_purchase_kwh": 0.2,
    "peak_grid_purchase_energy_kwh": 0.2,
    "peak_grid_purchase_power_kw": 0.4,
    "battery_throughput_kwh": 0.1,
    "terminal_soc_kwh": 0.0
  }
}
```

### `POST /telemetry/audit`

对按时间顺序排列的半小时**实测记录**逐条做台账核对：电芯侧能量守恒、购电量功率平衡、相邻记录 SOC 衔接。只审计并标记异常，**不修正读数、不生成调度计划**。

请求体：

| 字段 | 单位 | 说明 |
|---|---|---|
| `capacity_kwh` | kWh | 电池容量，须为正 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `energy_tolerance_kwh` | kWh | 能量容差（非负）；任一分项残差绝对值**严格大于**它即判该条异常 |
| `records` | – | 按序排列的半小时实测记录，长度 1–2000 |

`records` 每条记录的字段（均须有限且非负，SOC 不得超过容量）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `base_load_kw` | kW | 基础负荷功率 |
| `pv_kw` | kW | 光伏功率 |
| `charge_ac_energy_kwh` / `discharge_ac_energy_kwh` | kWh | 交流侧充 / 放电量 |
| `grid_purchase_energy_kwh` | kWh | 电网购电量 |
| `soc_start_kwh` / `soc_end_kwh` | kWh | 时段首 / 末 SOC |

逐条核算规则（与 `/dispatch` 的模型假设一致）：

```
期望末态 SOC = 首态 SOC + 交流充电量×充电效率 − 交流放电量÷放电效率
期望购电量   = max(0, (基础负荷 − 光伏)×0.5 + 交流充电量 − 交流放电量)
SOC 衔接差   = 首态 SOC − 上一条记录的末态 SOC（首条记 0）
```

响应中每条记录给出三个分项残差（实测 − 核算）：`soc_residual_kwh`、`grid_residual_kwh`、`soc_continuity_gap_kwh`，任一绝对值超容差则该条 `anomalous=true` 并在 `reasons` 列出原因；顶层 `anomalous_records` 汇总异常下标。审计结果正常返回 **HTTP 200**（异常是核对结论，不是请求错误）。

示例（第二条记录购电量实测 0.7，核算仅 0.55，残差 0.15 超容差）：

```bash
curl -s -X POST http://localhost:8000/telemetry/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity_kwh": 10.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "energy_tolerance_kwh": 0.05,
    "records": [
      {"base_load_kw": 1.0, "pv_kw": 0.4, "charge_ac_energy_kwh": 0.5,
       "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.8,
       "soc_start_kwh": 2.0, "soc_end_kwh": 2.45},
      {"base_load_kw": 2.0, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0,
       "discharge_ac_energy_kwh": 0.45, "grid_purchase_energy_kwh": 0.7,
       "soc_start_kwh": 2.45, "soc_end_kwh": 1.95}
    ]
  }'
```

```json
{
  "num_records": 2,
  "period_hours": 0.5,
  "energy_tolerance_kwh": 0.05,
  "anomalous": true,
  "anomalous_records": [1],
  "records": [
    {
      "index": 0,
      "expected_soc_end_kwh": 2.45,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.8,
      "grid_residual_kwh": 0.0,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": false,
      "reasons": []
    },
    {
      "index": 1,
      "expected_soc_end_kwh": 1.95,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.55,
      "grid_residual_kwh": 0.15,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": true,
      "reasons": ["购电量残差 +0.150000kWh 超容差 0.05kWh（实测 0.7kWh，核算值 0.55kWh）"]
    }
  ]
}
```

### `POST /dispatch/settlement`

对一次计划执行做**结算**。请求携带一次 `/dispatch` 的完整输入、该次调用的完整响应快照、结算容差，以及与调度时段**等长**的实测记录。处理流程：

1. **快照核验**：用所给输入重新求解，与 `plan` 快照逐字段比对（数值容差 1e-9，仅吸收 JSON 表示差异）；任何篡改或与其他输入混用都会暴露，整体拒绝；
2. **记录审计**：复用 `/telemetry/audit` 的同一套核算（电芯能量守恒、购电量功率平衡、相邻 SOC 衔接），容差取 `settlement_tolerance_kwh`；
3. **有效区间**：自首条异常记录（含 SOC 断点）起，该条及后续所有记录只能标为**待结算**；之前的连续有效区间才参与结算；
4. **费用核算**：对有效区间按实测值计算实际费用，与计划费用对比。

实际费用口径（与 `/dispatch` 费用模型一致）：

```
实际能量费 = 时段电价 × 实测购电量
实际衰减费 = degradation_cost_per_kwh × 实测电芯吞吐量
实测电芯吞吐量 = 交流充电量×充电效率 + 交流放电量÷放电效率   （充、放各计一次）
```

请求体：

| 字段 | 说明 |
|---|---|
| `dispatch` | 一次 `/dispatch` 的完整请求输入（结构同 `/dispatch` 请求体） |
| `plan` | 该次 `/dispatch` 的完整响应快照（`periods` + `summary`），用于防篡改核验 |
| `settlement_tolerance_kwh` | 结算容差（kWh，非负），用作实测记录审计的能量容差 |
| `records` | 按时间顺序排列的实测记录（字段同 `/telemetry/audit`），条数必须与调度时段数一致 |

响应（HTTP 200）：`status` 为 `settled`（全部记录完成结算）或 `partially_settled`（仅首条异常前的连续有效区间完成结算）；`first_anomaly_index` 给出首条异常下标（无异常为 `null`）；顶层汇总只覆盖有效区间；`periods` 逐时段给出 `settled`/`pending` 状态，已结算时段含计划费用、实际费用与各项偏差，待结算时段费用字段为 `null`。

**整体拒绝**（HTTP 422）：计划快照与重算结果不一致（`plan_mismatch`）、实测记录数与调度时段数不符（`invalid_input`）、首条记录即异常导致没有有效区间（`no_valid_interval`）。

示例（4 个时段：t0 实测购电 0.12 与计划 0.1 的残差 0.02 未超容差，正常结算；t2 实测购电 0.3 与核算 0.0 的残差 0.3 超容差 → t2 起待结算）：

```bash
# plan 字段即先前 POST /dispatch 的完整响应，原样回填
curl -s -X POST http://localhost:8000/dispatch/settlement \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [1.0, 0.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.2,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 1.0,
      "discharge_efficiency": 1.0,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.0
    },
    "plan": {
      "periods": [
        {"index": 0, "action": "idle", "action_energy_kwh": 0.0, "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0, "battery_power_kw": 0.0, "grid_purchase_energy_kwh": 0.1, "pv_curtailed_energy_kwh": 0.0, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0, "energy_cost_yuan": 0.1, "degradation_cost_yuan": 0.0},
        {"index": 1, "action": "charge", "action_energy_kwh": 0.1, "charge_ac_energy_kwh": 0.1, "discharge_ac_energy_kwh": 0.0, "battery_power_kw": 0.2, "grid_purchase_energy_kwh": 0.2, "pv_curtailed_energy_kwh": 0.0, "soc_start_kwh": 0.0, "soc_end_kwh": 0.1, "energy_cost_yuan": 0.0, "degradation_cost_yuan": 0.0},
        {"index": 2, "action": "discharge", "action_energy_kwh": -0.1, "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.1, "battery_power_kw": -0.2, "grid_purchase_energy_kwh": 0.0, "pv_curtailed_energy_kwh": 0.0, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0, "energy_cost_yuan": 0.0, "degradation_cost_yuan": 0.0},
        {"index": 3, "action": "idle", "action_energy_kwh": 0.0, "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0, "battery_power_kw": 0.0, "grid_purchase_energy_kwh": 0.1, "pv_curtailed_energy_kwh": 0.0, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0, "energy_cost_yuan": 0.0, "degradation_cost_yuan": 0.0}
      ],
      "summary": {"num_periods": 4, "period_hours": 0.5, "soc_resolution_kwh": 0.1, "total_energy_cost_yuan": 0.1, "total_degradation_cost_yuan": 0.0, "total_cost_yuan": 0.1, "total_grid_purchase_kwh": 0.4, "peak_grid_purchase_energy_kwh": 0.2, "peak_grid_purchase_power_kw": 0.4, "battery_throughput_kwh": 0.2, "terminal_soc_kwh": 0.0}
    },
    "settlement_tolerance_kwh": 0.05,
    "records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.12, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0},
      {"base_load_kw": 0.2, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.1, "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.2, "soc_start_kwh": 0.0, "soc_end_kwh": 0.1},
      {"base_load_kw": 0.2, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.1, "grid_purchase_energy_kwh": 0.3, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
      {"base_load_kw": 0.2, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
    ]
  }'
```

```json
{
  "status": "partially_settled",
  "num_records": 4,
  "num_settled_records": 2,
  "num_pending_records": 2,
  "period_hours": 0.5,
  "settlement_tolerance_kwh": 0.05,
  "first_anomaly_index": 2,
  "first_anomaly_reasons": ["购电量残差 +0.300000kWh 超容差 0.05kWh（实测 0.3kWh，核算值 0.0kWh）"],
  "anomalous_records": [2],
  "pending_record_indices": [2, 3],
  "planned_cost_yuan": 0.1,
  "actual_cost_yuan": 0.12,
  "cost_difference_yuan": 0.02,
  "planned_energy_cost_yuan": 0.1,
  "planned_degradation_cost_yuan": 0.0,
  "actual_energy_cost_yuan": 0.12,
  "actual_degradation_cost_yuan": 0.0,
  "planned_grid_purchase_kwh": 0.3,
  "actual_grid_purchase_kwh": 0.32,
  "planned_throughput_kwh": 0.1,
  "actual_throughput_kwh": 0.1,
  "periods": [
    {"index": 0, "status": "settled", "planned_cost_yuan": 0.1, "actual_cost_yuan": 0.12, "cost_deviation_yuan": 0.02, "planned_grid_purchase_energy_kwh": 0.1, "measured_grid_purchase_energy_kwh": 0.12, "grid_deviation_kwh": 0.02, "planned_throughput_kwh": 0.0, "measured_throughput_kwh": 0.0, "throughput_deviation_kwh": 0.0},
    {"index": 1, "status": "settled", "planned_cost_yuan": 0.0, "actual_cost_yuan": 0.0, "cost_deviation_yuan": 0.0, "planned_grid_purchase_energy_kwh": 0.2, "measured_grid_purchase_energy_kwh": 0.2, "grid_deviation_kwh": 0.0, "planned_throughput_kwh": 0.1, "measured_throughput_kwh": 0.1, "throughput_deviation_kwh": 0.0},
    {"index": 2, "status": "pending", "planned_cost_yuan": null, "actual_cost_yuan": null, "cost_deviation_yuan": null, "planned_grid_purchase_energy_kwh": null, "measured_grid_purchase_energy_kwh": null, "grid_deviation_kwh": null, "planned_throughput_kwh": null, "measured_throughput_kwh": null, "throughput_deviation_kwh": null},
    {"index": 3, "status": "pending", "planned_cost_yuan": null, "actual_cost_yuan": null, "cost_deviation_yuan": null, "planned_grid_purchase_energy_kwh": null, "measured_grid_purchase_energy_kwh": null, "grid_deviation_kwh": null, "planned_throughput_kwh": null, "measured_throughput_kwh": null, "throughput_deviation_kwh": null}
  ]
}
```

## 模型与假设

- **时段**：固定 0.5 h；功率（kW）× 0.5 即该时段能量（kWh）。
- **动作与 SOC 网格**：动作即一个时段内电芯能量变化，离散为 0.1 kWh 的整数倍：正数充电、负数放电、0 静置；SOC 全程位于 `[0, capacity]` 的 0.1 kWh 网格上。容量、初始 SOC、最低末端 SOC 必须精确落在该网格上，否则报参数错误（不做静默取整）。
- **功率平衡**（每时段）：

  ```
  购电量 = max(0, 基础负荷 − 光伏 + 充电交流电量 − 放电交流电量)
  ```

  光伏优先供负荷，剩余可给电池充电；**不允许向电网倒送电**，无法消纳的光伏盈余计为弃光（`pv_curtailed_energy_kwh`），放电量因此不会超过同时段净负荷。
- **效率**：充电交流电量 = 电芯电量 / ηc；放电交流电量 = 电芯电量 × ηd。功率上限按交流侧考核（充电：电芯电量 ≤ P充·Δt·ηc；放电：电芯电量 ≤ P放·Δt/ηd）。
- **费用**：
  - 能量费 = 电价 × 购电量；
  - 衰减费 = `degradation_cost_per_kwh` × 电芯侧吞吐量，吞吐量 = Σ|充入电芯能量| + Σ|电芯放出能量|（充、放各计一次）；
  - 总成本 = 能量费 + 衰减费。
- **末端约束**：最后一个时段结束时 SOC ≥ `min_terminal_soc_kwh`。

### 动态规划

状态 `(t, j)`：第 `t` 个时段结束、SOC 处于第 `j` 格（j × 0.1 kWh）时的最优前缀。每格最多转移到「静置 / 充电 1..k 格 / 放电 1..k 格」，转移范围同时受功率上限、SOC 上下界和"禁止倒送"约束，转移成本即时算出能量费与衰减费。时间复杂度 O(N · C · P)，其中 C = 容量/0.1，P = 单时段最多动作格数。

### 破平局（保证结果确定、可复现）

当不同计划在数值容差内无法区分时，依次按：

1. **总成本**低者优先（能量费 + 衰减费）；
2. 总成本相同 → **峰值购电量**（各时段购电量的最大值，kWh/半小时）小者优先，即偏好削峰；
3. 仍相同 → **动作序列字典序**小者优先，动作为整数（0.1 kWh 格数，正充负放），越靠前的时段差异权重越大——因此"先静置、后动作"会胜过"先动作、后静置"。

## 错误处理

非法输入或无解统一返回 **HTTP 422**，JSON 说明原因：

```json
{"error": "invalid_input", "reason": "电价、基础负荷、光伏预测必须等长：当前分别为 3、2、3"}
```

```json
{"error": "infeasible", "reason": "无满足末端 SOC 约束的计划：调度期末最高可达 SOC 约为 0.0kWh，低于最低末端 SOC 0.1kWh；原因是受时段数、最大充电功率或充电效率限制，无法在期末充到要求电量"}
```

`invalid_input` 覆盖：数组不等长、负值/非有限值、效率不在 (0,1]、容量/SOC 非 0.1 整数倍、初始或末端 SOC 超过容量、数组长度超出 1–200 等；`infeasible` 表示输入合法但约束可行域为空，并给出最高可达末端 SOC 等诊断信息。`/telemetry/audit` 的非法输入（容量非正、效率越界、测量值或容差为负/非有限、SOC 超容量、记录数超出 1–2000 等）同样以 `invalid_input` 返回。

`/dispatch/settlement` 的整体拒绝（均为 422）：

```json
{"error": "plan_mismatch", "reason": "计划快照与按所给输入重算的最优计划不一致（疑似被篡改或与输入不配套）：periods[2].grid_purchase_energy_kwh 快照值 0.5 与重算值 0.0 不符"}
```

```json
{"error": "no_valid_interval", "reason": "首条实测记录即未通过审计（购电量残差 +0.400000kWh 超容差 0.05kWh（实测 0.5kWh，核算值 0.1kWh）），不存在可结算的连续有效区间，本次结算整体拒绝"}
```

其中 `plan_mismatch` 表示快照被篡改或与所给输入不配套；`no_valid_interval` 表示首条记录即异常、没有可结算的连续有效区间；实测记录数与调度时段数不符、记录或容差非法等归入 `invalid_input`；`dispatch` 输入本身无解则沿用 `infeasible`。

## 测试

```bash
pip install pytest httpx
pytest tests/ -q
```

覆盖谷充峰放套利、效率损耗与衰减费核算、末端 SOC 约束、禁止倒送、光伏免费充电、无解诊断、各类非法输入，以及峰值与字典序两级破平局；遥测审计部分覆盖自洽记录通过、三类残差（电芯能量 / 购电量 / SOC 衔接）越限标记、容差边界（等于容差不判异常）、光伏盈余购电截断，以及容量 / 效率 / 容差 / 测量值的非法输入拦截；计划结算部分覆盖完全一致全额结算、实测偏差与费用差额、异常截断（首条异常起待结算）、SOC 断点截断、快照篡改与跨输入混用拒绝、记录数不符拒绝、首条即异常拒绝、效率与衰减费口径，以及既有接口回归。
