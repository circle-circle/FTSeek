# ADR-038: 2026-09-06 裁决执行批 — DA/UE prompt 优化 + reentry RR 倒挂修复 + 退出/隔夜架构收敛 (P0/P1/P2a/P2b) + R-12b 时间戳同源

**日期**: 2026-09-06
**状态**: 已执行 (全部落地, 全量回归 2928 passed / 37 skipped / 0 failed)
**决策者**: 用户 (逐项裁决) + Hermes (执行)
**提案文档**: docs/proposals/PROPOSAL-20260905-da-ue-prompt-optimization.md ·
docs/proposals/PROPOSAL-20260906-reentry-rr-inversion.md ·
docs/proposals/PROPOSAL-20260906B-exit-arch-convergence.md (含多模型核查记录)

## 背景

三个独立立项在同一会话链完成裁决与执行:

1. **DA/UE prompt 优化** (PROPOSAL-20260905): UE prompt 膨胀 (55 行散文依从性差)、
   挑战者取低覆写把 DA HIGH 档 77% 拉平 (打分区分度归零)、盘前榜"展示但不入围"
   自相矛盾输入、tier 间质量分层证据已够 (HARD_EVENT +$101.54/17笔 vs
   MARKET_CORROBORATED −$379.10/12笔)。
2. **reentry RR 倒挂** (PROPOSAL-20260906): n=19 死单回查实锤 LONG dev>0 追高桶
   9 笔 −96.95 是唯一出血桶 (dev≤0 回踩桶 +24.24), 与策略自身"回踩企稳"论点自洽。
3. **退出/隔夜架构收敛** (PROPOSAL-20260906B): 隔夜豁免三轨散落两处 EOD 决策面、
   `_check_playbook_exit_rules` 714 行巨石嵌在 9453 行 pipeline.py 中、
   `_skip_allowed` 同一公式三处重复计算。
4. **R-12b** (执行 0906B 回归时由棘轮抓获): US.HOOD 2026-09-04 两表 entry_time
   漂移 2.94s (常态 42-74ms) — 根因是富化 I/O (regime assess 的 VIX 重试链 +
   get_full_market_context) 夹在 trade_records 取时戳与 open_positions 自取
   now_et() 之间, 属结构性慢路径漂移, 非口径违规。

## 决策

| # | 事项 | 裁决 | 执行形态 |
|---|------|------|---------|
| 1 | UE prompt v2.9 全文重写 (55→26 行) | "如无问题可以实施" | 直接生效 |
| 2 | UE U1 渲染双开关 (极端带不渲染 + 多空对称截断) | 同上 | yaml 开关, 回滚值行内注释 |
| 3 | UE U3 tier 闸 | 同上 | **影子-only** (tier_gate_shadow), enforce 键留 false, 攒样≥15 交易日 + fwd n≥30 后裁决 |
| 4 | DA 丙1 挑战者取低覆写 → 影子记录 | 同上 | 直接生效 (保留主评分, 低分落 _challenger_score_shadow) |
| 5 | reentry R1 LONG 禁追高闸 (vwap_band_long_high_pct=0.0) | "三模型确认有效可不用影子" | 直接 enforce (影子豁免系用户显式一次性例外, 不构成惯例) |
| 6 | reentry R2① 武装时刻指标埋点 | 同上 | record-only 落库 |
| 7 | P7 军规: prompt 字段消费方登记制 | "直接按建议执行" | 入 AGENT.md + scripts/shadow_field_cardinality.py |
| 8 | 0906B P0 打假 (文档/YAML 头不管辖声明) | 批准 | 零行为变更 |
| 9 | 0906B P1 隔夜豁免三轨合一 (overnight_decision.py 单决策点) | 批准 | 行为等价合并, 360 组全组合契约对旧链钉锁 |
| 10 | 0906B P2 拆三步: P2a 引擎纯搬家 / P2b 参数单点化 / P2c exit_profile | "同意" (P2a/P2b 直接执行, P2c 留影子+裁决) | P2a: 711 行逐字迁 exit_engine.py + 转发器; P2b: 两纯助手收三处公式, poll 点不叠总开关的既有不对称刻意保留并钉测试 |
| 11 | 深水闸 (loser_overnight_max_depth_pct) | 维持 8/21 回滚裁决, 禁止直改 | 正确形态=EOD 判定时刻闸, 登记 P3 影子 |
| 12 | R-12b: entry_time 单时间戳同源 + HOOD 存量修复 | "单时间戳治本 + HOOD存量修复" | upsert_position_simple 加可选 entry_time 参数 (唯一活调用点 playbook_service 传 _now_iso); HOOD 行回填成交时刻 :09.928 (证据: dual_broker 成交日志 :09.927); R-12 棘轮 2s 容差不动 |

## 明确不做 (本期)

- P2c (exit_profile 身份证 + 双跑 comparator): 待影子数据 + diff=0 报告后再裁;
- P3 (MAE 分带隔夜政策 + EOD 作用域深水闸影子) / P4 (配置收敛 + 反分叉断言): 登记排期;
- 富化 I/O 挪出"成交→落账"关键路径 (R-12b 附带发现: 慢路径期间 SL 监控延迟 ~3s
  才启动): 独立立项, 不夹进本批;
- 深水闸任何直改 (8/21 AMZN 误杀回滚裁决在先)。

## 验证

- 全量回归: 2928 passed / 37 skipped / 0 failed (2026-09-06, 三批合并工作区);
- P1 契约: tests/playbook/test_overnight_decision_contract.py 360 组组合对旧链等价;
- P2b 契约: tests/playbook/test_exit_engine_params.py 66 例 (公式等价/组合语义/
  不对称钉/异常不吞/防忘接线);
- R-12b: test_r9_r13_contracts.py 新增 2 行为钉 (透传/CONFLICT 语义/空串回退) +
  防忘接线源码钉; HOOD 修复后 test_live_rows_aligned_with_trade_records 复绿;
- except:pass 棘轮 (955) 无新增。

## 部署与回滚

- 大部分代码已于 2026-09-06 09:00 deploy-freshness 自愈重启后在线; P2b 与 R-12b
  为该重启后落地, 随下次重启自然生效 (均为行为等价或秒级精度改善, 无窗口敏感);
- 各 yaml 开关行内注释均带回滚值; R1 回滚=改回 1.0; 整体回滚=逐提交 git revert;
- HOOD 数据修复回滚: UPDATE open_positions SET entry_time='2026-09-04T13:30:12.864694-04:00'
  WHERE symbol='US.HOOD' (旧值已留档于此)。

## 风险

- reentry R1 是唯一的行为变更闸 (LONG dev>0 直接不放行): 证据 n=19 全为死单回查,
  样本小; 缓解=回滚一行 yaml + reentry 家族有独立看板段持续观测;
- tier 闸影子攒样 ≥15 交易日, 期间 THESIS_ONLY 信号照常入场 (影子不拦) — 这是
  刻意的 (record-only), 裁决前不产生行为影响;
- P2a/P2b 为纯搬家/单点化, 行为等价由契约测试钉锁; 残留风险=未发现的外界
  monkeypatch 依赖旧路径 (已普查: 仅 test_rfc073 三处, 已随迁)。
