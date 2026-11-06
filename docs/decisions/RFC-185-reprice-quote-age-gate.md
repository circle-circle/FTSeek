# RFC-185 — 执行前报价陈旧度门 (re-price 定价那一跳太旧就不起价)

- 状态: 已落地 (SIM, 用户 2026-11-06 裁「按建议执行」)
- 类型: 交易行为变更 (会改变实际成交集合) — 入场侧, 不触退出参数
- 关联: RFC-141/158 (入场延迟确认门)、RFC-152 (执行 TCA 台账)、ADR-013 (订阅精简集)
- 落地日: 2026-11-06

## 1. 病灶

re-price 用**触发瞬间** `data_router` ctx 的 `ask`(BUY)/`bid`(SHORT) 定 entry、并按该价
算 SL/TP/仓位；而真实成交发生在之后（还要过 120s 确认门）。当这一跳 ctx 的**交易所更新龄**
偏大时，止损距离是按旧价定的 ⇒ 实际风险偏离 vol_bucket 设计的百分比。

提交层已有 `entry_max_quote_age_sec: 90`，但它量的是**提交前重新取的那一跳**（`_private_router_quote`），
量不到"定价用的那一跳"。两者是两道门，不互相覆盖。

## 2. 证据（execution_tca.jsonl，56 笔有成交的行）

`|成交价 − 执行价|` 按 re-price 时报价龄分桶（绝对值 = 定价偏差，不分方向）：

| 报价龄 | n | 中位 | 均值 | 最大 |
|---|---|---|---|---|
| <2s | 30 | 0.064% | 0.076% | 0.210% |
| 2-10s | 16 | 0.046% | 0.087% | 0.262% |
| 10-30s | 4 | 0.085% | 0.092% | 0.144% |
| **>30s** | **5** | **0.256%** | 0.223% | **0.490%** |

**悬崖在 30s，不在 10s**（10-30s 与前两档不可区分）。0.256% ≈ 26bps，已明显高于
往返费用线（≈10bps 量级），且最差 49bps。

## 3. 判据与阈值

- 阈值 `reprice_quote_age_max_ms = 30000`；`age > 30000` 严格大于才拦。
- 量纲 = `lv3_quote_age_ms`（交易所 `update_time` 到现在的毫秒），**不是** `timestamp`
  （我方取价时刻，恒 ~0）。
- `age` 取不到 / 非法 / 判定异常 ⇒ **放行**（fail-open，与全文件"取不到数据不阻断交易"口径一致）。
- `max_ms <= 0` ⇒ 门关闭语义（不是"全拦"）。

## 4. 实施

- `app/risk/data_health_guard.py` 新增纯函数 `reprice_quote_age_ms` / `check_reprice_quote_age`。
- `auto_execute_flow` 在 `_ctx` 取回后、**算 entry/SL/TP 之前**求值；拦下时：
  - 回写具名 `skip_reason = stale_quote_gate(age=NNNms > 30000ms)`
  - `_emit_exec_gate_audit(action="skipped_stale_quote", extra={gate,why,quote_age_ms})`
  - `skipped_count += 1; continue`（本轮不起价，下轮重试）
- 配置: `playbook.execution_parameters.reprice_quote_age_gate_enabled / reprice_quote_age_max_ms`。

## 5. 预注册停止条件（任一条触发 ⇒ 当天回滚，不放宽阈值、不延窗口）

| 条件 | 内容 | 触发动作 |
|---|---|---|
| **S1 丢单** | 被拦指令当日**下一轮起价成功率 < 80%**（即拦了就没再成交），连续 2 个交易日 | `reprice_quote_age_gate_enabled=false` + restart |
| **S2 经济性** | 攒满 ≥30 笔被拦事件后，被拦集（其后续若成交）的**净额均值显著差于**同期未拦集（配对/日聚类 80%CI 整体 >0 表示拦错方向） | 同上 + 记 DEB |
| **S3 结构性** | 被拦标的的 **ADV/流动性分布显著低于全体**（该门实际在筛流动性而非筛陈旧），或单日拦截 > 当日候选 30% | 同上（说明门等价于加严流动性门，非本意） |

读数面：`exec_gate_audit` 的 `skipped_stale_quote` 计数 + `skip_reason LIKE 'stale_quote_gate%'`；
被拦集的次日/后续成交与净额在 settle 脚本里按 `skip_reason` 前缀分桶。

## 6. 已知局限（先写清，不事后找补）

1. **n=5** 的 >30s 档是薄证据；30s 阈值来自"档间悬崖"而非回归，**不做参数寻优**，
   若日后 >30s 档积累到 n≥20 且中位偏差回落，允许**一次**重评审（须新预注册）。
2. **交易所更新龄与流动性正相关**：低流动性票在正常交投下龄也会 >30s。本门对它们等价于
   "更严的流动性门" —— S3 就是为此设的止损阀，而不是事后解释。
3. 方向不敏感：本门治的是"定价用的价旧"，不是"追高"。追价仍由 `chase_cap`（以生成价为锚）管。

## 7. 回滚

`playbook.execution_parameters.reprice_quote_age_gate_enabled: false` + `systemctl restart ftseek-us`
（`playbook.*` 是启动快照，热重载穿不透）。代码级回滚 = revert 同批提交。

## 8. 同批零行为变更项

- **re-price 审计保留首跳与轮数**：`metadata_json.reprice_audit` 原为每轮整块覆盖 ⇒ 只留末跳，
  累计漂移不可重建。现加 `first`（只写一次，从既有 metadata 继承）+ `rounds`（累加）。
- **未知 horizon 告警**：`get_horizon_params("medium")` 返回 `{}`（yaml 只定义 short/long）⇒
  re-price 静默回退固定 SL%/TP 倍率。实测当前 horizon 只有 short(151)/long(1)，该分支 0 次；
  只加一行 WARNING，让日后 selector 产出新 horizon 时不再无声降级（零行为变更，不新增 medium 配置）。