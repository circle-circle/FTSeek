---
title: "RFC-164: 回归红点清理（17 项既有失败归零）"
status: implemented
module: governance
related_code:
  - app/review/shadow_lexicon.py
  - app/playbook/shadow_registry.py
  - scripts/config_key_census.py
  - scripts/rth_zero_fill_autorelease.py
  - scripts/settle_winner_overnight_shadow.py
related_okr: OKR-2026-11
related_deb: DEB-20260907-prompt-contract-role-boundary-review
tags: [regression, w9, w456, shadow-registry, test-caliber]
updated: 2026-10-23
---

# RFC-164: 回归红点清理（17 项既有失败归零）

> **状态**: 已实施
> **日期**: 2026-10-23
> **作者**: Qoder (Bot)
> **审批人**: 用户（「按建议优化」并授权合并/重启）

## 摘要

主仓全量回归在 DA 契约批之后出现 **17 个既有失败**（7726 passed / 17 failed / 72 skipped）。这些失败均不在 DA 契约批 diff 内，属于词典终态文案、日志棘轮、测试陈旧、配置普查和一处代码回归。本 RFC 记录逐项修复，最终主仓全量 **7744 passed / 72 skipped / 0 failed**，外部 DA skill **113 passed**。**不修改任何入场门、仓位、止损/止盈、交易频率。**

## 问题陈述

全量失败分为六类：

| 类别 | 数量 | 根因 |
|---|---:|---|
| W9 词典↔注册表矛盾 | 9 | `premarket_invalidation` / `eth_relative_volume_gate` 已在注册表 KILLED，词典仍保留活跃措辞 |
| 治理/卫生 | 3 | `da_skip_leveraged_etf` 仓外消费未登记；`rth_zero_fill_autorelease.py` 硬编码废弃 live 单库；已完成 one-shot cron prompt 内嵌只读 SQL |
| 数据依赖 | 1 | factor 回归测试用滚动 90 日窗，新增交易使 `pe_below_industry` 合法转为 positive |
| 测试契约陈旧 | 2 | playbook hotfix 已删第二处报价兜底；UE quote freshness 断言固定旧日期 |
| 影子治理 | 1 | `premarket_invalidation` 判据结题后产物仍被 W7/freshness 只读消费，未显式认领 |
| 日志棘轮 | 1 | `settle_winner_overnight_shadow.py` 3 处 warning 缺 `exc_info` |

## 提议方案（已实施）

1. **W9 词典终态对齐**：更新两条 conclusion 为 KILLED 终态措辞；`eth_relative_volume_gate` 保留真轴 `would_pass_relative=0/30`，删除旧“53/30 达门”假陈述。
2. **W456**：`settle_winner_overnight_shadow.py` 三处 warning 补 `exc_info=True`。
3. **playbook helper 测试**：hotfix `55e4331de` 已将成交价兜底改为券商终态累计量价；测试从“两处 helper”改为“仅 arrival price 一处”，不恢复陈旧报价兜底。
4. **影子消费者**：`_CONSUMER_ACK` 新增 `premarket_invalidation`，理由 = W7 干净昨收快照 + freshness watchdog 只读消费，非交易链；采集保留。
5. **UE quote freshness**：断言由固定 `== 2026-10-09` 改为 `>= 2026-10-09`，保留“已裁 NOT_READY 不得自动提升”硬断言。
6. **配置普查**：`da_skip_leveraged_etf` 登记为仓外 `deep-stock-analysis/pipeline.py` 消费（RFC-159）。
7. **死库字面量**：`rth_zero_fill_autorelease.py` 改用 `get_trade_records_db_for_arm`；sim/real 路径语义不变。
8. **Hermes prompt 守卫**：只扫描可执行 job；`enabled=false/state=completed` 的一次性验证 job 不再扫描；新增“enabled job 内嵌 SQL 仍告警”钉子。
9. **factor 回归窗**：测试冻结到 RFC-112 评估窗 `2026-06-28→2026-09-26`；判据/阈值未动，避免生产新交易导致 verdict 漂移。

## 影响范围

- 交易行为：**无**。不改 gate、仓位、SL/TP、VWAP、频率、退出参数。
- `rth_zero_fill_autorelease`：DB 解析从硬编码改为 SSOT resolver，解析结果语义不变。
- Hermes prompt 守卫：仅对已完成 one-shot 豁免；enabled job 仍受 SQL 告警约束。
- factor 测试：仅冻结评估窗，不改分类器或阈值。
- 词典/注册表：仅展示与治理文案，不改判据。

## 风险评估

### 最坏情况

- 词典文案改错导致周检/推送显示与注册表不一致；测试 W9 会直接拦截。
- `_POSITION_DBS` 解析在异常环境下返回不同路径；单测已覆盖 monkeypatch，生产 sim/real 语义与旧硬编码一致。
- Hermes 守卫放宽后，已完成 job 若被重新启用且含 SQL 不会被发现；新增测试仅豁免 `state=completed`，`enabled=true` 仍扫描。

### 缓解措施

- 全量回归 + 受影响组测试 + 外部 skill 全套。
- 所有改动可按 commit revert；不涉及数据库迁移或订单状态。

## 验证计划

```bash
cd /opt/ftseek/FTSeek-US
./venv/bin/python -m pytest tests -q
./venv/bin/python -m pytest \
  tests/test_rth_zero_fill_autorelease.py \
  tests/test_hermes_job_prompts.py \
  tests/test_config_key_census.py \
  tests/test_dead_db_literal_gate.py \
  tests/test_factor_stats_denominator.py \
  tests/test_lexicon_consistency.py \
  tests/test_shadow_artifact_consumers.py \
  tests/test_w456_log_hygiene_ratchet.py \
  tests/test_playbook_private_router.py \
  tests/test_ue_quote_freshness_shadow.py -q
cd /root/.hermes/skills/deep-stock-analysis
/opt/ftseek/FTSeek-US/venv/bin/python -m pytest tests -q
```

实测：主仓 **7744 passed / 72 skipped / 0 failed**；受影响组 **141 passed**；外部 skill **113 passed**。

## 回滚计划

```bash
git revert 3294d1516
```

**回滚触发条件**：W9/词典展示异常、配置普查误报、或 `rth_zero_fill_autorelease` 路径解析异常。回滚不涉及重启交易参数；`shadow_registry`/`shadow_lexicon` 展示面回滚后需重启主服务才生效。

## 替代方案

### 方案 A：删除已完成 one-shot cron job

会丢失验证历史；不选。改为只对 `state=completed` 豁免，并保留 enabled-SQL 告警钉子。

### 方案 B：恢复第二处报价兜底

hotfix `55e4331de` 已明确删除陈旧报价兜底（用券商终态累计量价替代）；恢复会重新引入旧风险，不选。

## 审批

- [x] 5 问自检完成
- [x] 测试覆盖完成
- [x] 回滚路径明确
- [x] 用户授权「按建议优化」并合并/重启

## 实施记录

**实施日期**: 2026-10-23
**Commit**: `3294d1516`（清理批）；本 RFC 随附提交
**验证结果**: 主仓 7744 passed / 72 skipped / 0 failed；外部 113 passed
