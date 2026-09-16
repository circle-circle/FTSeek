# RFC-081: 入场确认门落地设计 — B案(2min延迟复核) vs D案(回调限价), 裁决日直用

> **状态**: 草稿 (预裁决设计包 — 两案均不实施, 等 D2'/E3 影子裁决日二选一)
> **日期**: 2026-09-11
> **作者**: Hermes (qwen3.8-max)
> **审批人**: 用户 (裁决日)

---

## 摘要

针对 MFE<0.3% 入场墓地 (30天 64笔 净−$4,860), 两条正交路线已在影子攒样:
**B案 = D2' 触发延迟确认** (穿越后等 2min, 价格仍站触发方向才进场) /
**D案 = E3 PULLBACK_LIMIT 回调限价** (穿越不买, 挂限价在触发线上等回踩, TTL=300s)。
DEB-20260909 §5.2 已强制披露两者互斥 (同一触发时点只能选一种语义)。
本 RFC 预先定死两案的落地工程细节与交互规则, 裁决日 whichever 过线即按本文件直接实施,
不再重新设计。**本文件零代码零行为变更。**

---

## 问题陈述 (数据基线, 2026-09-10 复核)

- 30天 (8/11–9/10) 净口径: SL_INITIAL 20笔 −$3,341 (avg MFE=0.02% = 入场即错);
  OVERNIGHT_GAP_STOP 3笔 −$911。MFE<0.3% 桶 64笔 −$4,860 ≈ 全部净亏。
- D2' 影子首日回补 (60天 412笔×2腿, 2026-09-10 05:58 结算):
  - N=2min: 回避率 50.2%, 回避组亏损单 98 笔省 $4,657 / 误杀赢家 109 笔丢 $3,102
    → 净回避价值 +$1,555; 延迟成交组机会成本均值 0.125% (< 0.3% 预注册门)
  - SL_INITIAL 子集: 回避 14/21 (66.7%), 省 $2,206, 机会成本仅 $86 → 净 +$2,120
  - N=5min: 机会成本 0.336% 超门 → **N=5 全体口径已死, 落地只可能 N=2**
- E3 archetype 影子: pullback_limit_shadow.jsonl 337 行 (9/2–9/10),
  触发行含 chase_cap_price/repriced_entry 全上下文; 生产开关
  `entry_archetype_routing_enabled: false` (纯影子)。
- 反向先验 (强制披露): ue_confirm_gate 8轮实证 10/30min 确认方向 92% 不变但
  +0.50%/笔 (t=5.67) — 长延迟在本系统有成本前科; 2min 是微结构噪声带复核, 域不同,
  裁决以 D2' forward 新窗冻结值为准。

---

## 裁决门 (预注册, 引自 D2' 脚本头, 数值已冻结)

| 判据 | B案 (D2') | D案 (E3) |
|---|---|---|
| 样本 | forward 新窗 n≥20 (count_mode=where:status=ok) | 影子结算配对样本 (cf_pullback_limit_replay 口径) |
| 主判读 | 避免率≥30% ∧ 机会成本<0.3%; 双人群 (ALL + SL_INITIAL) 并列呈裁, 二选一不算挪门柱 | 反事实净Δ>0 ∧ 误杀率≤40% ∧ n≥20 |
| 互斥 | 两案都过线 → 按净Δ/误杀率横向对比, 用户终裁 | 同左 |
| 不过线 | 退役 (影子脚本保留为回放资产), 不动生产 | 同左 |

裁决日预计: D2' forward 攒样自 9/10 起, 1–2 周; E3 同源同期。

---

## B案设计: 2min 延迟复核 (载体 = PlaybookExitFastLoop)

### 否决 A案 (主循环两段式) 的理由 — 实测节奏

主触发循环 (LLMStrategyLoop→run_playbook_auto_execute) 实测 **5–6 分钟/轮**
(9/10 日志: 21:45→21:52→21:58→22:03→22:09→22:14), 历史单轮可达 58min
(scheduler_core.py:1013 注记)。在主循环内"本轮武装/下轮复核"= 实际延迟 N≥5min,
而 N=5 腿机会成本已超预注册门 → **A案落地的是失败变体, 永久否决**。
快循环 (PlaybookExitFastLoop) config 10s/轮 (unified_config.yaml:1894,
scheduler 默认 20s 被 config 覆盖), 是唯一与影子 1min-bar 语义保真的现役宿主。

### 数据流

```
_check_intraday_trigger 穿越命中 (pipeline.py:5045)
  ├─ delay_confirm_enabled=false (现状) → 原路径: triggered=1 → 同轮执行 (零变更)
  └─ delay_confirm_enabled=true →
       ① ARM: triggered 不置 1; 写 metadata_json._delay_arm =
          {armed_at(ET iso), arm_price, direction, deadline=armed_at+150s}
          + 影子行 data/shadow/entry_delay_gate.jsonl (action=armed)
       ② 快循环每 10s 扫 _delay_arm 未决指令:
          - now < armed_at+120s → 跳过 (未到期)
          - 120s ≤ now ≤ deadline(150s): 取实时价复核
              LONG: px ≥ arm_price → CONFIRM; px < arm_price → REVOKE
              SHORT: px ≤ arm_price → CONFIRM; px > arm_price → REVOKE
            CONFIRM → triggered=1, 走既有 auto_execute 执行链 (chase_cap/re-price/
                      admission 全部原样生效, 评估价=确认时价=实际下单价)
            REVOKE → skip_reason='delay_confirm_revoked', 影子行 action=revoked
          - now > deadline (快循环停摆/重启错过窗口) → skip_reason='delay_arm_expired',
            影子行 action=expired (fail-safe: 过期宁可放弃不追单)
       ③ 重启恢复: armed 状态在 DB (metadata_json), 快循环启动即扫;
          deadline 已过 → expired。零内存态。
```

### 交互规则 (裁决通过即生效, 不另行讨论)

1. **chase_cap**: 在确认后的执行链原位评估 (现状即"下单价 vs 禁追线",
   pipeline.py:4250 块) → 延迟 2min 后价格更高若越线, 照拦。零代码改动,
   语义=以实际可成交价判追高, 与 9/4 P0-A enforce 裁决一致。
2. **量能门 (intraday_vol_gate)**: 在 ARM 时刻评估 (穿越轮), 确认时刻不重评 —
   量能是"突破真实性"证据, 属武装判据; 2min 内量能语义无新信息。
3. **RFC-068 接飞刀门**: 维持执行时刻口径 (现状), 确认执行时再判一次 — 双保险不变。
4. **intraday_reentry 通道豁免**: `decision_id LIKE 'intraday_reentry%'` 指令
   **不走延迟门** (直接原路径)。理由: ① 再入场设计语义=出场后快速回接,
   加 2min 直接废掉该通道 (8/7 审计其供给本就脆弱); ② 它自带 spread_bps
   微结构门已承担同类噪声过滤。⚠️ 此为 Hermes 推荐值, **裁决日用户可推翻**
   (推翻=再入场同走延迟门, 一行配置分支)。
5. **PDT/单日帽/购买力帽**: 全部在确认后执行链原位, 零交互。
6. **E3 互斥**: B案落地时 `entry_archetype_routing_enabled` 必须保持 false —
   同一指令不允许"既延迟复核又改挂限价", 影子落盘照旧 (record-only 无冲突)。

### 配置键 (unified_config.yaml playbook 节)

```yaml
entry_delay_confirm_enabled: false     # 总开关, 默认 false 零行为变更
entry_delay_confirm_minutes: 2         # 复核延迟 (=影子 N=2 腿, 冻结)
entry_delay_confirm_ttl_sec: 150       # 复核窗口上限, 过期作废 (fail-safe)
entry_delay_confirm_reentry_exempt: true  # 交互规则4
```

PlaybookConfig 单例缓存穿不透热重载 → **改键必须重启** (与 early_tight_sl 同款纪律)。

### 代码量与触点

| 文件 | 改动 | 量级 |
|---|---|---|
| app/playbook/pipeline.py | `_check_intraday_trigger` 命中后 ARM 分支 (~20行) | 中 |
| app/playbook/pipeline.py | 快循环头部插入 `_scan_delay_arms()` (~35行新函数) | 中 |
| config/unified_config.yaml | 4 键 + 注释 | 小 |
| data/config_freeze_baseline.json | 新版本指纹豁免登记 (KR2 窗口分裂留痕, BE垫先例) | 小 |
| tests/ | 契约钉 ≥6: ARM落库/120s前不复核/CONFIRM-REVOKE双向/过期fail-safe/重启恢复/reentry豁免 | 中 |

合计 ~60 行生产代码。**Guard-First 核查**: 下游 auto_execute 有幂等锁
(executed 标志) + 重复开仓拦截, ARM/CONFIRM 拆两段不产生重复下单路径;
影子 dedup 键 (trade_id, delay_min) 与生产无关。

### 观测与判分 (落地后 5 交易日)

- 影子 jsonl 三分计数: armed / confirmed / revoked / expired
- 判分口径: confirmed 组的 MFE<0.3% 率 vs 落地前基线 (30天 23%);
  revoked 组的反事实损失 (若进场会怎样, T+1 用既有 post_exit_path bar 回放结算)
- 熔断回滚: revoked/(armed) > 70% 或 confirmed 组 SL_INITIAL 率不降 →
  `entry_delay_confirm_enabled: false` + 重启, 影子继续攒

---

## D案设计: PULLBACK_LIMIT 回调限价 (复用 E3 archetype 基建)

### 现状盘点 (已存在, 非新建)

- `app/playbook/entry_routing.py`: 路由/确认位/TTL/chase_cap 全套已实现,
  生产开关 `entry_archetype_routing_enabled: false`, 影子常开
  (337行, 9/2 起; 落盘点 pipeline.py:5945)
- `scripts/cf_pullback_limit_replay.py`: 反事实回放器已存在 (bar 级)
- `app/playbook/playbook_service.py:2178`: 限价阻塞上限=route.ttl_seconds (300s) 已接线

### 落地 = 翻开关 + 裁决参数, 无新引擎

```yaml
entry_archetype_routing_enabled: true   # 唯一生产开关
entry_archetype_ttl_seconds: 300        # 回踩等待窗 (现值, 裁决日可调)
```

语义: 带 `entry_archetype=PULLBACK_LIMIT` 的指令触发时不市价追,
挂限价单在确认位 (DA 计划价/回调位), TTL 300s 内回踩即成交, 超时撤单作废。
FADE_SHORT 同族 (SHORT 版回调)。

### D案特有风险 (裁决日必读)

1. **供给侧收窄**: 真突破不回踩 → 整单错过。影子须给出"回踩成交率"
   (337行触发中实际 5min 内触及确认位的比例) — 若 <50%, D案把系统变成
   "只做回调"策略, 与 DA 突破语义冲突, 须用户明示接受。
2. **限价单在 Futu sim 的成交保真**: 触及≠成交 (排队); sim 撮合乐观,
   实盘滑向更差。落地判分用"触及价 vs 成交价"差监控。
3. **与 B案互斥点**: 同一指令二选一。D案 TTL=300s ≫ B案 150s,
   资金占用周期不同, 单日帽消耗节奏不同。

### D案代码量

~0 行新生产代码 (基建已在) + 开关翻转 + 契约测试补 E3 enforce 分支 (~4 钉)。
**这是 D 案最大优势: 爆炸半径 = 一个布尔值。**

---

## 两案对比表 (裁决日呈裁用)

| 维度 | B案 2min延迟复核 | D案 回调限价 |
|---|---|---|
| 语义 | 追确认 (真突破多付 0.125%) | 等回踩 (假脉冲买更低, 真突破错过) |
| 新代码 | ~60行 + 快循环触点 | ~0行 (翻开关) |
| 影子证据 (首日) | 净回避价值 +$1,555 (ALL) / SL_INITIAL 子集 +$2,120 | 337行触发行在攒, 回放器就绪, 裁决日出反事实净Δ |
| 主要风险 | 误杀率 53% (109/207) — 一半回避是赢家 | 回踩成交率未知 — 可能系统性错过趋势单 |
| 成交笔数影响 | ~50% 触发被放弃 (各影子攒样同步变慢) | 不回踩单全错过 (可能更多) |
| 与用户哲学贴合 | "识别不预测" 中性 | "留空间/不追" 更贴 |
| 回滚 | 单键+重启 | 单键+重启 |

**Hermes 推荐序** (数据未出全前的先验, 裁决日以影子冻结值为准):
若两案都过线 → **D 优先** (零新代码、爆炸半径最小、贴用户"不追"哲学);
若仅 B 过线 → B 按本文件实施; 若仅 D 过线 → D; 都不过 → 双双退役, 墓地留给
入场质量侧 (D1' 跳空门 / D3' 按源监控) 继续攻。

---

## 风险评估 (共同)

- **最坏情况**: 落地后成交笔数腰斩, 各攒样影子 (tiered/KR2/E1') 判分周期拉长一倍;
  sim 数据供给不足拖慢转正门槛 (OOS≥15日) 时间线。缓解: 落地后 5 交易日观察期内
  若日均成交 <3 笔, 触发供给复审 (用户 8/6 裁决"数据量优先"仍有效)。
- **重启丢 ARM**: B案已设计为 DB 持久化 + deadline fail-safe, 昨日 8 次重启的
  现实下零内存态是硬要求。
- **配置指纹分裂**: 两案任一新键都进 config 版本哈希 → KR2 判分开新窗,
  须在 config_freeze_baseline.json 登记豁免 (BE垫 9/9 先例, 强制留痕)。

## 验证计划 (实施日)

```bash
# 1. 契约测试全绿
venv/bin/python -m pytest tests/playbook/ tests/risk/ -q
# 2. 重启 (HotReloader 穿不透 playbook.*)
systemctl restart ftseek-us && sleep 30 && systemctl is-active ftseek-us
# 3. 开盘后 grep 新行为证据
grep -E '_delay_arm|delay_confirm|archetype路由' logs/ftseek_us.log | head
# 4. 影子 jsonl 当日有行且三分计数合理
wc -l data/shadow/entry_delay_gate.jsonl   # B案
# 5. 5交易日后按"观测与判分"节出首份对照报告
```

## 回滚

- B案: `entry_delay_confirm_enabled: false` + 重启 (影子落盘可留, record-only)
- D案: `entry_archetype_routing_enabled: false` + 重启
- 两案回滚后 KR2 指纹再分裂一次 → 回滚同样要在 config_freeze_baseline.json 留痕

---

## 台账

- 影子注册表: entry_delay_confirm (OBSERVING, min_n=20) / extreme_gap_ban_shadow (OBSERVING) 已在册 (9/9 落地批)
- 本 RFC 不含新 cron/新影子 — 纯设计预置, 不触发 CONTROL-LOOP-LEDGER 入账义务
- 裁决日动作: 读本文件 → 对照 latest.json 冻结值 → 二选一 → 按对应节实施

---

## 增补 (2026-09-16, 用户裁「写」): 分流融合方案 — 按 archetype 分支路由, B/D 不叠加

> **状态**: 预注册设计 (纯文档, 零代码零行为变更)。裁决日在本文件 B/D 二选一之外,
> 新增「分流融合」第三选项。本节所有数据为 2026-09-16 实测复核值, 来源逐条标注。

### 动机 (用户 9/16 两问的结论沉淀)

1. **「B 案能否融合进主循环轮次」→ 数据判死, 维持 A 案永久否决**:
   实测主循环 (LLMStrategyLoop→run_playbook_auto_execute) 近两周轮间隔 n=305:
   中位 5.2min, P10–P90 = 5.1–5.4min, max 6.0min (9/9–9/16 日志 grep "auto_execute 开始")。
   同轮 ARM→下轮复核 = 实际延迟 ≈5min。补测 N=3/N=4 腿 (60 天窗 409 笔, 影子同一
   price_at 口径, 离线回放):

   | 腿 | 回避率 | 机会成本 | 回避省 | 延迟多付 | 净 | 预注册门(<0.3%) |
   |---|---|---|---|---|---|---|
   | N=2 (冻结) | 49.6% | 0.1291% | $1,866 | $1,281 | +$586 | ✅ |
   | N=3 | 52.3% | 0.2555% | $3,253 | $2,321 | +$932 | ✅(贴线) |
   | N=4 | 49.6% | 0.3027% | $3,603 | $3,007 | +$596 | ❌ |
   | N=5 | 49.4% | 0.3396% | $3,994 | $3,110 | +$884 | ❌ |

   N≥4 超门, N=3 贴线但系**看数据后挑腿** — forward 冻结值为 N=2, 改用 N=3 = 挪门柱,
   除非预注册新腿重攒 forward (本节不预注册 N=3)。B 案宿主仍为 10s 快循环 (正文 B 案节)。

2. **「降追高能否结合 ADR-035 限价路由」→ 方向成立, 以分流而非叠加融合**:
   正文互斥规则 6 禁止的是*同一指令*既延迟复核又改挂限价; **按指令 archetype 分支
   各走一案不违反互斥**, 且顺手化解披露② TTL 冲突 (各分支各自 TTL: B=150s / D=300s,
   无需重议)。

### 分流规则 (裁决通过后与所选案同批实施)

```
触发命中 → 读 raw_instruction_json:
  entry_archetype ∈ {PULLBACK_LIMIT, FADE_SHORT} ∧ confirmation.price_level 有效
      → D 案分支: 确认位限价路由 (entry_routing.resolve_route 既有逻辑, TTL 300s)
  其余 (BREAKOUT / 无 archetype / 无有效确认位)
      → B 案分支: 2min 延迟复核 (快循环 ARM/CONFIRM/REVOKE, deadline 150s)
  intraday_reentry 通道: 维持正文交互规则 4 豁免 (两分支都不走)
```

- chase_cap 禁追线保持全局兜底, 两分支执行链原位评估 (已验活门: 生产开关 true,
  9/8–9/15 实弹拦截 8 笔 skip_reason=chase_cap_enforced, 非死门)。
- 分流不改变任何一案的判据、影子口径、落地工程细节 — 只决定「哪条指令走哪案」。

### 覆盖现状 (数据基线, 2026-09-16 实测)

- 9/9 以来 388 条指令: 可路由 archetype+有效 price_level 仅 47 条 (**12%**,
  PULLBACK_LIMIT 36 / FADE_SHORT 11), 其余 88% 走 B 案分支。
- 含义: 分流融合 ≈ "B 案为主 + D 案精修 12% 子集"。若希望 D 案覆盖面扩大,
  杠杆在 DA 侧 confirmation.price_level 产出率 (供给侧), 不在执行层。

### 裁决门 (预注册, 冻结)

| 分支 | 判据 (与正文一致, 不新增不放宽) | 数据源 |
|---|---|---|
| B 分支 | forward n≥20 ∧ 回避率≥30% ∧ 机会成本<0.3% (N=2 腿, ALL+SL_INITIAL 双人群并列) | entry_delay_confirm_latest.json |
| D 分支 | 配对 n≥30 ∧ 尺子 L1a 方向判对率≥0.8 (许可配对差结论) ∧ 反事实净Δ>0 ∧ 误杀率≤40% | cf_pullback_limit_replay v2 读数卡 |

- D 分支现状 (v2 窗 09-08→09-14): 配对 n=20 < 30, L1a = 0.765 < 0.8 → **无裁决资格**,
  点估计中位 -$2.30/笔 (n<30 不结论, 宪章口径)。**现在翻 entry_archetype_routing_enabled
  = true 违反 ADR-035 启用门, 本节不豁免。**
- 裁决组合: ①两分支各自过线 → 分流融合落地 (两开关同批翻 true + 重启一次);
  ②仅 B 过线 → 只落 B, D 分支指令维持现状路径 (影子继续攒); ③仅 D 过线 → 只落 D
  (12% 子集限价, 其余现状); ④都不过 → 双双退役, 墓地留给入场质量侧。
- 落地顺序若分批: 先 B 后 D 时, D 分支指令在 B 落地期间走 B 案延迟复核 (临时),
  D 翻开关后切回限价路由 — 切换时点记 config_freeze_baseline.json 留痕 (KR2 纪律)。

### 风险增补 (相对正文)

- 分流引入「同一触发时点两种语义并存」: 单日帽/购买力帽消耗节奏混合 (B 150s + D 300s),
  正文共同风险节的供给复审触发条件 (日均成交<3 笔) 不变, 合并统计。
- D 分支 12% 占比小 → 其独立判分样本积累慢于 B; 裁决日若 D 仅差样本量, 允许
  「B 先落、D 延后单独裁」(组合②路径), 不算挪门柱。

### 台账

- 本增补不含新 cron/新影子/新开关 — 纯设计预置, 不触发 CONTROL-LOOP-LEDGER 入账义务。
- 裁决日动作更新: 读本文件 → 对照两影子 latest 冻结值 → 按「裁决组合」四选一 →
  分流融合中选时按分流规则节实施。

---

## 增补批注 (2026-09-16, 用户裁「要」): §六 分流人群分层 + 误杀率口径冻结 — 裁决读法预注册

> 背景: 对增补节的独立复算发现三个未处理点 (盲区1/2/3)。本批注只加读数与口径定义,
> **不改任何判据数值、不挪门槛、不动两案工程设计**。配套代码同批提交 (record-only)。

### 盲区1 — 分流覆盖面按成交单口径是反的, B 案证据基础须重切

增补"分流 ≈ B为主(88%) + D精修12%子集"是**指令口径**。按**成交单口径**复算
(forward 窗 9/10–9/15, B 影子 N=2 腿 status=ok, 经 decision_id 映射到指令 archetype):

- forward n=25 中 **18 笔 (72%) 属 D 分支候选** (PULLBACK_LIMIT/FADE_SHORT+有效确认位,
  ARM/HPE/CRM/META/INTC/SKHY 等全在内); B 自身人群 (非可路由) 仅 **7 笔**
- B 自身人群 forward 读数: 回避 2 笔 (回避价值 −$20, 负), 机会成本均值 **0.39% > 0.3% 门**
- 含义: B 案现在"过门"(forward n=25, 回避 52%, opp 0.203%) 的读数七成由分流后将归 D 的
  单子背书。**分流落地后 B 只管非可路由人群, 而该人群的 forward 证据目前近乎空白且方向不利。**

**预注册读法 (裁决日生效)**:
1. B 影子 latest.json 新增 `by_branch` (B_only/D_candidate/unmapped) 与 `forward`
   (ALL/B_only/D_candidate) 分层块 + `forward_ALL_pass`/`forward_B_only_pass` 毕业读数;
   行级 `routable_candidate` 标签 (jsonl 新行写入, 历史行汇总期映射回填)。
2. 裁决日 B 分支若与分流融合捆绑呈裁, **主判读 = forward_B_only_pass**;
   混合人群 ALL_pass 只作历史参考, 不得用它给分流后的 B 背书。
3. B_only 单独过门与否不改变 B 案"不过线退役"的原文规则 — 只是把"过线"的人群
   对齐到分流后的真实管辖范围。**原 ALL 口径判据 (DEB §5.2) 若单独落地 B (无分流),
   仍然有效** — 分层读数服务的是"分流融合"这一第三选项。
4. forward 起点 = 2026-09-10 (DEB §5.2 预注册攒样日), 常量 FORWARD_SINCE 锁死。

### 盲区2 — D 案"误杀率≤40%"分母冻结为 expired_unfilled

增补裁决门 D 分支判据含"误杀率≤40%"但未定义分母。两种自然口径横跨门槛
(9/8–9/15 窗实测): expired_unfilled 口径 = 11/24 = **46% (FAIL)**; 全配对口径 = 27.5% (PASS)。

**冻结**: 误杀率 = 限价臂 TTL 到期未成交 (expired_unfilled) 且实盘臂净利>0 的行数 /
expired_unfilled 行数。语义 = "限价没等到回踩而错过的真钱"。replay 读数卡新增
`d_branch_miskill` 块 (denominator 字段内嵌冻结声明)。全配对口径稀释成交腿, 弃用。

### 盲区3 — expired 腿放弃经济须与配对差同屏

"限价不成交=躲烂单"叙事在窗口聚合上不总成立: 9/8–9/15 窗 expired 24 笔合计
**放弃实盘净利 +$361** (误杀赢家 +$638 / 躲掉亏损 −$276) — 躲烂单只在 HPE/CRM 等
子集成立。读数卡新增"不成交腿放弃实盘净利"一行, forgone>0 时显式打折标注。
裁决日 D 分支呈裁必须三读数同屏: 配对差 + C3 分层 + 放弃经济, 缺一即读数不完整。

### 现状快照 (2026-09-16 实算, 只作时点留痕, 裁决日以当日 latest 冻结值为准)

| 读数 | 值 | 门 | 状态 |
|---|---|---|---|
| B forward ALL | n=25, 回避 52%, opp 0.203% | n≥20∧≥30%∧<0.3% | ✅ (混合人群) |
| B forward B_only | n=7, 回避价值 −$20, opp 0.39% | 同上 | ❌ 样本不足+方向不利 |
| D 配对样本 | n=24 (L1a=0.81 已过尺) | n≥30 | ❌ 无裁决资格 |
| D 误杀 (expired 口径) | 46% | ≤40% | ❌ (当前窗) |
| D 放弃经济 | +$361 forgone | 呈报项 | ⚠️ 躲烂单叙事打折 |

⇒ 当前证据不支持任何接线; 两影子继续攒样。B_only 人群按近两周节奏 (~1.8 笔/日)
到 n≥20 需 ~3-4 周 — 裁决日 B 分支若仅样本不足, 走增补"组合②: B 先落 D 延后"
之前须先看 forward_B_only 读数方向, 不得只看 ALL。

**台账**: 本批注不含新 cron/新影子/新开关。配套代码: entry_delay_confirm_shadow.py
(分层块+标签+披露), cf_pullback_limit_replay.py (d_branch_miskill+读数卡两行)。

---

## 增补批注二 (2026-09-16, 用户裁「按照建议执行」): 宪章第9律读数 + n门槛统一 + 落地工程增补 — 纯读数与设计要求, 两案判据/门槛零改动

> 本批注为裁决前最后一批读数预注册 (读数可后加, 判据不可挪)。配套代码已落地
> (record-only), 落地工程增补仅写入本文件供实施日使用 — 今日零生产路径改动。

### ① 日聚类 t + MDE(80%) (宪章第9律, B/D 两案裁决日必读)

- **D 案** (`cf_pullback_limit_replay.py`): summary 新增 `paired_diff_day_clustered` +
  `day_pair_counts`; 口径与 exit_scorecard 完全一致 (日等权均值 → 单样本 t, |t|≥2
  可分辨; n_days<3/零方差 → None)。**本窗实算**: pooled 中位 −$2.30 (p=0.968) vs
  日聚类 day_mean +$11.18 (t=1.28, n_days=6, MDE(80%)=±$24.56, 9/10 单日占 42%) —
  连符号都不稳定, pooled 符号检验不得单独作为裁决依据。
- **B 案** (`entry_delay_confirm_shadow.py`): 每个汇总块新增 `net_value_usd`
  (延迟门净价值/笔 = 回避省的钱 − 延迟付的机会成本) + `day_clustered` (同一口径)。
  **关键实算**: forward_B_only day_mean **−$17.56/笔 (t=−2.10, n_days=4, MDE=±$23.41)**。
  ⚠️ **判读修正 (2026-09-16, Hermes 独立复核 + 用户裁「按建议执行」)**: 此读数**不得**写成
  "与0可分辨的负值/方向不利实锤"。两条独立理由: ① n_days=4 → df=3, 双侧95% t 临界值=**3.182**
  (非粗启发 |t|≥2), |t|=2.10 **未越过真实临界** ⇒ 统计上仍**与0不可分辨**; ② forward 起点
  含 9/9 (多一个 +$74/笔 赢家日) → t 由 −2.10 **翻为 +0.04** (符号反转), 对单日极敏感。
  诚实表述: forward_B_only **方向为负但与0边界不可分辨** (t 在 −2.1~+0.04 随口径/起点摆动,
  n_days=4 不足以下实锤); 增补批注一"方向不利"维持**点估计**级别, **未**升级为日聚类可分辨证据。
  (Codex 算术 t=−2.10 / day_mean −$17.56 经独立重算吻合 — 仅"可分辨/实锤"的**判读**被修正,
  数值未改。Hermes 上轮口误 t=−1.94 系用 per-day-total 非 canonical 口径, 一并作废, 以 per-day-mean −2.10 为准。)
  60 天 ALL 口径 day_mean +$1.50 (t=0.59, MDE=±$7.11) ⇒ 与 0 不可分辨 — 此前"+$1,555 净回避价值"
  头条在日聚类下不成立 (此条 Hermes 复核同样吻合, 无异议)。
  ⚠️ **工具/文档口径差 (裁决日必读, 防混淆)**: 影子脚本的 `day_clustered.distinguishable`
  字段沿用全仓房规 `bool(abs(t)>=2.0)` (与 exit_scorecard.py / cf_pullback 同一粗启发), 故对
  forward_B_only 仍会输出 `distinguishable=true`。**本批注的判读以 df 校正临界值为准** (n_days=4
  → df=3 → 双侧95%临界 3.182 > 2.10 ⇒ 不可分辨)。`|t|≥2` 房规在 n_days 很小 (df≤4) 时偏松,
  会把边界值误判成可分辨; 裁决日引用 `distinguishable` 字段前必须按 df 复核, **不得直接采信该布尔**。
  (不改房规: 它跨 exit_scorecard/两影子共用, 单方面挪 = 跨组件挪门柱; 仅在本文档标注其小样本局限。)

### ② n 门槛统一声明 (消歧, 防裁决日争议)

三处文字并存: 主表 (原文) n≥20 / 增补表 n≥30 / ADR-035 与 replay 读数卡 n≥40。
**统一**: 裁决资格 = **n≥40 配对 (最新预注册, 最严优先)**; n≥30 = 宪章「n<30 不结论」
中间态 (可说方向, 不得裁开关); 主表 n≥20 已被覆盖, 仅作历史留痕。

### ③ B 案落地工程增补 (实施日生效, 本批不写代码)

**expired 归因遥测**: ARM 过期落行时必须带 `expiry_cause`
(`price_unconfirmed` = 到期复核价不满足 / `loop_stall_restart` = 快循环停摆或重启
错过窗口) + expiry 时刻快循环心跳龄。无此遥测, 供给不足 (重启/停摆饿死样本) 会被
误读为"市场不确认" — 9/16 服务重启 1 次, 150s TTL 窗口内重启即静默饿死, 且 B 案
判据无法区分这两种病因。契约钉: expired 行必带两字段。

### ④ D 案落地工程增补 (实施日生效, 本批不写代码)

**partial fill 费用地板**: ADR-035 状态机已支持部分成交记账, 但极小成交腿
(如 1 股) 会撞佣金下限 — 落地时契约钉「部分成交股数 < 最小可记股数 → 撤余单且
整单不记」(防费用地板吞噬小腿); 佣金下限值从券商费率表读取, 不硬编码。

### ⑤ 现状快照 (2026-09-16 实算, 裁决日以当日 latest 为准)

| 读数 | 值 | 判读 |
|---|---|---|
| B forward_B_only 日聚类 | day_mean −$17.56/笔, t=−2.10, n_days=4 (df=3 真实临界3.182) | **方向为负但与0不可分辨** (未越 df=3 临界; 含9/9则翻+0.04) — 非"实锤" |
| B 60d ALL 日聚类 | day_mean +$1.50/笔, t=0.59, MDE±$7.11 | 与0不可分辨 (头条+$1555不成立) |
| D 配对日聚类 | day_mean +$11.18/笔, t=1.28, MDE±$24.56 | 与0不可分辨; pooled中位−$2.30符号相反 |
| C3 子桶 | <0.25ATR 72.7% / 0.25–0.5 25% / ≥0.5 0% | 距离梯度, 非二值归因 |
| ATR 口径 | real:15 / proxy:10 / unknown:10 | real 不主导 ⇒ 桶界降级方向性 |

**台账**: 本批含 record-only 代码 (两脚本读数 + 测试), 无新 cron/新影子/新生产开关;
③④ 为实施日设计要求 (写入本文件即预注册, 落地时按此验收)。

## 增补批注三 (2026-09-16, DEB-20260916 独立复核回灌): 源头审计对 B/D 裁决的三条约束 — 判据零改动

1. **几何改写结局不可分辨**: re-price 后落库 SL 跨 DA 失效线 vs 未跨, 全历史日配对差
   **−$16.76/笔 (t=−0.35, n_days=7, MDE(80%)=±$133.61)**。
   该读数只约束"不得为几何另立守卫/另加判据"，不改 B/D 两案主判据。
2. **TP 不是可期待收益项**: 全史 TAKE_PROFIT 触发率 0.41% (2/488)。D 案若过 C1-C4，
   收益解释必须落在入场时机/成交率/mfe=0 改善, 不得把静态 TP 或 RR 几何改善计入预期。
3. **不新增 prompt 字段**: ARM 的 `confirmation.price_level=248.5` 已在
   `raw_instruction_json` 透传；B/D 实施若需消费确认位，只做路由/状态机接线。

---

## 复核批注 (2026-09-16, Hermes 独立复核, 用户裁「复查是否可代落地」): 双读数同屏补充 + 传感器门槛对齐

> 本节为代落地批次的复核方增量 (对批注二/三); 只补读数与一致性, 判据/门槛零改动。
> 数字均为复核方独立重算 (不采信批次自报, 宪章律7)。

### ① 双读数同屏: 批注二⑤快照表缺 SL_INITIAL 日聚类 (主判读人群的正向读数)

同一份 entry_delay_confirm_latest.json (08:35) 内, SL_INITIAL 60d day_clustered =
**t=+4.27, day_mean +$106.93/笔, n_days=15, distinguishable=true**
(SL_INITIAL = DEB §5.2 预注册主判读人群, 本影子动机人群)。复核方交叉验证:
SL_INITIAL 23 笔中 **19 笔属 B_only** (回避 13 笔, 回避省 $2,146) — 即该正向读数
主要落在分流后 B 分支的管辖人群上, 与 forward_B_only 负值读数 (t=−2.1, n=7/n_days=4)
**方向相反、同等脆弱** (前者 n=23<30 宪章地板; 后者样本量正是批注二自己批评的
"大日绑架"形态)。

**裁决日规则 (预注册, 读数可后加判据不可挪)**: 引用 forward_B_only 负值时,
SL_INITIAL 日聚类必须同屏; 任一读数都不得单独定性"方向不利实锤"/"方向有利实锤",
两人群各自到 n 地板后再谈裁决。

### ② 传感器门槛对齐: push 脚本 D_TARGET_N 30→40

批注二②已统一裁决资格 = 配对 n≥40 (最严优先, 溯源 DEB-20260907 §4.9 /
ADR035_VERDICT_MIN_N_PAIRED=40 系 ef5a1cc5d 已入库常量), 但已提交的
ops/hermes/entry_delay_shadow_push.py (7bbcca7c5) 仍 D_TARGET_N=30 —
n 达 30–39 时会提前推"裁决材料齐备, 提请裁决"。本批同批对齐为 40 (一行+注释溯源),
生产镜像 /root/.hermes/scripts/ 已同步 (sha256 一致), test_ops_scripts_sync 绿。
