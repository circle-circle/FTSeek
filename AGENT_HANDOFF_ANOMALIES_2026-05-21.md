# 异常问题清单 — Agent Handoff (2026-05-21)

> **目标读者**: 另一个 AI Agent (你)
> **任务**: 独立检查 + 修复以下 11 个异常. 每项含: 现象 / 证据 / 代码位置 / 验证标准 / 修复建议 (建议非强制).
> **背景**: FTSeek-US 美股模拟盘交易系统. 5/19-5/21 三天累计亏损 -$6,407 (主要是期权策略灾难).
> **数据基础**: 项目根目录 `/Users/xuyuan.xy/Desktop/FTSeek-US`, DB `data/ftseek_us.db`.

---

## 📋 索引 (按严重度)

### P0 紧急 (已造成实际损失或安全隐患, 必须立即修)
- [#1 期权 selector 与 yaml 配置严重不一致 (-$5,984)](#1)
- [#2 期权硬止损 -50% 来源不明 (yaml 写 -30%)](#2)
- [#3 5/19 部署的所有 LLM/Hook 修复 0 触发](#3)
- [#4 当前持有 4 个看跌期权 PUT (12 天到期, theta 衰减风险)](#4)

### P1 重要 (影响业务但未立即损失)
- [#5 Shadow EOD batch cron 未配置 (174 笔数据 closed=0)](#5)
- [#6 yaml universe 仍 12 只老配置 (未加 ORCL 等真 alpha)](#6)
- [#7 5/21 scalping 5 笔 ghost_cleanup_cruise (broker 调用失败)](#7)
- [#8 期权持仓 sl_pct 数据异常 (TSLA PUT sl 高于 entry)](#8)

### P2 关注 (设计层问题, 长期影响)
- [#9 LLM 业务调用 0 (audit DB 仅 test 数据)](#9)
- [#10 PRE_TRADE_CHECK 拒绝 101 笔/3 天 (大量信号被风控拒)](#10)
- [#11 Phase 1 修复全部 0 日志 (代码改了但生产路径没执行)](#11)

---

## 🔧 工作守则 (给 Agent)

1. **先读不写**: 每项都先看实际代码 + 实际数据 (DB / log), 不要凭描述就改
2. **小改快验**: 单点修改 → 跑测试 → 看新数据是否变化, 不要一次改 5 处
3. **保留证据**: 修改前 grep 一下 baseline, 修改后再 grep, 留 diff
4. **失败必降级**: 任何修改要保证最坏情况下 (修 bug 反而引入新问题) 主流程仍能运行
5. **不要重写**: 用 minimal patch, 不要重构整个模块
6. **本地 import 限制**: 本机缺 pytz, 测试避免触发 scalping import 链 (用文件读源码代替)
7. **测试位置**: `tests/coordinator/`, 命名 `test_<topic>_<fix_id>.py`

---

<a id="1"></a>
## #1 🚨 期权 selector 与 yaml 配置严重不一致 (-$5,984)

### 现象
5/19-5/21 期权策略买入的是 **ATM Put** (Delta ≈ -0.5, 单价 $26-52, 单张 cost $5,000+), 但 `config/unified_config.yaml` 设计的是 **OTM Put** (Delta -0.3 到 -0.4, 单价 $0.5-5, 单张 cost ≤$2,000).

### 证据
**yaml 配置** (`config/unified_config.yaml` 搜 `^options:`):
```yaml
options:
  enabled: true
  delta_range: [-0.4, -0.3]      # OTM
  preferred_dte: 30
  price_range: [0.5, 5.0]
  max_cost_per_contract: 2000
  stop_loss_pct: -0.3
```

**代码事实** (`app/execution/options_selector.py`):
- Line 4-5: 模块标题 "ATM Put 期权选择器" — 整个模块就是设计成 ATM
- Line 324: `delta = float(row.get("delta", -0.5) or -0.5)` — hardcode -0.5
- Line 464: 同上

**实际入场 (`logs/ftseek_us.log` 搜 `构造 ATM Put`)**:
```
5/19 21:41 SNDK PUT K=$1350 estPrice=$52.57 cost=$5,257 (违反 max_cost=2000)
5/19 22:12 SPY  PUT K=$730  estPrice=$28.54 cost=$2,854 (违反 price_range)
5/19 22:57 MU   PUT K=$680  estPrice=$26.23 cost=$5,246 (违反 max_cost)
5/20 02:11 QQQ  PUT K=$710  estPrice=$26.60 cost=$5,320 (违反 max_cost)
```

实际亏损 (DB `data/ftseek_us.db` 表 `trade_records`):
```sql
SELECT trade_date, symbol, ROUND(realized_pnl,0) pnl
FROM trade_records WHERE direction='SHORT_PROXY_OPTION';
-- 5/20 QQQ -$2,934
-- 5/21 QQQ -$3,050
```

### 验证标准
修复后, 调用 `OptionsSelector.select(...)` 应返回:
- `delta` 在 `[-0.4, -0.3]` 区间 (OTM, 不是 ATM)
- `est_price * 100 ≤ max_cost_per_contract` (≤ $2,000)
- `dte` 接近 `preferred_dte` (~30 天)

### 建议修复 (非强制)
让 `options_selector.py` 读 yaml 配置, 在 strike 选择时按 Delta 区间过滤:

```python
# app/execution/options_selector.py
def select(...):
    cfg = get_config()
    delta_min, delta_max = cfg.get_dotted("options.delta_range", [-0.4, -0.3])
    max_cost = cfg.get_dotted("options.max_cost_per_contract", 2000)
    preferred_dte = cfg.get_dotted("options.preferred_dte", 30)
    
    # 过滤期权链
    candidates = [c for c in chain
                  if delta_min <= c.delta <= delta_max
                  and c.price * 100 <= max_cost
                  and c.dte >= 25]
    if not candidates:
        return None  # 没合适期权 → 不入场, 不要 fallback ATM!
    return min(candidates, key=lambda c: abs(c.dte - preferred_dte))
```

### 注意事项
- 修复前先 yaml `options.enabled = false` 紧急停止入场
- 不要一边修一边让生产跑 (会继续亏)

---

<a id="2"></a>
## #2 🚨 期权硬止损 -50% 来源不明 (yaml 写 -30%)

### 现象
5/20 QQQ PUT 亏 -55% 触发 `QUICK_EXIT_🛑 硬止损触发 (-55.0% <= -50%)`, 5/21 类似. 但 yaml `options.stop_loss_pct: -0.3` (-30%).

### 证据
**yaml** (`config/unified_config.yaml` 搜 `options:` 段):
```yaml
options:
  stop_loss_pct: -0.3   # -30%
  stop_loss_ratio: 0.5  # 注: 这个 0.5 可能是 -50% 来源!
```

**触发位置**: `app/pipeline/position_monitor.py:403`
```python
_execute_option_close_order(scheduler, position, option_code, qty, 
                            reason=f"QUICK_EXIT_{exit_reason[:20]}")
```

`exit_reason` 字符串中的 `-50%` 来自其他逻辑分支, 需要追溯.

### 验证标准
任意期权持仓亏损时, 触发硬止损的阈值应等于 `yaml.options.stop_loss_pct` (默认 -30%, 不是 -50%).

### 建议排查方向
1. grep `-0.5` 或 `0.5` 在 `app/pipeline/` 和 `app/execution/`, 找 hardcode
2. 查 `position_monitor.py` 中所有 `stop_loss` 相关逻辑
3. yaml `options.stop_loss_ratio: 0.5` 是否被误读为止损比例?

---

<a id="3"></a>
## #3 🚨 5/19 部署的所有 LLM/Hook 修复 0 触发

### 现象
5/19 部署 11 个文件修改 (Phase 1 / L3 thesis / PreEntryGate / news_classifier 迁移), 但 5/19-5/21 三天 0 触发.

### 证据
```bash
# 1. PostTradeHook stats 持续 {0,0,0}
grep -aE "PostTradeHook stats" logs/ftseek_us.log | tail -5
# 期望: submitted > 0
# 实际: 全部 {'submitted': 0, 'completed': 0, 'failed': 0}

# 2. L3 scalping 触发 thesis 应有日志
grep -aE "L3·thesis·scalping|infer_thesis_async" logs/ftseek_us.log | grep "2026-05-2" | head
# 期望: 每笔 scalping 入场后一行
# 实际: 0 行

# 3. Phase 1 gates 应有拦截日志
grep -aE "Scalping·低价股门|Scalping·regime门|Scalping冷却·loss|CircuitBreaker·噪声" logs/ftseek_us.log | grep "2026-05-2" | head
# 实际: 0 行

# 4. LLM business audit
sqlite3 data/llm_audit.db "
SELECT feature, COUNT(*) FROM llm_calls 
WHERE timestamp >= strftime('%s','2026-05-19 00:00:00')
  AND feature != 'test_feature_xyz'
GROUP BY feature;"
# 实际: 0 行 (只有 test_feature_xyz 7 笔)
```

### 关键线索
5/21 5 笔 scalping 都是 `ghost_cleanup_cruise` (入场未成交). 说明 scalping 在 broker 调用层就失败了, **永远到不了** L3/PostTradeHook 调用点.

```sql
SELECT symbol, exit_reason FROM trade_records
WHERE trade_date='2026-05-21' AND owner_strategy='scalping';
-- 5 行全部 ghost_cleanup_cruise
```

### 验证标准
修复后期望:
- scalping 入场成功 (不再 ghost_cleanup_cruise)
- 每笔 scalping 入场后 logs 中有 `L3·thesis·scalping` 或 `ThesisInferer-` 行
- audit DB `feature='thesis_inferer'` 行数 ≥ scalping 入场笔数 × 0.8

### 建议排查
1. **关键文件**: `app/scalping/engine.py:1869+ _execute_trade`
   - L3 thesis 触发位置: line 2300-2316 (在 attribution 写入后)
2. **检查 broker 调用失败原因**:
   ```bash
   grep -aE "broker.*reject|order.*fail|ghost" logs/ftseek_us.log | tail -10
   ```
3. **可能原因**:
   - 富途模拟盘异常 (broker_adapter)
   - 信号 cooldown 路径有 bug
   - 主动重启导致状态不一致

---

<a id="4"></a>
## #4 🚨 当前持有 4 个看跌期权 PUT (12 天到期, theta 衰减风险)

### 现象
当前 `open_positions` 表持有 4 个看跌期权 PUT, 全部 6/2-6/3 到期 (剩 ~12 天):

```sql
SELECT symbol, size, ROUND(avg_entry_price,2) entry, ROUND(current_sl,2) sl 
FROM open_positions WHERE size > 0 AND symbol LIKE '%P%';
```
```
US.QQQ260602P700000   2 张  entry=$9.79
US.QQQ260602P710000   2 张  entry=$13.60
US.SPY260602P730000   1 张  entry=$6.89
US.TSLA260603P410000  6 张  entry=$12.91
```

总 cost ~ $9.79×2×100 + $13.60×2×100 + $6.89×1×100 + $12.91×6×100 ≈ **$13,000**

### 风险
- Theta 衰减: 12 天到期, 每天加速衰减
- 5/20 + 5/21 已经各亏 $3k, 还有 4 张可能继续亏
- 即使期权设计正确, 持有到期前必须主动管理

### 验证标准
- 4 个 PUT 持仓主动平仓 (broker 端)
- `open_positions` 表中 `symbol LIKE '%P%'` 行数 = 0

### 建议
1. **立即平仓** (今晚 broker 端手动)
2. 或加自动管理脚本 `scripts/close_expiring_options.py`:
   ```python
   # 持有期权 DTE < 5 天自动平
   if option.dte < 5:
       close_position(option)
   ```

---

<a id="5"></a>
## #5 🟡 Shadow EOD batch cron 未配置 (174 笔数据 closed=0)

### 现象
5/19+ shadow A/B 系统启动了 (8 个 variant 各 23 笔 = 174 笔镜像入场), 但**全部 closed=0**, 没回放 PnL.

### 证据
```sql
SELECT strategy, COUNT(*) n_total, SUM(closed) n_closed
FROM shadow_trades WHERE date(created_at) >= '2026-05-19'
GROUP BY strategy;
-- 8 行, 每行 n_total=21-23, n_closed=0
```

### 根因
- 脚本 `scripts/shadow_eod_replay.py` 已存在 (5/19 我写的, 验证 dry-run 工作)
- 但**没有 cron 配置**自动每天 04:30 ET 跑

### 验证标准
- 设 cron 后, 隔天 (5/22 04:30 ET 后) `n_closed > 0`

### 建议修复
```bash
# 在生产服务器添加 cron
sudo tee /etc/cron.d/ftseek_us_shadow > /dev/null <<EOF
# 每日 04:30 ET = 16:30 北京 (盘后) 跑 shadow EOD replay
30 16 * * * ftseek cd /opt/ftseek/FTSeek-US && \
  /usr/bin/python3 scripts/shadow_eod_replay.py --days 1 \
  >> logs/shadow_eod.log 2>&1

# 每周一 09:00 北京 跑 shadow t-test 统计
0 9 * * 1 ftseek cd /opt/ftseek/FTSeek-US && \
  /usr/bin/python3 scripts/shadow_compare.py --days 7 --t-test \
  >> logs/shadow_weekly.log 2>&1
EOF

# 立即手动跑一次, 回放 174 笔
PYTHONPATH=/opt/ftseek/FTSeek-US python3 \
  /opt/ftseek/FTSeek-US/scripts/shadow_eod_replay.py --days 3
```

---

<a id="6"></a>
## #6 🟡 yaml universe 仍 12 只老配置

### 现象
`config/unified_config.yaml` 中 `mode.sandbox.universe` 仍是 5/16 老配置:
```yaml
universe:
  - NVDA, TSLA, AMD, RIVN, LCID, SNDK, ARM, NIO, BWXT, MU, SPY, QQQ
```

但 5/19 audit 数据驱动建议 (`docs/SUBSCRIPTION_UNIVERSE_AUDIT_2026-05-19.md`):
- 应**加** ORCL (历史 78 笔 +$20,492), MSFT (+$3,142), NTES (+$1,855), AAPL, JPM
- 应**删** TSLA/AMD/NIO (0 交易), NVDA (-$2,905), MU (-$216), LCID (摩擦灾难)

### 证据
```sql
-- 1 个月内 universe 外的真 alpha
SELECT symbol, COUNT(*) n, ROUND(SUM(realized_pnl),0) pnl
FROM trade_records 
WHERE realized_pnl IS NOT NULL AND trade_date >= '2026-04-24'
  AND symbol NOT IN ('US.NVDA','US.TSLA','US.AMD','US.RIVN','US.LCID','US.SNDK',
                     'US.ARM','US.NIO','US.BWXT','US.MU','US.SPY','US.QQQ')
GROUP BY symbol ORDER BY pnl DESC LIMIT 5;
-- US.ORCL 78 笔 +$20,492 ← 最大 alpha, 不在 universe!
-- US.MSFT 1 笔 +$3,142
-- US.NTES 1 笔 +$1,855
```

### 验证标准
yaml `mode.sandbox.universe` 含 ORCL, MSFT 等; 重启后 logs 中 `已订阅 X 只美股` 应包含 ORCL.

### 建议修改
```yaml
mode:
  sandbox:
    universe:
      # 保留 (历史盈利): 5
      - ARM, RIVN, SNDK, SPY, BWXT
      # 新增 (真 alpha): 5
      - ORCL          # 78 笔 +$20k (最大 alpha)
      - MSFT          # +$3.1k
      - AAPL          # 大盘
      - NTES          # 中概
      - JPM           # 金融
      # 保留 (主仓监控): 1
      - QQQ
      # 删除: TSLA AMD NIO NVDA MU LCID (拖累 / 0 交易)
```

注意: 修改后需重启服务, 同时确保 `app/data/futu_lv3.py` 也按新 universe 订阅 (双套订阅一致性).

---

<a id="7"></a>
## #7 🟡 5/21 scalping 5 笔 ghost_cleanup_cruise (broker 调用失败)

### 现象
5/21 凌晨 00:35-01:59 共 5 笔 scalping SHORT 入场记录, 但全部 `exit_reason='ghost_cleanup_cruise'`, 入场未成交.

### 证据
```sql
SELECT entry_time, symbol, owner_strategy, exit_reason
FROM trade_records WHERE trade_date='2026-05-21' AND realized_pnl IS NOT NULL
ORDER BY entry_time;
```
```
00:35:01 BA   scalping SHORT ghost_cleanup_cruise
01:35:32 BA   scalping SHORT ghost_cleanup_cruise
01:45:33 BA   scalping SHORT ghost_cleanup_cruise
01:46:25 UNH  scalping SHORT ghost_cleanup_cruise
01:59:28 ARM  scalping SHORT ghost_cleanup_cruise
```

### 影响
- 链路断 → L3 thesis_inferer 0 触发 (#3 直接关联)
- 信号生成了但没成交, 浪费机会
- 同 symbol (BA) 1h 内 3 次入场尝试 — cooldown 似乎没拦住

### 验证标准
查 logs 找 broker 调用失败具体原因:
```bash
grep -aB3 -aA3 "ghost_cleanup_cruise" logs/ftseek_us.log | grep "2026-05-21" | head -30
grep -aE "broker.*reject|订单.*失败|order.*fail" logs/ftseek_us.log | grep "2026-05-21" | head
```

### 建议排查
1. **broker_adapter 模拟单失败原因**: 富途模拟盘 broker 可能在某些条件下拒单 (比如 PDT 或时段限制)
2. **scalping cooldown 失效**: BA 1h 内 3 次, 应该被 cooldown 拦
3. **查 BA 的具体 entry_price**: 是不是数据异常导致 invalid order

---

<a id="8"></a>
## #8 🟡 期权持仓 sl_pct 数据异常

### 现象
TSLA260603P410000 (TSLA Put 持仓) `current_sl > avg_entry_price`, sl_pct=+6.3%.

### 证据
```sql
SELECT symbol, ROUND(avg_entry_price,2) entry, ROUND(current_sl,2) sl,
       ROUND((current_sl - avg_entry_price)/avg_entry_price*100, 1) as sl_pct
FROM open_positions WHERE symbol='US.TSLA260603P410000';
-- entry=12.91 sl=13.73 → sl_pct=+6.3%
```

**期望**: PUT (LONG 持仓, 期权多头) 的 SL 应该是 entry × (1 - stop_loss_pct), 即 < entry. 实际 sl > entry 是错的.

### 影响
- 触发硬止损时机错误
- 可能根本不会触发止损 (price 永远不会上穿 sl)

### 建议排查
1. `app/core/position_sync.py:_calc_option_sl` 或类似 SL 计算逻辑
2. 检查 PUT vs CALL 方向计算是否搞反了 (PUT 涨意味着标的跌, 但持有 PUT 是 LONG, SL 应该是 -X%)

---

<a id="9"></a>
## #9 🟡 LLM 业务调用 0 (audit DB 仅 test 数据)

### 现象
5/19+ `data/llm_audit.db` 业务 feature 调用为 0:
```sql
SELECT feature, COUNT(*) FROM llm_calls 
WHERE timestamp >= strftime('%s','2026-05-19 00:00:00')
  AND feature != 'test_feature_xyz'
GROUP BY feature;
-- 0 行
```

但 yaml `llm.features` 配置了 12+ 个 feature, scheduler 启动时也加载了.

### 影响
- thesis_inferer / news_classifier / pre_entry_gate 等都没真触发
- LLM 链路实际是死的

### 验证标准
随便一个 feature (如 thesis_inferer) 应至少 1 次/天调用记录.

### 建议排查
与 #3 关联. 关键检查:
1. `app/llm/model_call.py:call_llm()` 是否真被业务代码调用
2. `_record_audit()` 是否被调 (特别是 schema 失败时, L2-B 修复应记录 raw)
3. 加诊断日志:
   ```python
   # app/llm/model_call.py 头部
   logger.info(f"[ModelCall·entry] feature={feature}")
   ```
4. 看 thesis_inferer.py 是否被任何 import 触发

---

<a id="10"></a>
## #10 🟡 PRE_TRADE_CHECK 拒绝 101 笔/3 天

### 现象
5/19-5/21 三天:
```sql
SELECT reason_category, COUNT(*) FROM rejections
WHERE date(timestamp) >= '2026-05-19' GROUP BY reason_category;
-- PRE_TRADE_CHECK     101 (大部分)
-- SHORT_TRIPLE_CONFIRM 32
-- SHORT_STRENGTH_WEAK   8
```

### 影响
- 大量信号被风控拒, 但具体拒因细分不清
- `PRE_TRADE_CHECK` 是个笼统的拒因, 内含多种子规则

### 建议排查
1. `app/risk/pre_trade_check.py` 看具体 reason 子分类
2. 改进 rejections 表加 `reason_detail` 字段记录子原因
3. 看 SHORT_STRENGTH_WEAK 8 笔 — 我之前 5/19 删除了 short_min_strength<20, 怎么还有? (验证修复是否生效)

---

<a id="11"></a>
## #11 🟡 Phase 1 修复全部 0 日志

### 现象
5/19 我加的 5 项 Phase 1 修复 (代码已 commit), 但生产 0 日志:
- B.2 低价股门 (`app/scalping/engine.py:1881+`)
- C.1 regime门 (`app/scalping/engine.py:1894+`)
- B.1 sl_hit 90s 二级冷却 (`app/scalping/engine.py:1297+`)
- C.2 CB 噪声过滤 (`app/scalping/circuit_breaker.py:57+`)
- A.1 删除 SHORT_STRENGTH_WEAK 门 (`app/pipeline/order_executor.py:792-803` 已删)

### 证据
```bash
grep -aE "Scalping·低价股门|Scalping·regime门|Scalping冷却·loss|CircuitBreaker·噪声" logs/ftseek_us.log | grep "2026-05-2" | head
# 0 行
```

### 关联
与 #7 关联: scalping 5 笔 ghost_cleanup_cruise 说明 scalping 入场链路在 broker 调用前就失败. Phase 1 gates 都在 `_execute_trade` 中, 入场失败就到不了.

### 验证标准
修复 #7 (broker 调用失败) 后, scalping 入场成功的笔数应触发 Phase 1 gates 中至少 1 个 (有日志).

---

## 📊 关键 SQL 查询 (供 Agent 调研用)

```sql
-- 三天战损全貌
SELECT trade_date, COUNT(*) n, ROUND(SUM(realized_pnl),0) pnl,
       SUM(CASE WHEN direction='SHORT_PROXY_OPTION' THEN 1 ELSE 0 END) opt_n,
       ROUND(SUM(CASE WHEN direction='SHORT_PROXY_OPTION' THEN realized_pnl ELSE 0 END),0) opt_pnl
FROM trade_records WHERE trade_date >= '2026-05-19' AND realized_pnl IS NOT NULL
GROUP BY trade_date ORDER BY trade_date;

-- 当前期权持仓
SELECT symbol, size, ROUND(avg_entry_price,2) entry, ROUND(current_sl,2) sl,
       ROUND((current_sl - avg_entry_price) / avg_entry_price * 100, 1) sl_pct
FROM open_positions WHERE size > 0;

-- shadow A/B 收集情况
SELECT strategy, COUNT(*) n_total, SUM(closed) n_closed
FROM shadow_trades WHERE date(created_at) >= '2026-05-19'
GROUP BY strategy ORDER BY n_total DESC;

-- LLM 链路健康
SELECT feature, COUNT(*) FROM llm_calls
WHERE timestamp >= strftime('%s','2026-05-19') AND feature != 'test_feature_xyz'
GROUP BY feature;

-- 拒绝分布
SELECT reason_category, COUNT(*) FROM rejections
WHERE date(timestamp) >= '2026-05-19' GROUP BY reason_category ORDER BY 2 DESC;
```

---

## 📁 关键文件 (供 Agent 直接读)

| 维度 | 文件 |
|---|---|
| 期权选择 | `app/execution/options_selector.py` |
| 期权止损 | `app/pipeline/position_monitor.py:403` |
| Yaml 配置 | `config/unified_config.yaml` (搜 `^options:` / `^mode:`) |
| Scalping 入场 | `app/scalping/engine.py:1869` `_execute_trade` |
| 主线入场 | `app/pipeline/order_executor.py:1290` `_execute_long`, `:1147` `_execute_stock_short` |
| Pre-Entry Gate | `app/llm/pre_entry_gate.py` |
| LLM 调用 | `app/llm/model_call.py:251` `call_llm` |
| Shadow EOD | `scripts/shadow_eod_replay.py` |
| 5/19 详细 audit | `docs/COMPREHENSIVE_OPTIMIZATION_2026-05-21.md` |
| 5/19 universe 建议 | `docs/SUBSCRIPTION_UNIVERSE_AUDIT_2026-05-19.md` |

---

## ✅ 总成功标准

修复全部完成后, 期望 5/22 单日数据:
1. **0 笔期权交易** (P0-1/2/4: 期权策略已禁用)
2. **PostTradeHook stats > 0** (#3/11: hook 链路通了)
3. **shadow_trades closed > 0** (#5: EOD cron 工作)
4. **logs 含 universe 含 ORCL** (#6: yaml 已改)
5. **0 笔 ghost_cleanup_cruise** (#7: broker 调用失败修了)
6. **LLM audit 业务 feature 调用 ≥ 5 笔** (#9: 链路通了)
7. **TSLA PUT sl < entry** (#8: SL 计算修了)

---

## 🎯 优先级建议 (给 Agent 的执行顺序)

1. **第 1 步 (5 min)**: 把 yaml `options.enabled = false` 紧急禁用期权 (#1, #2 立即止血)
2. **第 2 步 (10 min)**: 平掉 4 个看跌期权 (#4, broker 端)
3. **第 3 步 (30 min)**: shadow EOD cron + 立即手动跑一次回放 174 笔 (#5)
4. **第 4 步 (30 min)**: yaml universe 加 ORCL (#6, 1 行改)
5. **第 5 步 (1-2h)**: 排查 #7 broker 调用失败 (是 #3/#11 的根因)
6. **第 6 步 (2-3h)**: 修 #1 期权 selector 读 yaml (短期不要重启用 options)
7. **第 7 步 (1-2 天)**: 修 #2/#3/#9, 加诊断日志, 验证生效路径

---

## ⚠️ 不要做的事

1. **不要重写 options_selector.py** — 用 minimal patch 加 yaml 读取
2. **不要重启用 options 直到 #1+#2 都验证** — 会继续亏
3. **不要乐观估计 P2 数据** — 需要 1+ 周数据才能下结论
4. **不要在不充分了解时改 main strategy** — 主线 SHORT 历史只有 3 笔样本
5. **不要 import scalping 的整个模块跑测试** — 本地缺 pytz, 用文件读源码

---

## 📞 Handoff 完成

**总结**: 三天 -$6,407 主因是期权策略 selector 与 yaml 配置不一致 + 5/19 修复全部生产 0 触发. 11 个异常按 P0/P1/P2 分类, 每个都有具体证据 + 代码位置 + 验证标准.

**Agent 你的任务**: 按 P0 → P1 → P2 顺序处理, 每修一个验证一个, 不要一次改多个. 修完写 `docs/AGENT_FIX_REPORT_<date>.md` 报告每项的修改内容 + 测试结果 + 数据驱动验证.
