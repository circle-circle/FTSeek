# 外部 Agent 异常报告评估 — Hermes 独立审计

> 评估对象: AGENT_HANDOFF_ANOMALIES_2026-05-21.md (11 项异常)
> 评估方法: 逐项对照实际代码 + DB + 日志独立验证
> 评估时间: 2026-05-21

---

## 总评

报告质量 **中上**。数据驱动、结构清晰、优先级分级合理。但存在 **3 处事实性错误**、**2 处遗漏关键风险**、**1 处归因错误**。如果不修正就直接执行修复，可能走偏。

---

## 逐项评估

### #1 期权 selector 与 yaml 不一致 — ✅ 准确，P0 成立

**验证结果:**
- yaml `delta_range: [-0.4, -0.3]` (OTM) — 确认
- code `options_selector.py` 全模块 0 处读取 yaml/config — `grep config/yaml/get_config` 返回空
- 代码标题明确写 "ATM Put 期权选择器"，hardcode `delta=-0.5`
- 实际交易: 仅 2 笔 SHORT_PROXY_OPTION (QQQ 5/20 -$2,934, QQQ 5/21 -$3,050)

**⚠️ 小修正:** 报告中列了 4 笔期权入场 (SNDK/SPY/MU/QQQ)，但 DB 实际只有 2 笔 SHORT_PROXY_OPTION 已平仓。其余可能是入场日志而非实际成交。损失总额 -$5,984 与 DB 的 -$5,984 (2,934+3,050) 一致。

**修复建议可行:** 让 selector 读 yaml 配置，方向正确。但 "没有合适期权就不入场" 这个 fallback 逻辑需要仔细设计——当前系统可能在 selector 返回 None 后仍有 fallback 到 ATM 的路径。

---

### #2 期权硬止损 -50% 来源不明 — ⚠️ 部分正确，归因错误

**验证结果:**
- yaml `stop_loss_pct: -0.3` — 确认
- yaml `stop_loss_ratio: 0.5` — 存在，但 **不是止损阈值**

**关键代码追踪:**
```
option_quick_exit.py:39  → self.stop_loss = config.get("stop_loss_pct", 0.25)  # 默认25%
position_monitor.py:395  → qe.stop_loss = abs(cfg.get("options",{}).get("stop_loss_pct", -0.25))  # 覆盖为0.3
```

**结论:** 按代码逻辑，stop_loss 应该被 position_monitor 覆盖为 `abs(-0.3) = 0.3` (30%)。但日志显示 -55% 和 -58.9% 才触发。

**问题在于:** exit_reason 被截断 `reason=f"QUICK_EXIT_{exit_reason[:20]}"`，我们看不到完整的触发阈值信息。实际可能是 **价格获取延迟** 导致 PnL 已经跌到 -55% 才触发检查（应该 -30% 就触发但轮询间隔太长）。

**agent 归因错误:** `stop_loss_ratio: 0.5` 在代码中仅用于 `batch_processor_us.py` 的统计指标，不影响止损逻辑。真正的根因更可能是 **监控轮询频率不够** 或 **价格获取失败后跳过检查**（position_monitor.py:380-386 有价格获取失败时 continue 的逻辑）。

---

### #3 5/19 修复全部 0 触发 — ❌ 事实错误，部分有效

**这是报告最大的事实性错误。**

**实际日志:**
```
grep Phase 1 关键词 → 44 行匹配
```

全部 44 行是 `Scalping·低价股门` (TME 低价拦截)，从 5/19 22:27 开始持续触发。

| 门控 | 触发次数 | 说明 |
|------|----------|------|
| 低价股门 | 44 | ✅ 生效中，拦住 TME |
| regime门 | 0 | ⚠️ 可能市场状态未触发 |
| sl冷却 | 0 | ⚠️ 可能无 SL 命中事件 |
| CB噪声 | 0 | ⚠️ 可能无 CB 触发条件 |

**PostTradeHook stats {0,0,0}** — 确认。L3 thesis **0 触发** — 确认。

**修正结论:** 不是 "全部 0 触发"，而是:
- 低价股门 **正常工作** (44次拦截 TME)
- 其他 3 个门控因前置条件不满足未触发 (不一定是 bug)
- LLM 链路 (thesis/hook) 确实 0 触发 — 这才是真正的问题

---

### #4 持有 4 个看跌期权 — ✅ 正确，但有重大遗漏

**验证结果:** 4 个 PUT 持仓确认，总 cost 计算正确 (~$13K)。

**⚠️ 重大遗漏 — 还有 3 个幽灵股票持仓:**

```
US.QCOM  12股  entry=$200.84  sl=$192.80
US.SNDK   7股  entry=$1,426.84  sl=$1,369.77
US.ORCL  80股  entry=$184.95  sl=$185.33
```

Reconciler 日志 (5/21 05:44) 明确标记这 3 个为 **ghost** (local_only，broker 端无对应持仓)。

这意味着:
1. ORCL 80 股 ($14,796) 是本地记录的幽灵仓位 — broker 没有，但系统以为有
2. SNDK 7 股 ($9,988) 同理
3. QCOM 12 股 ($2,410) 同理

**风险:** 这些幽灵仓位会占用风控额度、影响 PnL 计算、可能干扰新的入场决策。比 4 个 PUT 的 theta 衰减更紧急——PUT 至少是真实持仓，幽灵仓是数据污染。

---

### #5 Shadow EOD cron 未配置 — ✅ 准确

**验证结果:**
- `shadow_eod_replay.py` 存在 (5/19 创建，10KB)
- cron/systemd 无任何 shadow 相关配置
- 174 笔 shadow_trades 全部 closed=0

报告建议可行。补充：cron 时区注释写 "04:30 ET = 16:30 北京"，但 ET 夏令时 UTC-4 → 北京 UTC+8 = 差 12h，16:30 北京时间 = 04:30 UTC，不是 04:30 ET。应该改为 `0 4 * * 2-6` (UTC) 或者用 `TZ=America/New_York 30 4 * * 1-5`。

---

### #6 Universe 仍是老配置 — ✅ 准确

**验证结果:**
- yaml universe 确认仍含 TSLA/AMD/NIO/NVDA/MU/LCID
- ORCL 22 笔 +$20,492 真 alpha 不在 universe — 确认
- ORCL 有 80 股 open_position 但不在 universe → 通过非 universe 路径订阅 (scheduler_core.py:1022 注释确认)

**补充风险:** ORCL 80 股幽灵仓 (见 #4) 说明 ORCL 的订阅/入场链路有问题——它不应该在 open_positions 中有记录，除非通过非标准路径进入。加 ORCL 到 universe 之前应先排查为什么它已经以幽灵仓形式存在。

---

### #7 Ghost cleanup cruise — ✅ 核心正确，日期有误

**验证结果:** 5 笔 ghost_cleanup_cruise 确认。

**日期修正:** 5 笔全部 trade_date=2026-05-20 (不是 5/21)。entry_time 是 5/21 凌晨 UTC (对应美东 5/20 晚间)，说明是 5/20 夜盘时段的交易。

**BA 1h 内 3 次入场** — 确认。cooldown 确实失效。

**与 #3/#11 的关联分析正确:** broker 调用失败 → 永远到不了 L3/PostTradeHook。

---

### #8 期权 SL 数据异常 — ✅ 准确

**验证结果:** TSLA PUT sl=$13.73 > entry=$12.91，sl_pct=+6.3%。

对 LONG PUT 来说，SL 必须 < entry。SL > entry 意味着:
- 永远不会触发止损 (价格涨到 SL 反而是盈利)
- 或者方向逻辑搞反了

根因需要查 position_sync 中期权 SL 的计算。当前 `position_recovery.py:378` 用 `entry_price * (1 - STOP_LOSS_RATIO)` 计算 SL，但 `position_sync.py:745` 用 `avg_entry_price * sl_mult`，sl_mult 的计算逻辑可能有 PUT/CALL 方向问题。

---

### #9 LLM 业务调用 0 — ✅ 准确

**验证结果:** `llm_audit.db` 仅 `test_feature_xyz` 7 笔，业务 feature 0 行。确认。

---

### #10 PRE_TRADE_CHECK 拒绝 101 笔 — ✅ 准确

**验证结果:** PRE_TRADE_CHECK 101, SHORT_TRIPLE_CONFIRM 32, SHORT_STRENGTH_WEAK 8。确认。

**补充:** agent 问 "5/19 删了 short_min_strength<20，怎么还有 SHORT_STRENGTH_WEAK 8 笔？" — 这值得追踪。可能是:
1. 服务没重启，跑的是旧代码
2. 删除不彻底，还有其他地方检查 strength
3. 这 8 笔发生在 5/19 之前 (需要看 timestamp 分布)

---

### #11 Phase 1 修复 0 日志 — ❌ 与 #3 同样事实错误

同 #3 分析。低价股门有 44 行日志。其他 3 个门控未触发可能是前置条件不满足而非代码未部署。

---

## 报告遗漏的关键风险

### 遗漏 1: 幽灵仓位 (比 PUT theta 更紧急)

3 个股票持仓 (ORCL/SNDK/QCOM) 被 Reconciler 标记为 ghost，总计约 $27K 名义价值。这些是数据污染，会导致:
- 风控额度虚占
- PnL 计算偏差
- 新入场决策受干扰

### 遗漏 2: 价格获取失败时跳过止损检查

position_monitor.py:380-386 在价格获取失败时 `continue` 跳过。如果期权价格频繁获取失败，止损检查就会被跳过——这可能是 #2 的真正根因 (不是 -50% vs -30% 的配置问题，而是检查根本没跑到)。

---

## 修正后的优先级建议

| 优先级 | 项目 | 原报告 | 修正 |
|--------|------|--------|------|
| P0-0 | yaml options.enabled=false 止血 | ✅ 同意 | 立即执行 |
| P0-1 | 平 4 个 PUT | ✅ 同意 | 今晚执行 |
| **P0-2** | **清理 3 个幽灵仓位** | **❌ 遗漏** | **比 PUT 更紧急** |
| P1-1 | 排查 ghost_cleanup 根因 | ✅ 同意 | 是 #3/#11 的前置 |
| P1-2 | Shadow EOD cron | ✅ 同意 | 修时区 |
| P1-3 | Universe 更新 | ✅ 同意 | 先查 ORCL 幽灵仓 |
| P1-4 | 期权 SL 计算修复 | ✅ 同意 | |
| P2-1 | 期权 selector 读 yaml | ✅ 同意 | 短期不启用 |
| P2-2 | LLM 链路诊断 | ✅ 同意 | |
| P2-3 | 止损检查频率/跳过逻辑 | **❌ 遗漏** | #2 的真根因 |

---

## 总结

| 维度 | 评分 | 说明 |
|------|------|------|
| 数据准确性 | 7/10 | DB 查询结果可靠，但日期有偏移、交易笔数有误 |
| 事实完整性 | 5/10 | 漏掉幽灵仓位、价格获取跳过逻辑、Phase 1 部分生效 |
| 根因分析 | 6/10 | #2 归因错误 (stop_loss_ratio 非根因)，#3/#11 结论过强 |
| 修复建议 | 7/10 | 方向正确，但部分建议基于错误前提 |
| 优先级排序 | 7/10 | 整体合理，但幽灵仓应提到 P0 |
| 工作守则 | 9/10 | "先读不写""小改快验""失败必降级" 都是好实践 |

**建议:** 修正上述错误后，报告可作为修复工作的有效输入。不建议直接按原报告执行——特别是 #3/#11 的 "全部 0 触发" 结论会误导排查方向。
