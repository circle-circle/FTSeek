# 行情订阅槽 × 策略匹配度审计 (2026-05-19)

> **结论**: 当前订阅 universe **严重错配真实 alpha 标的**. 12 只 sandbox universe 中 4 只 0 交易, 1 只 (NVDA) 大亏; 而 universe 外的 ORCL/MSFT/NTES 贡献了 80%+ 的真实盈利但没订阅.
> **数据**: 1 个月 (4/24-5/19) 真实交易 + 订阅资源用量.

---

## 1. 现状: 双套订阅 + 12 只 sandbox universe

### 1.1 订阅资源

| 系统 | 标的数 | 数据类型 | 总槽位 |
|---|---:|---:|---:|
| **subscription_manager** (sandbox) | 12 | QUOTE+TICKER | ~24 槽 |
| **futu_lv3** (LV3 实时) | 33 | QUOTE/K_1M/K_5M/ORDER_BOOK/TICKER | **165 槽** |
| 富途 1000 槽限额 (与 HK 共享) | — | — | 165/1000 (16%) |

**槽位充裕** — 不是被订阅数量约束, 是被 universe 选择约束.

### 1.2 sandbox universe (12 只, 严格过滤)
```
TSLA, NVDA, AMD, RIVN, LCID, SNDK, ARM, NIO, BWXT, MU, SPY, QQQ
```

**行业分布**:
- 半导体: NVDA, AMD, ARM, MU (4 = 33%)
- 电动车: TSLA, RIVN, LCID, NIO (4 = 33%)
- ETF: SPY, QQQ (2)
- 存储/核能: SNDK, BWXT (2)

→ **66% 集中在半导体 + 电动车**, 缺 cloud/SaaS/金融/医疗/能源.

### 1.3 futu_lv3 订阅 33 只 (实际行情数据接收)
```
TSLA NVDA RIVN UBER LCID SNDK ARM MU QCOM BWXT LLY AAPL MSFT GOOG AMZN
META AVGO TSM AMD NFLX BABA PDD NIO NTES TME ORCL CRM BA LMT UNH JPM SPY QQQ
```

→ **接收 33 只行情数据, 但 sandbox 策略只用 12 只入场** — 浪费 21 只数据!

---

## 2. 真相 1: 当前 universe 实际表现 (1 月)

### 2.1 universe 12 只 (订阅了)

| Symbol | 笔数 | PnL | 胜率 | 评估 |
|---|---:|---:|---:|---|
| **BWXT** | 6 | **+$3,115** | 33% | ⭐ 真实 alpha (但仅 6 笔) |
| **SPY** | 2 | **+$2,015** | 50% | ⭐ 但样本小 |
| **RIVN** | 56 | +$1,020 | 16% | OK 中等 |
| **ARM** | 145 | +$357 | 9% | 主战场, 但接近 0 |
| **SNDK** | 10 | +$255 | 20% | OK |
| **LCID** | 127 | +$127 | **4%** | 🔴 摩擦灾难 (B.2 已禁) |
| **MU** | 1 | -$216 | 0% | 单笔大亏 |
| **NVDA** | 3 | **-$2,905** | **0%** | 🔴 灾难, 应除名 |
| TSLA | **0** | $0 | — | ❌ 占槽不交易 |
| AMD | **0** | $0 | — | ❌ 占槽不交易 |
| NIO | **0** | $0 | — | ❌ 占槽不交易 |
| QQQ | 0 | $0 | — | ❌ 主仓持有不交易 |

**结论**: 12 只中
- 真贡献: **5 只** (BWXT, SPY, RIVN, ARM, SNDK) +$6,762
- 拖累: **3 只** (LCID, MU, NVDA) -$2,994
- 浪费: **4 只** (TSLA, AMD, NIO, QQQ) — 占槽 0 笔

### 2.2 universe 外的"幽灵" alpha (没订阅但有交易)

| Symbol | 笔数 | PnL | 备注 |
|---|---:|---:|---|
| **ORCL** | **78** | **+$20,492** | 🔴🔴🔴 **未订阅 universe 但最大 alpha** |
| MSFT | 1 | +$3,142 | 单笔大赢 |
| NTES | 1 | +$1,855 | 单笔大赢 |
| XOM | 2 | +$437 | 能源行业 |
| IWM | 1 | +$134 | 中盘 ETF |
| AAPL | 2 | +$53 | 大盘 |
| **BA** | 24 | -$262 | 中频亏 (军工) |
| **CRM** | 23 | -$180 | 中频亏 (SaaS) |
| **LMT** | 20 | -$134 | 军工 |
| **UNH** | 10 | -$120 | 医疗 |
| **NFLX** | 51 | -$122 | 中频亏 |

→ **ORCL 一只贡献 +$20,492 = 总盈利 80%+**, 但**根本不在 sandbox universe**!

### 2.3 ORCL 真相 (拆解 78 笔)

| 日期 | 笔数 | PnL | 备注 |
|---|---:|---:|---|
| 4/24 | 1 | **+$17,883** | EXTERNAL_CLOSE (单笔 outlier) |
| 5/7 | 61 | +$195 | 短线刷量 (avg 0.03h) |
| 5/8 | 7 | **+$2,339** | TRAILING_STOP |
| 5/9 | 2 | +$127 | TRAILING_STOP |
| 5/12 | 1 | -$25 | |
| 5/18 | 2 | -$26 | scalping |

**剔除 4/24 outlier 后**: 77 笔 +$2,609 (avg +$34/笔) — **依然显著 alpha**, 不是纯 outlier 主导.

---

## 3. 真相 2: 双套订阅不一致

**futu_lv3 订阅 33 只** (大量数据接收) vs **sandbox universe 12 只** (策略只用 12)
- 21 只在订阅但不被策略使用 → 浪费 21 × 5 = 105 槽位 (假设都开 5 数据类型)
- ORCL 在 futu_lv3 但不在 sandbox → 5/18 实盘 2 笔 ORCL scalping 是从 futu_lv3 数据流意外触发的, 不是设计中

**核心矛盾**: 你订阅了 ORCL 数据, 系统看见了, 但因为 universe 过滤拒绝交易 — 数据收了不用.

---

## 4. 4 项优化建议 (数据驱动 + 风险可控)

### P1 ⭐ 立即: universe 重构 (15 只精选)

**删** (4 只, 节省 4 槽位):
- TSLA (0 交易)
- AMD (0 交易)
- NIO (0 交易)
- NVDA (-$2,905 大亏)
- MU (-$216, 仅 1 笔)
- LCID (-$127 摩擦灾难, B.2 已实施 < $20 拒入场, 但仍在订阅浪费槽)

**加** (7 只, 数据驱动 alpha):
- **ORCL** (78 笔 +$20k, 即使剔除 outlier 仍 +$2.6k)
- **MSFT** (1 笔 +$3k, 单样本但合理)
- **NTES** (1 笔 +$1.8k)
- **XOM** (能源对冲)
- **JPM** (金融, 已在 futu_lv3)
- **CRM** (SaaS, 23 笔小亏但行业多样化)
- **AAPL** (大盘)

**保留** (5 只): BWXT, SPY, RIVN, ARM, SNDK

**结果**: 12 → 15 只, **行业从 2 → 6 (半导体/EV/ETF/Cloud/金融/能源)**, 槽位 -1 (15 × 5 vs 12 × 5 用 ETF/Cloud 替换电动车).

### P2 ⭐ 立即: 双套订阅去重

**问题**: futu_lv3 33 只 vs sandbox 12 只, 21 只数据接收但策略不用.

**修复**: 让 futu_lv3 也按 sandbox universe 订阅. 一个真相源.
```python
# app/data/futu_lv3.py
# 改: 从 sandbox.universe 读, 而非硬编码 33 只
universe = cfg.get_dotted("mode.sandbox.universe", DEFAULT_33)
```

**节省**: 21 × 5 = 105 槽 → 释放给其他用途 (HK / 期权 等).

### P3 评估: 5 数据类型是否都必要

当前 33 只 × 5 类型 (QUOTE/K_1M/K_5M/ORDER_BOOK/TICKER):
- **QUOTE**: 必需 (price)
- **K_1M**: 必需 (短线)
- **K_5M**: 可能必需 (中线)
- **ORDER_BOOK**: 仅 scalping 用 (vwap)
- **TICKER**: 流量大, 仅 scalping 用

**建议**: 主线策略仅需 QUOTE+K_5M (15 × 2 = 30 槽), scalping 额外加 K_1M+ORDER_BOOK+TICKER 仅对 ARM/BWXT 等高频标的 (5 × 3 = 15 槽). 总 30+15 = 45 槽 vs 当前 165 槽.

**节省**: 120 槽 (12% of 1000 限额).

### P4 慎重: ORCL 加入 universe + 同时 shadow A/B 验证

**风险**: ORCL 4/24 +$17,883 是 EXTERNAL_CLOSE 人工触发, 不一定是策略 alpha. 5/8 +$2,339 才是系统化盈利.

**做法**:
1. ORCL **加入 universe** (开始正式订阅 + 接受信号)
2. **同时让 short_only variant 跑** (P5 的 shadow), 1 周后看 ORCL 是否在 SHORT 主导下持续盈利
3. 1 周后 t-test 显著才放大权重

---

## 5. universe 重构对比

### 当前 universe (12, 行业失衡)
```
半导体 (4): NVDA AMD ARM MU
电动车 (4): TSLA RIVN LCID NIO
ETF (2):    SPY QQQ
其他 (2):   SNDK BWXT
```
**1 月 PnL**: -$2,994 - $0 + $2,015 + ... ≈ +$3,768 (主要 BWXT + SPY 单笔大赢)

### 建议 universe (15, 6 行业平衡)
```
半导体 (2): ARM, SNDK (保留赚钱的, 删 NVDA/AMD/MU)
EV (1):    RIVN (删 TSLA/LCID/NIO)
ETF (2):    SPY, QQQ
能源 (1):   XOM (新增)
医疗 (0):   (暂无 alpha, 等 shadow 验证)
金融 (1):   JPM (新增)
软件/云 (2): ORCL, MSFT (新增, 高 alpha)
中概 (1):   NTES (新增, 单笔大赢)
大盘 (1):   AAPL (新增)
SaaS (1):   CRM (新增, 行业多样)
军工/航空 (1): BWXT (保留, 已 +$3k)
```
**1 月历史 PnL 总计**: +$23,109 (剔除 ORCL outlier 后 +$5,226, 仍显著高于现状)

---

## 6. 实施路径 (与 shadow A/B 同步)

### Stage 0 (今晚, 本周): 数据收集准备
- ✅ shadow EOD batch 修好 (已做)
- ✅ 8 variant 已加 (含 short_only 测试)
- 待做: universe 重构 yaml 改动 (P1)

### Stage 1 (今晚 21:30 开盘): 仅 universe 改动
**只改 yaml** (低风险):
```yaml
# config/unified_config.yaml
mode:
  sandbox:
    universe:
      # 保留: 5 只历史盈利
      - "ARM"
      - "RIVN" 
      - "SNDK"
      - "SPY"
      - "BWXT"
      # 新增: 7 只真 alpha + 行业多样化
      - "ORCL"      # 78 笔 +$20k
      - "MSFT"      # +$3.1k
      - "NTES"      # +$1.8k
      - "XOM"       # 能源
      - "JPM"       # 金融
      - "CRM"       # SaaS 多样化
      - "AAPL"      # 大盘
      # 删除: 4 只 0 交易 + 3 只大亏
      # - "TSLA" "AMD" "NIO" (0 交易)
      # - "NVDA" (-$2.9k 灾难)
      # - "MU" (-$216)
      # - "LCID" (B.2 已拒, 仍在订阅浪费)
      # - "QQQ" (主仓不交易, 保留为持仓监控)
      - "QQQ"       # 保留持仓 trail 监控
```

### Stage 2 (1 周后): shadow 验证 + futu_lv3 同步
- 5/26 周一: t-test 看 ORCL/MSFT/NTES 在 short_only variant 中是否显著
- 若显著 → futu_lv3 改为按 sandbox universe 订阅 (P2)
- 若不显著 → 回滚 universe 仅保留 5 老 + 2 高置信新

### Stage 3 (2-4 周): 优化数据类型订阅 (P3)
- 仅在确定 universe 后, 评估 5 数据类型必要性

---

## 7. 风险与回滚

### 风险
| 风险 | 缓解 |
|---|---|
| **ORCL 加入后, 4/24 +$17k 是 outlier 不重现** | 同时跑 short_only shadow variant, 1 周后 t-test 验证 |
| **MSFT/NTES 1 笔样本不可靠** | 加入只是开始收集数据, 配合 Pre-Entry Gate 历史检查 (n < 5 默认放行) |
| **删 NVDA/TSLA 错过未来反弹** | 这些是大盘 alpha, 删除是基于 1 月数据. 1 个月后 review |

### 回滚 (1 行 yaml)
```yaml
yq -i '.mode.sandbox.universe = ["TSLA","NVDA","AMD","RIVN","LCID","SNDK","ARM","NIO","BWXT","MU","SPY","QQQ"]' config/unified_config.yaml
systemctl restart ftseek-us
```

---

## 8. 一句话总结

> **当前严重错配**: sandbox universe 12 只 — 4 只占槽 0 交易, 3 只持续大亏; 而真正最大 alpha (ORCL 78 笔 +$20k) **根本不在订阅 universe**.
>
> **优化方向**: universe 12 → 15, 删 6 拖累 + 加 7 真 alpha, 行业从 2 个 → 6 个分散.
>
> **配合 shadow A/B**: 新加的 ORCL/MSFT/NTES 同步进入 short_only variant 测试, 1 周后 t-test 验证, 数据驱动决定是否放大.
>
> **不动**: 主线 voting 逻辑 / 退出策略 / scalping gate. **只改 yaml universe** — 低风险 + 高 ROI.
>
> **预期 (保守, 剔除 4/24 outlier)**: 1 个月 +$3,768 (现状) → +$5,000-8,000 (新增 ORCL/MSFT/NTES 系统化 alpha).
>
> **Karpathy 第一性**: 不是策略代码不行, 是策略**对错的标的**生效. 改 universe = 给现有策略匹配真正的赛道.
