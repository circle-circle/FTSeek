# RTH Fade 借券费净额口径补全 (空侧毕业前置项 #1, 分支 rfc-060-phase1-hotfixes)

**日期**: 2026-09-24  
**状态**: 已落地 (随本批 commit)  
**关联**: ext-short-arm-live-20260923.md 台账点名的 P0 前置项 #1

---

## (a) accrue_borrow_fee 口径结论

### 能否离线复用？

**可以**。`app.execution.fee_model.accrue_borrow_fee` 是纯计算函数：

```python
def accrue_borrow_fee(qty: float, price: float, annual_rate_pct: float,
                      hold_days: float, basis: Optional[int] = None) -> float:
    days = int(hold_days)  # 日内=0 天, 隔夜从 1 起算
    if days <= 0 or qty <= 0 or price <= 0 or annual_rate_pct <= 0:
        return 0.0
    b = basis or get_schedule()["borrow_day_basis"]  # 缺省 365
    return round(qty * price * (annual_rate_pct / 100.0) * days / b, 4)
```

- **无 futu 依赖**: 不触发 OpenQuoteContext 连接，纯本地数学运算
- **幂等**: 给定相同输入 (qty/price/rate/hold_days)，输出恒等
- **basis 参数化**: 支持 360/365 基准切换 (缺省 365，与富途日息惯例一致)

### 如何参数化？

影子回放**无法查历史实时借券费率** (futu 接口只给当日快照)，故：

- **费率走 env 常量**: `BORROW_FEE_RATE_PCT` (缺省 1.0% = ESB 保守值)
  - ESB (easy-to-borrow): 0.5–2% 年化
  - HTB (hard-to-borrow): 5–50+% 年化 (可 env 覆写做敏感性分析)
- **新增 min_days 开关**: `BORROW_FEE_MIN_DAYS` (缺省 0)
  - 设 1 可模拟「最低 1 日借券费」的经纪商口径 (部分券商有此惯例)
- **输出字段留痕**: `borrow_fee_rate_used` / `borrow_fee_hold_days` / `borrow_fee_min_days_applied`
  - 毕业裁决可据此做敏感性分析 (不同费率假设下结论是否反转)

---

## (b) 改动文件清单与每处理由

### 1. `scripts/rth_fade_shadow.py`

**改动**:
- 新增 3 个 env 参数: `BORROW_FEE_RATE_PCT` (1.0) / `BORROW_FEE_BASIS` (365) / `BORROW_FEE_MIN_DAYS` (0)
- 新增 `estimate_borrow_fee_usd()` 辅助函数: 纯本地估算借券费，复用 fee_model 内核语义
- 在 `recs.append()` 中新增 6 个字段 (见 (c) schema 说明)
- 打印输出增加借券费与净净额提示

**理由**:
- 空侧毕业裁决前必须补的账 (记忆台账 ext-short-arm-live-20260923.md #1)
- 向后兼容: 不改既有字段 (`net_usd` / `net_usd_sl35`) 语义，只新增字段
- 幂等: 函数纯计算，同日重跑产出相同 borrow_fee_est (不破坏 a488ecb85 双侧幂等防线)

### 2. `scripts/rth_fade_graduation_check.py`

**改动**:
- 新增 `_net_usd(r)` 辅助函数: 优先读 `pnl_net_after_borrow`，向后兼容回退 `net_usd`
- 主逻辑改用 `_net_usd()` 计算 per/win/leave_net (毕业四判据)
- 打印输出增加口径提示: 多少笔含借券费字段 / 费率 / 借券费合计 / 日内=$0 说明

**理由**:
- 毕业判据切换到含借券费净额口径 (本次任务核心目标)
- 老数据 (无新字段) 自动回退 gross 口径，不阻断历史样本累积
- 阈值本身不动 (预注册纪律: 读数出来前不改判据)，只换输入口径

### 3. `tests/scripts/test_rth_fade_borrow_fee.py` (新建)

**改动**:
- 13 个测试覆盖:
  - `estimate_borrow_fee_usd`: SHORT vs LONG / 日内=$0 / 隔夜>0 / min_days 覆写 / 零费率 / 幂等性 / HTB 敏感性
  - Schema 一致性: 新字段存在 / LONG 腿恒 0
  - 毕业判据 `_net_usd`: 优先新字段 / 回退旧字段 / None 处理

**理由**:
- 记忆纪律: 新增逻辑必补测试 (且不能真实运行 rth_fade_shadow.py 主体，它连 futu)
- 幂等防线验证: 同日重跑必须产出相同 borrow_fee_est

---

## (c) 新增 schema 字段说明

`data/rth_fade_shadow.jsonl` 每行新增 6 个字段 (向后兼容，老行无这些字段):

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `borrow_fee_est` | float | 估算借券费 (USD) | 0.0 (日内) / 1.37 (隔夜 1 日) |
| `borrow_fee_rate_used` | float \| null | 实际使用的年化费率 (%) | 1.0 (ESB 缺省) / null (LONG) |
| `borrow_fee_hold_days` | float | 持有天数 (含 min_days 覆写) | 0.27 (日内 6.5h) / 1.25 (30h) |
| `borrow_fee_min_days_applied` | bool | min_days 是否生效 | true (设 1 且日内) / false |
| `pnl_net_after_borrow` | float | 主口径净净额 (net_usd - borrow_fee_est) | 24.46 (扣借券后) |
| `pnl_net_after_borrow_sl35` | float | 止损臂净净额 | 24.46 (未触发) / -175.0 (触发) |

**向后兼容**:
- 既有字段 (`net_usd` / `net_usd_sl35`) 语义不变 (仍为 gross 口径，只扣佣金)
- 下游消费者若不识新字段，继续用旧字段 (不受影响)
- 毕业判据 `_net_usd()` 优先读新字段，老数据自动回退旧字段

---

## (d) 测试结果

```
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_short_intraday_default_zero PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_short_overnight_1day PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_long_direction_zero_fee PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_min_days_override PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_zero_rate_zero_fee PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_idempotency PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestEstimateBorrowFeeUsd::test_htb_rate_sensitivity PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestRecordSchema::test_short_record_has_borrow_fields PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestRecordSchema::test_long_record_schema_consistent PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestGraduationNetUsd::test_prefers_pnl_net_after_borrow PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestGraduationNetUsd::test_fallback_to_net_usd PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestGraduationNetUsd::test_missing_both_returns_zero PASSED
tests/scripts/test_rth_fade_borrow_fee.py::TestGraduationNetUsd::test_pnl_net_after_borrow_none_falls_back PASSED

============================== 13 passed in 1.24s ==============================
```

**既有测试回归**:
- `tests/test_p1_4_gap_fade_preregistration.py`: 4 passed (未受影响)
- 禁区文件 (`test_hardcoded_utc4_ratchet.py` / `test_exit_recovery.py` / `test_breakeven_shadow_c.py`) 未触碰

---

## (e) 对毕业判据读数的预期影响方向

### 默认口径 (BORROW_FEE_MIN_DAYS=0): **影响 = $0**

RTH fade 影子严格日内 (开盘→收盘, ~6.5h):
- `hold_days = 6.5 / 24 = 0.27`
- `int(0.27) = 0` (券商自然日计息惯例: 日内不收费)
- `borrow_fee_est = 0.0`

**结论**: 默认参数下，`pnl_net_after_borrow == net_usd`，毕业判据读数**零变化**。

### HTB 敏感性分析 (BORROW_FEE_MIN_DAYS=1):

若假设「最低 1 日借券费」(部分券商惯例)，影响量级：

| 费率 | 持仓 | 借券费 | 对净/笔影响 |
|------|------|--------|-------------|
| 1.0% (ESB) | 1 日 | $0.14 | −$0.14/笔 (忽略级) |
| 10% (中等 HTB) | 1 日 | $1.37 | −$1.37/笔 (量级 ~5% 净收益) |
| 50% (重度 HTB) | 1 日 | $6.85 | −$6.85/笔 (量级 ~25% 净收益) |

**基准**: 当前回放显示空头 fade 回吐 1% 再空约 +$24.46/笔 (gross 口径)

**结论**:
- ESB 票 (费率 ≤2%): 借券费可忽略 ($0.27/笔)，不反转结论
- HTB 票 (费率 ≥10%): 借券费显著 ($1.37–$6.85/笔)，可能将 +$24.46 打到 +$17–23/笔
- 若样本中 HTB 票占比 >30%，毕业判据可能从「达标」翻转为「未达标」

**建议**:
- 当前默认口径 (日内=$0) 是保守下限 (假设券商不收费)
- 毕业裁决前，用 `BORROW_FEE_MIN_DAYS=1` + `BORROW_FEE_RATE_PCT=10.0` 跑一次敏感性分析
- 若净/笔仍 >0 且胜率 ≥55%，则结论稳健；若翻负，则须剔除 HTB 票或提高阈值

---

## 工作区改动汇总

```
M scripts/rth_fade_shadow.py              (+45 行, −8 行)
M scripts/rth_fade_graduation_check.py    (+30 行, −12 行)
A tests/scripts/test_rth_fade_borrow_fee.py (新建, 13 测试)
```

**未 commit**，按用户要求留在工作区。

---

## 下一步建议

1. **审查代码**: 确认字段命名/口径/参数缺省值符合预期
2. **跑一次真实影子** (可选): `python scripts/rth_fade_shadow.py --date 2026-09-24`，验证 JSONL 输出含新字段
3. **敏感性分析** (毕业裁决前必做): 
   ```bash
   BORROW_FEE_MIN_DAYS=1 BORROW_FEE_RATE_PCT=10.0 \
     python scripts/rth_fade_graduation_check.py
   ```
   对比净/笔是否仍达标
4. **Commit**: `git add -A && git commit -m "[RFC-060] 借券费纳入 RTH fade 影子净额口径"`
