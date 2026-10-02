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
