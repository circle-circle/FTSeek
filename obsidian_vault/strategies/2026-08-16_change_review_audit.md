---
date: 2026-08-16
type: change-review-audit
scope: 8/12以来代码/配置/cron变更 (DEB-20260815系列)
verdict: 整体合理, 无回滚项; P1×1(文档卫生) P2×2(pending patch治理/联合效应)
---

# 变更复核审计 8/16 (DEB-20260815系列 + cron)

## ✅ 合理且已生效 (逐条实证)

| 变更 | 验证 |
|---|---|
| 进程/配置时序 | 重启8/15 23:08 > 最后代码提交22:23; 0ed2c12c预算门fix(mtime 23:06)在重启前 ✓ |
| cap=8 | yaml L1048=8, 消费端pipeline.py:3158 pb_cfg.get ✓ |
| blackout=0935 | yaml生效, 消费端可达 ✓ |
| W_WEEK(5交易日) | loser_overnight_max_hold_days=5, 3处调用点(6012/6231/7203) ✓ |
| SL不打折 beta=1.0 | yaml L1099=1.0, config.py:179读取→不打折 ✓ |
| 单名帽0.08 | yaml L1383, 3消费端get_dotted ✓ |
| W_WEEK回放数字 | 独立复跑: W_WEEK +$629.5 > W_SL +$463.2, n=15, 精确吻合claimed ✓ |
| mfe_stall撤销 | enabled=false, 代码保留fail-safe ✓ |
| 仙股拦截 | min_entry_price=5.0, admission PRICE_TOO_LOW判据接线 ✓ |
| 8/16删18个cron | 全部enabled=False旧job(合并治理后清理), 合理 ✓ |
| E1从未跑 | 8/15周六11:45创建, 错过当日04:45, 首触发=8/18周二 — 非故障 ✓ |
| alpha-backfill超时 | 根因FutuLV3历史K挂起, 8/15 15:18已修(pending=0早退), 手动实跑0.1s rc=0, 8/18自愈 ✓ |
| 止损监控/盘前趋势/盘中late | 脚本存在+语法OK+last_status=ok ✓ |
| 8/12影子钩子 | stale/entry_sync/stall 3项挂载+enabled, 数据更新至8/14 ✓ |
| untrack data/ | 白名单仅config_freeze_v2, DB文件仍在磁盘(15.8M/13.6M) ✓ |

## ⚠️ P1 (文档卫生, 非运行时风险)

**DEB-20260812 blackout-1015-oos 状态头过期**: 仍写"OOS进行中(裁决日8/26)", 但参数8/15已改0935(DEB§33.2用户直裁覆盖)。8/26裁决日若有人按此DEB裁决=裁决已不存在的参数。cron无对应裁决job, 故无自动触发风险, 但需更新状态头为"被0935覆盖, OOS作废"。

## ⚠️ P2 (治理项)

1. **pending-governance patch是陈旧基线**: `docs/debates/qoder_comment_cleanup_unified_config.patch.pending-governance` 写入 cap=5/blackout=1015 — 若apply将**回退**当前cap=8/0935。建议废弃或标注"已被8/15变更覆盖"。deadfetus patch已验证应用(口径警示+SQL过滤均在)。
2. **联合效应零回放(DEB§35.5自认)**: cap8+0935+W_WEEK+SL不打折四项同时放宽, 单项有防御, 联合无回放。观察项已登记(0935-1015入场SL触出局分布)但暂无专项shadow采集。sim期可接受, 需在周报跟踪。

## 结论

8/12以来变更链(核心盘经验挖掘→用户三问→落地)程序合规: 每项有回放裁决/ADR/回滚条款, 数字可复现, 消费端接线完整, 重启时序正确。**无P0, 无需回滚。** 8/18周一盘前关注: alpha-backfill自愈确认 + E1影子首跑产出。
