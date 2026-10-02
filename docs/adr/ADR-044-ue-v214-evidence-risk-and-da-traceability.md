# ADR-044: UE v2.14 证据校准、风险分层与 DA 溯源

**日期**: 2026-10-02  
**状态**: 已批准实施  
**决策人**: 用户  
**影响链路**: UE 输入初筛、UE 直写、UE→DA feed、`playbook_instructions` 统一执行出口

## 背景

昨日 DA/UE 合并产出净收益为正，但复盘发现 UE 输入存在报价新鲜度口径缺陷：抓取时间被伪装成报价源时点，可能把陈旧报价判为新鲜。同时 UE prompt 缺少显式反证、事件阶段、波幅依据与传导假设校准，UE→DA 溯源字段也不完整。

## 决策

1. Futu 快照透传交易所 `update_time`；UE 初筛只认真实 `source_asof`/`update_time`，缺失时保持 `UNKNOWN`。
2. UE prompt 升级为 v2.14：要求证据分层、反证、事件阶段、波幅依据、固定三段因果链与 SHORT 借券依据。
3. 传导假设置信封顶 0.55；UE 直写通道要求价格量能确认，DA 辩论通道保留完整 UE 上下文并独立裁决。
4. UE 直写启用风险分层：弱证据或 PRICE_IN 使用保守 SL/TP；ATR 只作上限，不放大风险。
5. UE→DA 透传批次、引证、校准、反证、事件阶段、波幅依据与 transmission 标记；所有执行仍统一写入 `playbook_instructions` 后由既有 admission/auto_execute 链路处理。

## 回滚

- `universe_expansion.transmission_assumed_require_market_confirmation=false` 关闭直写确认门。
- `universe_expansion.risk_layering_enabled=false` 恢复标准 3%/6% SL/TP。
- Prompt/brief/preview 版本号不回拨，历史样本按落库版本切分。

## 验证

UE/DA 核心回归 545 passed；`py_compile`、配置解析和 DA 外部 pipeline 语法检查通过。上线观察 transmission 封顶触发率、直写拒绝数、DA 对 UE 反证响应率、风险分层分布与新增枚举字段基数。

## 追加: 风险分层参数的转正前置 (2026-10-02, RFC-129 §② 独立复核登记)

复核方在本 ADR 落地后独立查证，登记一条前置，**不修改上文任何结论或参数值**：

- `risk_layering_enabled` 是**直接启用**（非 shadow 先行），而它改的是 SL/TP 数值
  ⇒ 属参数类行为变更。当前不构成风险，两条实测理由：
  ① UE 直写通道**零实弹流量** — 近 7 日 `UE-` 前缀指令 281 条 / 执行 **0** 条
  （成交全归 DA 32 + un 3 + EXT 1）；② 方向是**收紧**（`min(base, conservative)`
  降档 + ATR 只做上限，不放大风险）。
- **前置**：UE 直写通道**转正（开始真成交）之前必须复核 `conservative_stop_pct=2.0`**。
  依据：2.0% 落在既有实证的「<2% 噪声扫损区」附近（7/9 分桶 WR47% 亏 $1352）；
  且与 `trade_records.mae_pct` 分布对照 — 现行参数时代（entry ≥ 2026-08-20, n=283）
  中位 MAE 仅 0.33%、MAE ≥ 2% 占 18.0%，2% 档的绑定率会显著高于 5% 档
  （5% 仅 2.5%）⇒ **不是同一性质的死参数，需单独验证**。
- 零实弹样本期间无法攒 shadow，故只登记前置、不在本 ADR 内裁决。
  复核入口与数据见 `docs/decisions/RFC-129-blocked-release-push-and-sl-axis-verdict.md` §②。
- 回滚键沿用上文：`universe_expansion.risk_layering_enabled=false` 恢复 3%/6%。
