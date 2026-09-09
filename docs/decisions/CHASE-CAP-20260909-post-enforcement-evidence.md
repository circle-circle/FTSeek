---
title: chase_cap 追高帽转正后证据账 (首日) + 残留三项已裁
date: 2026-09-09
status: 已裁 (2026-09-09 用户「按建议执行」, 残留三项处置见 §3)
---

# chase_cap 追高帽：转正后证据账（首日）与残留三项已裁

> 触发：用户令「起草追高帽影子转正的裁决材料」。取证后发现前提已过时——
> **LONG 侧 chase_cap 已于 2026-09-05 转正为硬闸**，故本文改为「转正后首日证据账 +
> 残留三项待裁」。所有数字可复算，出处逐条标注。

## 1. 事实更正：chase_cap 已转正，不是待转正

| 项 | 证据 |
|---|---|
| enforce 开关现值 | `config/unified_config.yaml:1385` `chase_cap_enforce_enabled: true`（热载可回滚，置 false 退回纯遥测） |
| 转正通道 | P0 通道而非 B 系影子毕业：`87ebc785`（9/5 07:19 落盘）+ DEB-20260905 G-06（闸移出 reprice try + **fail-close**），9/5 14:15 随 `auto_execute` 恢复的 Tier-A A1–A9 同批上线（`docs/STRATEGY-BRIEF-FOR-EXTERNAL-REVIEW.md` §12） |
| 上线核验 | M-7 替代控制 = 9/8 开盘首小时人工盯盘，三项在场核验含「chase_cap 遥测/enforce」——9/8 日志证实两者俱在 |
| 代码现状 | `app/playbook/pipeline.py:4246-4370`：遥测写盘先于 enforce、enforce 在去重门之外、闸门不可用 → fail-close 终态跳过（`chase_cap_gate_unavailable`） |

## 2. 转正后首个完整交易日证据账（9/8 ET，21:30→04:00 CST）

日志跨轮转档拼接（`logs/ftseek_us.log` + `-20260909`，轮转盲区修复后口径，见
`docs/reviews/REVIEW-20260909-postclose-recap.md` §C）：

| 指标 | 值 | 说明 |
|---|---|---|
| 拦截事件 | **148 次**（56 条日落于 9/8 + 92 条于 9/9） | 每 ~5 分钟重评一次 ⇒ 事件 ≠ 指令数 |
| 涉及指令 | **仅 2 条** | US.SKHY #2311 ×76 次 / US.SNDK #2310 ×72 次 |
| SKHY 结局 | 帽 $182.00，被拦价 $184.06→$185.21，**全程未成交**（`executed=0`） | DA 计划原文即「禁止在 $182 以上追涨」（`obsidian_vault/symbols/US_SKHY.md`）——**闸 = 计划自定禁追线的机械执行**，不是外加约束 |
| SNDK 结局 | 帽 $1,760.00，被拦价 $1,766.75；价格回落帽下后 **15:43 ET 成交 @1,752.665** | 成交价比被拦价好 **0.80%**（reprice→拦→回落→低价成交路径按设计走通）；该仓 3 股现持有中，台账水深 −0.92%（9/8 过夜仓） |
| 反事实缺口 | SKHY 被拦后价格路径未复算 | 本次取证未拉 bar；若需「拦错成本」读数，须走 cf 族尺子补算，本文不估 |

**读数**：机制按设计工作——拦的是计划自定的线、拦后重评让价格回落者低价成交、
价格不回落者全程放弃。首日证据方向支持维持 enforce；**单日 n=2 指令，不构成
统计结论**（宪章第 9 律），意义在于「机制定性验证 + 反例零出现」。

## 3. 残留三项（已裁：2026-09-09 用户「按建议执行」）

1. **日志措辞与行为相反**（REVIEW §F-1）：`pipeline.py:4353` 打 `→ 终态跳过`，实际
   每 ~5 分钟重评（SNDK 正因重评才成交）。改 = 动最热路径文件的日志字符串，需重启生效。
   **建议**：随下一批改动带走，不单独立案。
   **✅ 已执行**：措辞改「→ 本轮跳过 (每~5min重评, 回落帽下即放行; P0-A enforce)」，
   上方注释块「首违即终态, 本就无第二次」同步更正（与 SKHY ×76 事实矛盾）。
2. **`skip_reason` 成交后不清空**（REVIEW §F-2）：`executed=1 AND skip_reason<>''`
   共 12/495 行（SNDK 在列），任何按 skip_reason 统计「为何没成交」的查询会误计。
   改点在 `db.py`/`playbook_service.py`。**建议**：登记为下批交接项，修复时连带
   `skip_reason` 消费方普查。
   **✅ 已执行**：消费方普查 6+ 处受影响（dashboard chain / premarket_trend_verify /
   plan_vs_actual / short_supply_funnel / gap_semantics / band_vol 均按 `skip_reason<>''`
   判「跳过」）；修复落在 `db.py:update_instruction_tracking` 单点不变式
   （任何写 `executed=1` 且未显式给 skip_reason ⇒ 强制清空；`try_claim_execution`
   下单前抢占不经该函数，不受影响）；契约测试 ×3（`test_playbook_db.py`）全绿。
   历史 12 行残留不回填（家法）。
3. **SHORT 侧追低帽仍是影子**（`short_chase_ban_shadow_enabled: true`，阈值 3.0%）：
   `data/short_chase_ban_shadow.jsonl` 自 8/24 仅 **n=5**（NTES/MSTR/BIDU×2/DDOG），
   且无 outcome/反事实列 ⇒ 不可裁。样本率 ~0.45 例/交易日，到 n=20 约需 44 个交易日
   （≈10 月末）。**预注册裁决口径（请裁）**：n≥20 would-block + 逐笔反事实
   （被拦价 vs 当日剩余时段极值/收盘）方向一致 ≥70% ⇒ 转正；若 10/31 仍 n<20，
   则裁「退役该影子」或「降档为纯日志」，不再无限攒样。
   **✅ 口径已采纳**：维持影子攒样，10/31 死线按上述预注册口径裁决。

## 4. 回滚条件（既有，不重立）

`chase_cap_enforce_enabled: false`（热载免重启）退回 record-only 遥测；
fail-close 分支不变。LONG 帽值由各指令 `chase_cap_price`（DA 产出）承载，
本闸不含全局阈值，无参数可漂移。

---

*取证：2026-09-09 上午，工作区收口批（968b938e…0b539460）之后；测试基线
3479 passed / 1 failed（`test_except_pass_not_growing` 既有红，REVIEW §F-12 已归因）。*
