# ADR-026: 单名帽收紧 0.25→0.08 + 撤销已否决 mfe_stall_exit (DEB-20260815 P0)

**日期**: 2026-08-15 | **状态**: Accepted | **提交分支**: rfc-060-phase1-hotfixes
**决策人**: 用户批准 (DEB-20260815 收敛版方案 P0)

## 为何 (Why)

1. **P0-A 单名帽**: 实盘人工 8 个月券商成交史 (DEB §24/§25, n=358 回合) 两条硬教训:
   - LONG 盈利口袋 86% 集中于 MU 单名 (+$44.5k/$51.7k), 单点风险=收益本身;
   - SNDK 空头扛单 -$65.9k (无止损/无时间帽/无单名帽三重缺失)。
   SIM 侧现状同源风险: 最大单笔敞口 $6,669 = 权益 15%, `max_per_symbol=0.25` 敞着口。
2. **P0-B mfe_stall_exit**: DEB §15 八变体重放已否决 (最佳 p=0.17/捕获率 14%/双窗异号),
   但配置仍 enabled=true, 7/1 起实跑出血 -$1,353 (n=31)。撤销已否决组件=回基线, 非新规则。

## 预期 (Expected Impact)

- P0-A: 单名敞口 ≤8% 权益 (~$7.8k), 尾部风险砍 2/3; 不改变期望值, 不影响 min_notional $3k。
  消费方: position_sync.py:971 / scheduler_triggers.py:506 / llm_pipeline.py:195 / playbook db.py
  (db.py 默认 0.08 与本值天然对齐)。
- P0-B: 消除一个统计上无支撑、实跑为负的退出路径; shallow_hold 嵌套于同一守卫下随之失效。
- C 项维持不动: 水坝 cap=3 / 禁触窗 1015 / overnight_hold=false (已是正确方向)。

## 回滚 (Rollback)

- P0-A: `risk.position.max_per_symbol` 0.08→0.25, `max_position_pct` 0.08→0.15,
  `capital.tiers.medium.max_position_pct` 0.08→0.25 (HotReloader 热生效, 无需重启)。
- P0-B: `mfe_stall_exit_enabled` false→true。
- 全部为 config/unified_config.yaml 数值级改动, 零代码变更。
