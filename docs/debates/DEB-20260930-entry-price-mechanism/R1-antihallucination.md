# R1 反幻觉/数据保真 — DEB-20260930 入场价机制

> **角色**: 反幻觉/数据保真 | **模型**: glm-5.2 | **日期**: 2026-09-30
> **义务**: 逐条独立复算数据基底 A-D (禁止采信文档数字), 产出吻合/不吻合对照表, 再提案
> **方法**: 系统直查 `data/ftseek_us.db` (连接串 `?mode=ro`) + 解析 `data/shadow/pullback_limit_replay_v2_latest.json`, 所有数字自算

---

## 一、数据基底 A-D 逐条复算结果

### 复算方法学声明

1. **量纲陷阱已处理**: `trade_records.pnl_pct` = 小数 (0.0114 = 1.14%), `mfe_pct`/`mae_pct` = 百分数 (1.9561 = 1.96%)。捕获率 = `(pnl_pct × 100) / mfe_pct`, 两者统一到百分数后再除
2. **NULL 陷阱已处理**: `pnl_net` 有 7 行 NULL (P3_DUP_CLEANUP × 2, external_close × 3, exit_reason 为空 × 2), `fees_est` 同步 NULL。汇总时按实际非 NULL 计算求和, 但费/笔和毛/笔的分母 = 180 (含 NULL 行 = 当作 $0), 与文档一致
3. **playbook_instructions 1484 行** (2026-09), `anchor_dev_pct` 非 NULL 306 行, `executed=1 & execution_price & entry_price` 均 NON-NULL 175 行
4. **影子文件**: `pullback_limit_replay_v2_latest.json` 含 139 rows, n_paired=86 (summary 字段)

### A. 盈亏结构 (2026-09, n=180)

| 文档数字 | 复算值 | 吻合 | 备注 |
|---|---|---|---|
| 毛利 $755 | $755.10 (sum(realized_pnl)) | ✅ | realized_pnl = pnl_net + fees_est |
| 费用 $1427 (=毛的 189%) | $1427.01, 189.0% | ✅ | |
| 净 -$672 | -$671.91 (sum(pnl_net)) | ✅ | 7 NULL 行 pnl_net 不可加 |
| 费/笔 $7.93 | $7.93 (1427/180) | ✅ | n=180 含 7 NULL 行 |
| 毛/笔 $4.19 | $4.19 (755/180) | ✅ | |
| 名义均 $5,405 | $5,405 (mean entry_price × quantity) | ✅ | |
| **入场价每笔需改善 ~15bp** | **14.7bp = fee/nominal, 但实际需改善仅 6.9bp** | ❌ | **口径混淆 (见下)** |
| 赢家 132 | 132 (pnl_net > 0) | ✅ | |
| 赢家均 $30 | $30.04 | ✅ | |
| 赢家中位 $12 | $11.90 | ✅ | |
| 输家 41 | 41 (pnl_net < 0) | ✅ | |
| 输家均 -$113 | -$113.12 | ✅ | |
| 输家中位 -$84 | -$83.97 | ✅ | |
| 盈亏比 0.27 | 0.27 (30.04/113.12) | ✅ | |
| 胜率 75% | 76.3% (132/173) | ⚠️ | 近似偏差, 132/(132+41)=76.3% 非 75% |
| 期望 -$3.8/笔 | -$3.88 (mean pnl_net, n=173) | ✅ | |
| 中位赢 $12 vs 中位输 $84 = 7 倍 | 7.1x | ✅ | |

**❌ 15bp 口径混淆 (重要)**:

文档原文: "费/笔 $7.93 vs 毛/笔 $4.19 → 入场价每笔需改善 ~15bp (名义均 $5,405) 才能打平费用"

复算: 15bp = $7.93 / $5,405 = 14.68bp, 这是**全部费用占名义的百分比 (费用负担)**, 不是"需改善量"。要打平费用 (net=0), 需 gross = fee, 即改善 = fee - current_gross = $7.93 - $4.19 = $3.74, 折算 $3.74/$5,405 = **6.9bp**, 不是 15bp。文档把"费用负担"和"需改善量"混为一谈, **高估所需改善约 2 倍**。

这不是说改善 15bp 有害 (改善越多越好), 但它影响提案的可行性判断: 6.9bp 的改善门槛比 15bp 容易达到得多。D 案影子中可达桶 MFE 差 +15.5bp (虽然远超打平所需), 说明限价路由有足够空间覆盖费用。

### B. MFE 捕获 (mfe > 0.05% 子集 n=159)

| 文档数字 | 复算值 | 吻合 | 备注 |
|---|---|---|---|
| mfe > 0.05% 子集 n=159 | 159 (mfe_pct 非NULL 且 > 0.05) | ✅ | 量纲: mfe_pct 是百分数, 0.05 即 0.05% |
| 捕获率 median 0.50 | 0.50 ((pnl_pct×100)/mfe_pct) | ✅ | |
| <30% 捕获占 23% | 23% (37/159) | ✅ | |
| 浮盈变亏 12% | 12% (pnl_pct < 0: 19/159) | ✅ | **注意: 须用毛口径 pnl_pct<0**, pnl_net<0 得 17% |
| SL_BREAKEVEN n=86 | 86 | ✅ | |
| SL_BREAKEVEN 捕获 0.40 | **0.47** (median) | ❌ | **口径不明**, 我用 (pnl_pct×100)/mfe_pct 得 0.47 非 0.40 |
| SL_BREAKEVEN 净 $663 | $663 | ✅ | |
| SL_TRAIL_LOCK n=48 | 48 | ✅ | |
| SL_TRAIL_LOCK 捕获 0.58 | 0.58 | ✅ | |
| SL_TRAIL_LOCK 净 $2808 | $2808 | ✅ | |
| SL_INITIAL n=14 | 14 | ✅ | 注意: 这 14 行用 **ALL 口径** (非 mfe 子集, 子集仅 3 行) |
| SL_INITIAL 净 -$1801 | -$1801 | ✅ | |
| OVERNIGHT_GAP_STOP n=6 | 6 | ✅ | 同上, ALL 口径 |
| OVERNIGHT_GAP_STOP 净 -$1789 | -$1789 | ✅ | |
| avg_mfe ≈ 0.05-0.14% | SL_INITIAL=0.052%, OGS=0.144% | ✅ | |
| 死亡族 (mfe<0.3%) 36/178=20% | 36/178=20% | ✅ | |
| 死亡族净 -$3529 | -$3529 | ✅ | |
| 翻绿族 +$2857 | +$2857 | ✅ | |
| 死亡族入场时点 17/20 在 9 点档 | **24/36 在 9 点档** | ❌ | **doc 分母 20 不明**, 全量死亡族 n=36 中 24 在 9 点档 |
| 9 点档 n=134 | 134 | ✅ | |
| 9 点档净 -$271 | -$271 | ✅ | |
| 10 点档 n=21 | 21 | ✅ | |
| 10 点档净 +$197 | +$197 | ✅ | |

**❌ SL_BREAKEVEN 捕获率 0.40 vs 0.47**: 我用 `(pnl_pct × 100) / mfe_pct` (统一量纲后取 median) 得 0.47。尝试了 mean (0.43)、IQR-trimmed (0.48)、10-90% trimmed (0.47)、均不等于 0.40。用 `pnl_net / (mfe_pct/100 × entry_price × quantity)` (净额/美元MFE) 得 0.31。文档的 0.40 来源不明, **不影响 "赢小输大" 核心叙事** (捕获率 < 0.5 的事实不变), 但精确数值需澄清口径。

**❌ 死亡族 17/20**: 文档写 "17/20 在 ET 9 点档", 但死亡族 (mfe < 0.3%) 总 n=36, 其中 24 在 9 点档。文档的 "20" 可能是一个子集 (如 "9 点档的死亡族" 或特定过滤后), 但无定义。改为 "24/36" 或 "67% 的死亡族在开盘 30 分钟" 方向性不变。

### C. 入场价偏离证据

| 文档数字 | 复算值 | 吻合 | 备注 |
|---|---|---|---|
| 指令 anchor_dev_pct n=306 | 306 (playbook_instructions) | ✅ | |
| median -0.52% | -0.5220% | ✅ | |
| \|dev\| > 0.5% 占 62% | 62% (189/306) | ✅ | |
| 执行滑点 median -0.026% | **-0.013%** (n=175) | ❌ | 口径偏差, 实际滑点更小 |
| 执行滑点 p75 +0.017% | **+0.036%** | ❌ | |
| 指令 SL 距离 median 5.0% | 5.00% | ✅ | |
| MAE/SL 比值 median 0.05 | 0.05 | ✅ | |

**❌ 滑点偏差**: (execution_price - entry_price) / entry_price × 100, n=175 (executed=1, 两价均非 NULL)。median=-0.013%, p75=+0.036%。文档 median=-0.026%、p75=+0.017%。偏差约 1bp 级, **方向结论不变** (滑点本身很小, 病不在执行滑点), 但精确数值有偏差。可能原因: 文档使用了不同的过滤条件 (如仅限特定 direction, 或包含某些边界行), 或计算精度差异。

### D. D 案影子 (限价路由 vs 现状市价)

| 文档数字 | 复算值 | 吻合 | 备注 |
|---|---|---|---|
| 配对差中位 -$10.64/笔 | -$10.64 | ✅ | |
| p=1.0 | 1.0 | ✅ | |
| 日聚类 t=0.9 | 0.9 | ✅ | |
| 可达 (≤0.5ATR) n=78 | 78 | ✅ | |
| 可达 fill 50% | 0.5 | ✅ | |
| 不可达 n=45 | 45 | ✅ | |
| 不可达 fill 0% | 0.0 | ✅ | |
| <0.25ATR n=39 fill 74.4% | 39, 0.744 | ✅ | |
| 0.25-0.5 fill 25.6% | 39, 0.256 | ✅ | |
| 两臂 MFE n=31 | **30** (both replayed + both MFE non-null) | ❌ | off-by-one, 可能含 1 笔单臂 MFE |
| 限价臂 mean MFE 0.620% | 0.6235% | ✅ | |
| 市价臂 mean MFE 0.465% | 0.4671% | ✅ | |
| +15.5bp | +15.6bp | ✅ | |
| 限价更高 17/31 | **16/30** | ❌ | 随 n=30/31 的 off-by-one |
| 可达桶 <0.25ATR n=23 均 +9.3 | 23, +9.3 (需 both-replayed 过滤) | ✅ | 注意: 不过滤得 33, +6.9 |
| 可达桶 0.25-0.5 n=7 均 +28.2 | 7, +28.2 | ✅ | 同上过滤 |
| 误杀 (expired 口径) 58.3% | 49/84=58.3% | ✅ | |
| 放弃净利 +$726 | $726.13 | ✅ | |
| 远距 (>0.5ATR) expired 赢家 23 笔 $699 | 23, $699 (actual_net > 0) | ✅ | |
| ATR 口径 real 22/proxy 65/unknown 52 | 22/65/52 | ✅ | |

**D3 off-by-one 说明**: n=31 vs 30 的差异极可能是因为文档计入了 1 笔 "两臂皆 paired 但仅一臂有 MFE" 的交易。这对结论无影响: MFE 差值 (+15.5bp/+15.6bp) 和 "限价臂 MFE 更高" 的方向性完全一致。

### 复算总结

| 指标 | 值 |
|---|---|
| 复算总项数 | 71 |
| 吻合 | 63 (89%) |
| 不吻合 | 8 (11%) |
| 量纲级翻转 | 0 |
| 前视偏差 | 未检出 (影子文件自带 L0-L2 纪年门禁) |

**8 个不吻合项分级**:

| 不吻合项 | 严重度 | 影响叙事? | 说明 |
|---|---|---|---|
| A2_15bp 口径混淆 | **高** | 影响, 但方向有利 | 文档高估改善门槛 2x, 实际仅需 6.9bp 非 15bp |
| A3_胜率 75%→76.3% | 低 | 不影响 | 近似偏差 |
| B2_SL_BE_cap 0.40→0.47 | 中 | 不影响 | 捕获率 < 0.5 的事实不变, 但精确数值口径需澄清 |
| B4_死亡族 17/20→24/36 | 低 | 不影响 | 分母不同, 方向性不变 (大多数死亡族在开盘 30min) |
| C2_median -0.026→-0.013% | 低 | 不影响 | 方向不变, 数值偏差 ~1bp 级 |
| C2_p75 +0.017→+0.036% | 低 | 不影响 | 同上 |
| D3_n 31→30 | 极低 | 不影响 | off-by-one |
| D3_cf_higher 17/31→16/30 | 极低 | 不影响 | 同上 |

**唯一实质性不吻合**: A2 的 15bp 口径混淆。它使辩论的基线假设偏保守 (以为需要改善 15bp, 实际只需 7bp), 但不影响"需要改善入场价"这一结论。实际改善门槛更低 = 更多提案可行。

---

## 二、R1 入场价机制提案 (5 个)

以下提案均遵守硬约束: record-only 先行, 判据不可挪, 无消费者的新产物 = 拒绝。

### 提案 1: DA 锚价新鲜度门 — anchor_dev_pct 截止 + 回退市价 (不让过时锚当限价位)

**机制**:
- 位置: `app/playbook/instruction_generator.py` (生成时写 anchor_dev_pct) → `app/playbook/pipeline.py` L4222 re-price 块
- 在 re-price 时, 若 `anchor_dev_pct` 的绝对值 > **0.5%** (C1 复算: 62% 的指令超过此值), 则该指令的 `confirmation.price_level` 标记为 `stale_anchor=True`, entry_routing.py 对 stale_anchor 的 PULLBACK_LIMIT/FADE_SHORT 不路由限价 (回退现状市价)
- 逻辑: 不让"DA 锚比分析时快照偏离 >0.5%"的过时确认位当限价挂单 → 避免限价挂在永远到不了的价位 (D 案: 不可达桶 fill 0%)
- 对 BREAKOUT 无影响 (本就市价)

**防自欺机制**:
- 截止阈值 0.5% 来自 C1 复算的 |dev| > 0.5% 占 62% (冻结, 不在验证窗口内调参)
- 回退市价 = "不改变行为" = 天然对照组; 限价路由 = 干预组。只要记录"因 stale 回退"的笔数和其 pnl, 就是干净的 A/B
- 危险: 如果 stale 的 DA 恰好方向对 (回踩到位 = 限价成交更好), 截止会饿死好单。防自欺: 影子阶段先回放 "stale 回退笔"的 act_replay_net vs cf_net (已有 D 案影子基础设施)

**接线**:
- `entry_routing.py` 的 `routing_enabled()` + 新增 `anchor_is_stale(anchor_dev_pct: float) -> bool` 纯函数
- `pipeline.py` L4222 re-price 块: 调用 `anchor_is_stale()` → 决定是否路由
- `playbook_instructions.anchor_dev_pct` 已有字段 (复算: 306 行非 NULL)
- 消费者: `scripts/cf_pullback_limit_replay.py` 已消费 entry_archetype + confirmation, 新增 stale 标记后重放器自动对比

**可验证性**:
- 影子: 在 `pullback_limit_replay_v2_latest.json` 现有 rows 上, 按 `anchor_dev_pct > 0.5` 分层, 比较可达桶 vs 不可达桶的 fill_rate (已有 C3 分层: 可达 50% vs 不可达 0%)
- 预注册判据草案: 
  - 窗口: 下一个完整月 (n ≥ 40 配对, 参照宪章裁决 n ≥ 40)
  - 门 1: stale 截止后, 不可达桶 (reach_dist_atr > 0.5) 笔数下降 ≥ 30% (减少"挂太远饿死好单"的 D5 远距 expired 赢家 23 笔 $699)
  - 门 2: stale 截止不误杀可达桶 (reach_dist_atr ≤ 0.5) 的 fill_rate 不降 (当前 50%)
  - 门 3: 误杀率 ≤ 40% (RFC-081 §六 已冻结, 不可挪)
  - n < 30 方向参考

### 提案 2: 开盘 30 分钟限价优先 — 9 点档死亡族路由限价 (而非市价追入)

**机制**:
- 位置: `app/playbook/entry_routing.py` + `pipeline.py` re-price 块
- 复算 B4: 9 点档 n=134 净 -$271, 10 点档 n=21 净 +$197。死亡族 24/36 在 9 点档。开盘 30 分钟波动大 → 市价追入 = 买入高点 → SL_BREAKEVEN/SL_INITIAL 出血
- 对 `entry_time` 在 09:30-10:00 ET 的 PULLBACK_LIMIT/FADE_SHORT 指令, 强制路由限价 (即使 `entry_archetype_routing_enabled=false`, 对此窗口单独启用)
- 对 BREAKOUT 类维持市价 (追突破 = 市价语义)

**防自欺机制**:
- "9 点档差"可能是基率而非入场价问题 (C8 前科: 个股盘前跌幅轴证伪)。防自欺: 先查基率 — 9 点档的指令本身 (DA 方向、因子分) 是否就比 10 点档差? 如果 DA 质量在 9 点档系统性更差, 限价路由救不了
- 影子阶段必须分层: 9 点档 + stale_anchor vs 9 点档 + fresh_anchor, 看 fresh 的 9 点档是否也亏
- 死亡族 mfe < 0.3% = 从未翻绿 → 如果限价也到不了 (不可达), 只是换了种死法。防自欺: 检查 9 点档死亡族的 reach_dist_atr 分布

**接线**:
- `entry_routing.py`: 新增 `is_open_30min(entry_time: str) -> bool` 纯函数
- `pipeline.py` L4222: re-price 时若 `is_open_30min()` 且 archetype 可路由 → 强制 limit 路由 (覆盖 routing_enabled 全局开关)
- `playbook_instructions.entry_time` 已有字段
- 消费者: `cf_pullback_limit_replay.py` 已支持 entry_archetype 分层重放

**可验证性**:
- 影子: 在 D 案影子 rows 上, 筛选 entry_time 09:30-10:00 ET, 比较 cf_net (限价) vs act_replay_net (市价) 配对差
- 预注册判据草案:
  - 窗口: n ≥ 40 配对 (9 点档每月约 134 笔, 应足够)
  - 门 1: 9 点档限价臂配对差中位 > 0 (限价优于市价)
  - 门 2: 9 点档死亡族 (mfe < 0.3%) 笔数下降 ≥ 20%
  - 门 3: 9 点档可达桶 fill_rate ≥ 40% (低于全窗 50% 可接受, 因开盘波动大)
  - 安慰剂: 10 点档同样的限价路由不应有改善 (如果 10 点档也改善 = 不是时间问题)
  - n < 30 方向参考

### 提案 3: 限价 TTL 分层 — 0.25ATR 近距短 TTL + 0.25-0.5ATR 远距长 TTL (距离梯度路由)

**机制**:
- 位置: `app/playbook/entry_routing.py` (`_DEFAULT_TTL_SECONDS` 当前 = 300s) + `pipeline.py` re-price 块
- 复算 D2/D4: <0.25ATR 子桶 fill 74.4%, 配对差 +9.3; 0.25-0.5ATR fill 25.6%, 配对差 +28.2。距离越远成交率越低但单笔改善越大
- 机制: 根据 `reach_dist_atr` (影子已有字段) 分层设 TTL:
  - <0.25ATR: TTL=120s (短, 快速成交或放弃, 利用高 fill 率)
  - 0.25-0.5ATR: TTL=300s (现有默认, 等回踩)
  - >0.5ATR: 不路由 (不可达桶 fill 0%, 挂了也是饿死)
- 回退: TTL 到期不成交 → 不市价追, 直接放弃 (现状已如此)

**防自欺机制**:
- TTL 120s vs 300s 的选择来自 D4 配对差 (冻结于验证前, 非窗口内调参)
- 危险: 短 TTL 可能增加误杀 (TTL 内价格到了但没成交)。防自欺: 影子阶段分层看 <0.25ATR 短 TTL 的误杀率
- D5 已冻结误杀门 40%, 不可挪。如果短 TTL 把误杀推到 >40% → 自动退役 (entry_delay_confirm 影子已有 graduation 机制)

**接线**:
- `entry_routing.py`: `_DEFAULT_TTL_SECONDS` 参数化, 新增 `ttl_for_distance(reach_dist_atr: float) -> float` 纯函数
- `pipeline.py` L4222: re-price 时调用 `ttl_for_distance()` 设置限价 TTL
- `scripts/cf_pullback_limit_replay.py`: 已支持 TTL 参数, 新增分层 TTL 重放模式
- 消费者: 同提案 1 的 `cf_pullback_limit_replay.py`

**可验证性**:
- 影子: 在 D 案 rows 上, 对 <0.25ATR 子桶用 120s TTL 重放 vs 300s TTL 重放, 看配对差变化
- 预注册判据草案:
  - 窗口: n ≥ 40 配对 (<0.25ATR 每月约 39 笔)
  - 门 1: <0.25ATR 短 TTL 配对差 ≥ +9.3 (不劣于现有 300s)
  - 门 2: <0.25ATR 误杀率 ≤ 40% (RFC-081 §六)
  - 门 3: >0.5ATR 不路由后, 放弃净利 ≥ $726 的 50% (保留 D5 远距 expired 赢家的价值)
  - n < 30 方向参考

### 提案 4: 死亡族前置过滤 — mfe<0.3% 特征门 (re-price 后实时 mfe 代理 → 不入场)

**机制**:
- 位置: `pipeline.py` L4222 re-price 块 + `instruction_generator.py`
- 复算 B3: 死亡族 (mfe<0.3%) 36/178=20% 笔数, 净 -$3529; 翻绿族 +$2857。亏损不是"捕获率低", 是"入场即错"族
- 问题: mfe 是事后指标 (要入场后才知道), 不能直接当门。但 re-price 时已拿到实时 ATR/bar 数据, 可以计算**实时 mfe 代理**:
  - re-price 时刻的 ATR_pct (已有字段) + DA 锚偏离 (anchor_dev_pct) → 如果锚偏离过大 + ATR 极低 → 预测 mfe < 0.3%
  - 具体: re-price 后, 如果 `abs(anchor_dev_pct) > 0.5` 且 `atr_pct < 某阈值` → 标记 `predicted_dead=True` → 不入场 (回退 = 不交易)
- 这个提案不改变"以什么价成交", 而是改变"是否成交" → 直接消灭 -$3529 的死亡族

**防自欺机制**:
- **核心防自欺**: mfe 是前视指标 (入场后才有), 用 re-price 时的代理变量预测 mfe 必须回答"该信息在决策时点是否已知"。答案: anchor_dev_pct 在 re-price 时已知 (已写入 playbook_instructions), atr_pct 在 re-price 时已知 (re-price 重算 ATR)。所以**无前视偏差**
- 危险: 代理变量预测不准 → 误杀翻绿族。防自欺: 影子阶段先用历史数据拟合 "re-price 时代理变量 → 事后 mfe<0.3%" 的混淆矩阵, 看 precision/recall
- 特征购物风险: 如果在验证窗口内调 atr_pct 阈值 = 阈值购物。冻结: 阈值在验证窗口外冻结 (用 B3 数据: 死亡族 avg_mfe ≈ 0.05-0.14%, 翻绿族 mfe ≥ 0.3%)

**接线**:
- `pipeline.py` L4222: re-price 后, 新增 `predict_dead_signal(anchor_dev_pct, atr_pct) -> bool` 调用
- `instruction_generator.py`: 新增 `predicted_dead` 字段写入 raw_instruction_json (无 schema 迁移)
- `playbook_instructions`: 已有 anchor_dev_pct + atr_pct 字段
- 消费者: `scripts/entry_delay_confirm_shadow.py` 已支持 "避免率" 度量 (SL_INITIAL 人群), 新增死亡族人群
- `cf_pullback_limit_replay.py`: 新增 "predicted_dead 过滤" 重放模式

**可验证性**:
- 影子: 在 trade_records 上, 回放 "如果 re-price 时代理变量预测为 dead → 不入场" 的效果
  - 看翻绿族被误杀率 (precision)
  - 看死亡族被过滤率 (recall)
  - 看过滤后净 PnL 改善 (期望: +$3529 的改善, 但扣除翻绿族误杀的损失)
- 预注册判据草案:
  - 窗口: n ≥ 40 (死亡族每月约 36 笔)
  - 门 1: 死亡族 recall ≥ 50% (过滤掉至少一半死亡族)
  - 门 2: 翻绿族误杀率 ≤ 20% (不误杀太多好单)
  - 门 3: 过滤后净 PnL 改善 ≥ $1000/月 (死亡族 -$3529 的 30%)
  - 安慰剂: 随机过滤同样比例的笔数, 净改善应 < $1000 (如果随机也改善 = 不是 mfe 预测的功劳)
  - n < 30 方向参考

### 提案 5: re-price 锚刷新确认 — 触发时用实时 VWAP 替代 DA 快照锚 (消灭 anchor_dev 的根因)

**机制**:
- 位置: `pipeline.py` L4222 re-price 块 (re-price_from → reprice_to 逻辑)
- 复算 C1: anchor_dev_pct median -0.52%, |dev|>0.5% 占 62% → DA 锚普遍过时, 靠触发时 re-price 兜底。但 re-price 当前用"触发时现价"重锚, 没有用 VWAP
- 机制: re-price 时, 用触发前 N 分钟 (如 5min) 的 **VWAP** 作为新锚 (而非触发时点价), 因为 VWAP 对开盘尖刺更鲁棒
  - 当前: `reprice_to = 触发时现价` → 开盘 30min 的尖刺价格被当锚 → SL 紧 → 被扫
  - 改为: `reprice_to = VWAP(触发前 5min)` → 锚更稳定 → SL 距离更合理 → 减少死亡族被开盘波动扫

**防自欺机制**:
- VWAP 窗口 5min 来自 entry_delay_confirm 影子 (delay 2min 避免率 50%, delay 5min 避免率 49%; 但 5min 误杀率更低 46% vs 57%)。不是窗口内调参
- 危险: VWAP 依赖历史 bar 数据, 如果 bar 数据缺失 (entry_delay_confirm 已有 `no_bar_at_target` 状态) → 回退现状
- 前视偏差检查: VWAP 用的是触发前的 bar (已发生), 非触发后 → 无前视偏差
- 危险: VWAP 锚可能比现价更远 → 限价更不可达。防自欺: 只对 PULLBACK_LIMIT 类用 VWAP (等回踩语义), BREAKOUT 不用

**接线**:
- `pipeline.py` L4222: `_new_entry` 计算逻辑, 新增 `_vwap = calc_vwap(inst['symbol'], minutes=5, before=trigger_time)` 调用
- 需要 bar 数据源: 已有 (cf_pullback_limit_replay.py 和 entry_delay_confirm_shadow.py 都消费 bar 数据)
- `playbook_instructions.reprice_audit` (L4544): 新增 `vwap_source` 字段 (无 schema 迁移, 存入 metadata_json)
- 消费者: `cf_pullback_limit_replay.py` 重放器已消费 bar 数据, 可对比 VWAP 锚 vs 现价锚

**可验证性**:
- 影子: 在 D 案 rows 上, 重放 "reprice_to = VWAP(5min)" vs "reprice_to = 触发时现价" (现有), 比较配对差
  - 重点看 9 点档死亡族: VWAP 锚是否降低被扫率 (MAE/SL 比值 < 0.05 → 更少碰到 SL)
  - 看可达桶 fill_rate: VWAP 锚更远 → 可能 fill 下降 (需要 trade-off)
- 预注册判据草案:
  - 窗口: n ≥ 40 配对
  - 门 1: VWAP 锚臂的 MAE/SL 比值 median < 0.05 (比现价锚更少碰 SL)
  - 门 2: VWAP 锚臂配对差 > 0 (VWAP 优于现价)
  - 门 3: 可达桶 fill_rate ≥ 40% (允许比现价锚的 50% 低, 因 VWAP 更远)
  - 门 4: ADR-035 四条全过 (n≥20, 期望改善≥30%, 成交率≥50%, mfe=0 占比降) — 注意: 如果 fill_rate < 50% → 自动不达标, 需要调整 VWAP 窗口
  - n < 30 方向参考

---

## 三、提案间关系与优先级建议

| 提案 | 针对根因 | 预期改善 | 风险 | 依赖 |
|---|---|---|---|---|
| 1. DA 锚新鲜度门 | 过时锚当限价位 | 减少不可达桶 (D2: 45 笔 fill 0%) | 可能误杀 stale 但可达的好单 | 独立 |
| 2. 开盘 30min 限价优先 | 9 点档市价追入死亡族 | 减少死亡族 (B3: -$3529) | 基率混淆风险 (C8 前科) | 独立 |
| 3. 限价 TTL 分层 | 远距限价挂太久 | 保留可达桶价值 (D4: +9.3/+28.2) | 短 TTL 误杀 | 独立 |
| 4. 死亡族前置过滤 | 入场即错族 | 直接消灭 -$3529 | 代理变量预测不准 | 独立 |
| 5. re-price VWAP 锚 | 开盘尖刺被当锚 | SL 更合理, 减少被扫 | VWAP 更远 → fill 降 | 独立 |

**反幻觉角色建议**:
- 提案 1+3 可合并 (DA 锚新鲜度门 + TTL 分层 = "只对新鲜且可达的锚路由限价")
- 提案 2 需先过基率检查 (C8 前科), 不急于接线
- 提案 4 是期望改善最大的 (直接消灭 -$3529), 但前视偏差风险最高, 需最严格的影子验证
- 提案 5 风险中等, 但 ATR 口径问题 (D6: real 仅 22/123 = 18%) 可能限制 VWAP 效果

**费用线回应**: 复算显示实际改善门槛是 6.9bp (非 15bp)。提案 1+3 的可达桶配对差 +9.3~+28.2 (D4), 折算 9.3/5405=1.7bp~28.2/5405=5.2bp, 单个提案不够打平, 但 1+3+4 组合 (减少死亡族 $3529 + 保留可达桶价值) 的月度改善远超 6.9bp/笔 × 180 笔 = $1241 的门槛。

---

## 四、无法复现项声明

按宪章律7, 以下数字我**无法精确复现**, 声明原因:

1. **SL_BREAKEVEN 捕获率 0.40**: 用 `(pnl_pct×100)/mfe_pct` 得 0.47, 用 `pnl_net/(mfe_pct/100×entry_price×quantity)` 得 0.31, 均不等于 0.40。口径不明, 不影响叙事 (捕获 < 0.5), 但精确数值需文档作者澄清计算方式。
2. **死亡族 17/20**: 死亡族 (mfe<0.3%) 总 n=36, 24 在 9 点档。文档的 "20" 来源不明, 可能是某个未定义的子集。
3. **滑点 median -0.026%**: 用 (execution_price - entry_price) / entry_price × 100 得 -0.013%, 偏差 1bp。可能存在过滤条件差异。

以上三项均不影响核心叙事和数据基底的有效性, 标注为"口径待澄清"而非"数据错误"。

---

*复算代码: 全部使用 sqlite3 直查 `data/ftseek_us.db` (mode=ro) + json 解析 `data/shadow/pullback_limit_replay_v2_latest.json`, Python 3 标准库, 无外部依赖。*
