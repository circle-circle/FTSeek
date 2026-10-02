# UE v2.14 证据、风险与 DA 溯源升级

## 结论

UE 保留直写供给与 DA 辩论双通道，执行出口继续统一收敛到 `playbook_instructions` 与既有 admission/auto_execute 链路。本批不强制所有 UE 走 DA；UE 直写只增加证据质量门，DA feed 保留完整 UE 上下文由 DA 辩论独立裁决。

## 行为变化

1. 报价新鲜度：Futu push/单只/批量快照透传交易所 `update_time`；UE 初筛只把真实 `source_asof`/`update_time` 当报价时点，不再把抓取 `timestamp` 洗成 `source_asof`。
2. 输出契约：`PROMPT_VERSION=v2.14`、`BRIEF_TEMPLATE_VERSION=ue_brief_v5`。要求 `confidence_basis`、`counter_evidence`、`event_phase`、`move_basis`、固定三段 `causal_chain`、SHORT `borrow_data_basis` 与四项 `evidence_checklist`。
3. 置信校准：`evidence_citation_state=transmission_assumed` 时置信度封顶 0.55；直写通道若 `price_volume_confirmed=false` 则拒绝，DA feed 不受该直写门限制。
4. 风险分层：`PLANNED_PREVIEW_VERSION=ue_planned_v3`。PRICE_IN、UNQUALIFIED、THESIS_ONLY 或传导假设走保守 SL/TP；输入初筛 ATR 仅作上限，不放大风险，并记录 `move_calibration`。
5. UE→DA 溯源：批次 ID、引证状态、校准、反证、事件阶段、波幅依据与 transmission 标记透传 DA 分析、bridge 与 raw 指令；DA prompt 要求正面回应 UE 反证。

## 配置与回滚

新增配置位于 `config/unified_config.yaml` 的 `universe_expansion` 段：

- `transmission_assumed_confidence_cap: 0.55`
- `transmission_assumed_require_market_confirmation: true`
- `risk_layering_enabled: true`
- `conservative_stop_pct: 2.0` / `conservative_tp_pct: 4.0`
- `atr_stop_multiplier: 1.25` / `atr_tp_multiplier: 2.0`

回滚开关：置信确认门设为 `false`；风险分层设为 `false` 可恢复标准 3%/6% 口径。Prompt/brief/preview 版本号不可回拨，历史样本必须按落库版本切分。

## 验证

- UE prompt/evidence/数值契约：338 passed。
- UE funnel、LONG gate、DA candidate selection：167 passed。
- `py_compile` 覆盖 `llm_universe_scan.py`、`futu_lv3_provider.py`、`shadow_field_cardinality.py`。
- `unified_config.yaml` 新增键值已解析核对。

上线后观察：`transmission_assumed` 占比与封顶触发率、直写拒绝数量、DA 对 UE 反证的显式响应率、`risk_profile` 分布、`move_calibration=exceeds_2atr` 占比，以及新增枚举字段的基数自检结果。
