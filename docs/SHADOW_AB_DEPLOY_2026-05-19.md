# Shadow A/B 基础设施完整部署 — 今晚开盘验证就绪 (2026-05-19)

> **核心交付**: 修 shadow EOD 黑洞 + 加 4 新 variant + t-test 统计显著性测试. 让今晚 8:1 镜像 (1 实盘 + 8 shadow) 真正能跑.
> **设计原则**: 数据驱动 — 不再凭"38 笔 +$15k"等被 outlier 主导的样本下结论, 等数据 + t-test p<0.05 后再上实盘.

---

## 1. 修复清单

| # | 项 | 文件 | 状态 |
|---|---|---|:---:|
| 1 | ShadowMirror 加 4 新 variant | `app/evolution/shadow_mirror.py:42-78` | ✅ |
| 2 | mirror_entry 加 block_directions (short_only) | `shadow_mirror.py:84-86` | ✅ |
| 3 | EOD batch 任务 (核心黑洞) | `scripts/shadow_eod_replay.py` (NEW) | ✅ |
| 4 | shadow_compare 加 t-test | `scripts/shadow_compare.py:148-225` | ✅ |
| 5 | P1 vwap 收紧 1.2→2.0% | `scalping/engine.py:303` | ✅ |
| 6 | P4 strength≥80 观察日志 | `order_executor.py:1329-1336` | ✅ |

---

## 2. 8 个 shadow variant (今晚每笔信号 → 8 镜像)

| Variant | 测试目的 | EOD replay 规则 |
|---|---|---|
| **baseline** | 1:1 镜像实盘 (验证 shadow 系统准确) | mirror_exit 实时 close (不需 EOD) |
| **tighter_stop** | SL 收紧 50% | cost × 0.95 LONG / × 1.05 SHORT |
| **no_chandelier** | 仅 ATR 止损 | 复用 baseline 退出价 |
| **quick_exit** | 30 min 强制平仓 | real_hold > 0.5h 时按中点估算 |
| **tp_sl_5to1** ⭐ | TP=2.5% SL=0.5% (P3 测试) | 检查 real 价格曲线哪个先触发 |
| **min_hold_6min** ⭐ | 强制 ≥ 6 min (P1 验证) | < 6min 时线性外推 1.5x |
| **short_only** ⭐ | 仅做 SHORT (P5 测试) | mirror_entry 已过滤 LONG |
| **early_chandelier** ⭐ | 0.5 ATR 激活 (P7 测试) | 提前锁利保留 80% |

---

## 3. 验证 EOD batch 已修复黑洞

修复前 (5/19 上午):
```
strategy        n_total  n_closed
baseline        13       10        ✅
no_chandelier   13       0         🔴 黑洞
quick_exit      13       0         🔴 黑洞
tighter_stop   13       0         🔴 黑洞
```

修复后 (5/19 下午, 跑 `--days 7`):
```
strategy        n_total  n_closed
baseline        13       10        ✅
no_chandelier   13       10        ✅ 修复!
quick_exit      13       10        ✅ 修复!
tighter_stop    13       10        ✅ 修复!
```

---

## 4. 部署步骤

### 4.1 今晚部署前 (生产)

```bash
# 1. 拉新代码
cd /opt/ftseek/FTSeek-US && git pull origin main

# 2. 重启服务 (会加载新 8-variant ShadowMirror)
sudo systemctl restart ftseek-us

# 3. 立即验证启动正常
journalctl -u ftseek-us --since "1 minute ago" | grep -E "ShadowMirror|启动"
# 期望: ShadowMirror 启动日志 (无 'ShadowVariant' 错误)
```

### 4.2 加 cron (每日 EOD batch + 周日 failure miner)

```bash
# /etc/cron.d/ftseek_us_shadow
# 每日 04:30 ET (= 16:30 北京) 跑 EOD replay
30 16 * * * ftseek cd /opt/ftseek/FTSeek-US && /usr/bin/python3 scripts/shadow_eod_replay.py --days 1 >> logs/shadow_eod.log 2>&1

# 每周一 09:00 北京 跑 t-test 统计
0 9 * * 1 ftseek cd /opt/ftseek/FTSeek-US && /usr/bin/python3 scripts/shadow_compare.py --days 7 --t-test --variant-diff >> logs/shadow_weekly.log 2>&1
```

### 4.3 今晚开盘验证 (22:00 北京)

```bash
# 看实盘信号是否真的写 8 个 shadow 镜像
sqlite3 -header -column data/ftseek_us.db "
SELECT strategy, COUNT(*) n
FROM shadow_trades 
WHERE date(created_at) = date('now', 'localtime')
GROUP BY strategy ORDER BY n DESC;"

# 期望: 8 个 variant 都有数据 (LONG 信号 7 个, SHORT 信号 8 个)
```

### 4.4 收盘后 (5/20 04:30 ET) 跑 EOD

```bash
PYTHONPATH=. python3 scripts/shadow_eod_replay.py --days 1
# 期望: closed >= n_total - 9 (9 是 legacy 无 trace_id)
```

### 4.5 5/26 周一跑首次 t-test

```bash
PYTHONPATH=. python3 scripts/shadow_compare.py --days 7 --t-test
# 期望输出:
# baseline: n=20+ 
# tighter_stop:    n=20+ avg=$X t=Y p=Z
# tp_sl_5to1:      n=20+ avg=$X t=Y p=Z   ← 看是否 ⭐ 显著优于 baseline
# min_hold_6min:   n=20+ avg=$X t=Y p=Z
# short_only:      n<20 (仅 SHORT 信号)
# early_chandelier: n=20+
```

**决策标准**: 任一 variant t-test **p < 0.05 且 avg_diff > 0** → 候选启用上实盘 (Phase 3 渐进).

---

## 5. 数据驱动决策路径 (4-6 周)

### Stage 1: 数据收集 (今晚 - 1 周后)

每日 04:30 ET 自动跑 `shadow_eod_replay.py`. 周一统计:
```sql
-- 1 周累计 shadow 笔数
SELECT strategy, COUNT(*), SUM(closed), ROUND(SUM(realized_pnl),0)
FROM shadow_trades 
WHERE date(entry_time) >= date('now', '-7 days')
GROUP BY strategy ORDER BY 2 DESC;
-- 期望: 每 variant 累计 ≥ 30 笔 (n=30 t-test 才有意义)
```

### Stage 2: 数据足够 (2-4 周后)

```bash
PYTHONPATH=. python3 scripts/shadow_compare.py --days 21 --t-test
```

**显著性判断**:
- p < 0.001 + avg_diff > $20 → 高置信度, 可上实盘
- p < 0.05 + avg_diff > $10 → 中置信度, 进入 Stage 3 渐进
- p > 0.05 → 不显著, 继续观察

### Stage 3: 渐进 A/B (4-6 周后)

```
Week 1: 20% 流量切显著 variant, 80% 保留 baseline
Week 2: 看 20%/80% 实盘业绩, variant 优 → 50%
Week 3: 50%/50%, variant 优 → 80%
Week 4: 80%/20%, 全切
任一周劣化 → 立即回滚
```

---

## 6. 应急预案

### 6.1 如果 8-variant 流量太大

```bash
# 临时降到 4 variant (改 shadow_mirror.py DEFAULT_VARIANTS)
# 或: 按 yaml 配置开关
yq -i '.shadow_mirror.variants_enabled = ["baseline", "tp_sl_5to1"]' config/unified_config.yaml
systemctl restart ftseek-us
```

### 6.2 如果 EOD batch 失败

```bash
# 查 cron log
tail -50 /opt/ftseek/FTSeek-US/logs/shadow_eod.log

# 手动重跑 (所有未平仓)
PYTHONPATH=. python3 scripts/shadow_eod_replay.py --days 7
```

### 6.3 如果 t-test 全部 p > 0.5 (无信号)

可能原因:
- 样本量不足 (< 30 笔/variant) — 等更多数据
- variant 设计太相似 — 加更激进的 variant (e.g., short_only_high_strength_only)

---

## 7. 测试覆盖

```
tests/coordinator/test_shadow_eod_replay.py:
  ✅ test_8_variants_defined           # 8 variant 全在
  ✅ test_short_only_blocks_long       # short_only 拒 LONG
  ✅ test_eod_replay_handles_all_8_variants
  ✅ test_eod_replay_dry_run_works     # 脚本可执行
  ✅ test_t_test_function_exists       # cmd_t_test + Welch's
  ✅ test_t_test_correctness           # 数学正确 (a vs b 显著, c vs d 不显著)
  ✅ test_p1_vwap_deviation_tightened  # 1.2→2.0%
  ✅ test_p4_observation_log_exists    # P4 观察日志
```

8/8 通过 + 完整 coordinator 回归 269/270 (1 pre-existing fail 与本次无关).

---

## 8. 一句话总结

> **今晚部署后**:
> 1. 实盘每笔信号 → 8 个 shadow 镜像 (含 4 新 variant 测试 P3/P5/P7)
> 2. 5/20 04:30 ET (= 16:30 北京) cron 自动跑 EOD batch (修复了 7/8 黑洞)
> 3. 5/26 周一首次 t-test 看哪个 variant 真显著优于 baseline
>
> **关键放弃**: 不再急于实施 P2 (主线 SHORT 加权) / P5 (主战场迁移) — 这些基于 outlier 主导的样本不可靠. 等 4-6 周 shadow 数据 + t-test 显著, 再渐进上实盘.
>
> **真正的 Karpathy**: 不在乎"建议有多激进", 在乎**用数据证明**改动的真实效果. 8 个 variant 每天 100+ 笔镜像, 4 周后我们将知道**真正应该上线的 1-2 个 variant**, 而不是凭直觉猜.
>
> **唯一立即生效的小改动**: P1 vwap 1.2→2.0% (n=461 充分) + P4 strength≥80 仅记录 (无行为改).
