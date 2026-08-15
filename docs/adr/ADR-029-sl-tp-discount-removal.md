# ADR-029: SL/TP 打折层关闭 — sl_tighten_beta 0.5→1.0, vix_tp_mult→1.0

**日期**: 2026-08-15
**状态**: 已采纳 (用户裁决)
**关联**: DEB-20260815 §三十五, ADR-028

## 背景

用户裁决: "SL/TP 请基于不同的标的和分桶, 不要打折"。

影响面分析发现 SL/TP 生成链上叠了两层**全局折扣**, 违背"按标的+分桶"原则:

1. **SL β打折 (主犯, 活跃)**: `sl_tighten_beta: 0.5` — 把 vol_buckets 按标的 ATR
   算好的 SL 距离统一打五折 (下限1.5%)。所有标的不分桶一律砍半。
   实锤: 8月指令分桶原值 SL 距离 2.13%~5.02%, β打折后实际生效 1.5%~2.5%,
   落进 7/9 证伪的 <2% 噪声扫损区边缘。
2. **TP VIX打折 (休眠)**: `vix_tp_mult: 0.9/0.7/0.5` — 高VIX时 TP 目标乘折扣。
   8月 0 次触发, 纯消除未来打折隐患。

注意: `vix_sl_widen` 是加宽 (留空间) 不是打折, 保留。vol_buckets 本身干净
(四桶 × per-symbol ATR), 打折是叠在它上面的全局因子。

## 决策

- `sl_tighten_beta: 0.5 → 1.0` — tighten_sl_by_beta 短路原样返回分桶 SL (代码已有
  `beta>=1.0 → 原样返回` 路径, 零代码改动)
- `vix_tp_mult: 0.9/0.7/0.5 → 全1.0` — 关闭 TP 市场折扣
- `scripts/settle_shadow_portfolio.py` 硬编码 `SL_BETA 0.50→1.0` 同步 (影子与生产不脱钩)

## 回滚

- SL: `sl_tighten_beta` 改回 0.5 + 重启
- TP: `vix_tp_mult` 恢复 0.9/0.7/0.5 + 重启
- 备份: config/unified_config.yaml.bak.nodiscount-0815, config/playbook_horizon.yaml.bak.nodiscount-0815

## 验证

- 实弹: tighten_sl_by_beta(LONG,100,95)=95.0 原样返回 ✓ (β=1.0 短路)
- 实弹: tighten_sl_by_beta(SHORT,100,105)=105.0 原样返回 ✓
- PlaybookConfig 读值: sl_tighten_beta=1.0, vix_tp_mult 全1.0 ✓
- 服务重启干净 (PlaybookConfig 启动快照), 零持仓窗口
