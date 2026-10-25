# RFC-167: 空头近失带影子 (2–3% 跳空) + 延长时段死配置清理

- 日期: 2026-10-25
- 类型: L1 可观测性新增 (record-only, **零下单 / 零改闸**)
- 触发: 用户问「延长时段的交易过程和逻辑是否有必要提升优化」。复核 A10 全量后, 唯一有数据支撑的
  扩量轴是空头通道, 但该档**从未被测量** ⇒ 先落影子, 不先改参。

## 1. 数据依据 (A10, 出场日口径, `pnl_net`)

| 人群 | n | 净额 | 每笔 | 说明 |
|---|---|---|---|---|
| EXT 自有单 (全期) | 23 平 + 1 持仓 | −115.45 | — | 全被 9/17 仙股批 (−648.86) 拖累 |
| EXT 自有单 (剔除 9/17 批, `$5` 地板后) | 19 | **+533.41** | **+$28.1 / +59.0bp** | 费仅 9.0bp, 16W/3L |
| └ 其中 LONG | 15 | +252.27 | +$16.8 | 涨幅带 0.5–2.0% |
| └ 其中 **SHORT** | **4** | **+281.14** | **+$70.3** | 跳空 ≥ 3.0%, 100% WR |

空侧每笔是多头的 **4.2 倍**, 但 6 周只出了 4 个信号 —— 因为 `EXT_ENTRY_SHORT_GAP_MIN=3.0`
把 2–3% 跳空的 fade 全部挡在门外, 而 `grep -rn "short_gap" data/shadow/` 为空:
**这一档没有任何影子在测**。直接降到 2.0 会改变成交集合 (交易行为变更, L2+) ⇒ 本 RFC 只补测量面。

同时 (P3): `crontab` 里 `EXT_PREMARKET_FLATTEN_FROM=925` 自 2026-10-08 用户改「默认移交盘中」后
就是死配置 (`EXT_PREMARKET_FLATTEN` 默认 false, 见 `scripts/premarket_exit_shadow.py` 头注释),
留着会让下一轮复盘读成「09:25 仍在无条件收口」——纯误判源, 一并摘除。

## 2. 改动 (文件级)

| 文件 | 改动 |
|---|---|
| `scripts/extended_hours_entry_executor.py` | 新增 `_short_nearmiss_lo()` / `_record_short_nearmiss()` / `_classify_ext_direction()`; 两条扫描臂 (dry 观测臂 + live 臂) 的内联式 `"SHORT" if (SHORT_ALLOW and change_pct >= SHORT_GAP_MIN) else "LONG"` 统一改为调 SSOT 助手 |
| `scripts/settle_ext_short_nearmiss.py` | **新** 结算器 (`--self-check` 三态核验 + 日常结算) |
| `tests/test_ext_short_nearmiss_shadow.py` | **新** 12 钉: 判定语义不变 / 带内外落行 / 去重 ticks / 写盘炸了不改判定 / 源码内联式消失 / 结算数学 / no_bars 不静默 / 幂等 / 当日不结 / 单笔主导拦 READY / 样本门 |
| `docs/CONTROL-LOOP-LEDGER.md` | 新环 `crontab-ext-short-nearmiss-settle` 入账 |
| crontab | premarket + afterhours 两行加 `EXT_ENTRY_SHORT_NEARMISS_BAND=2.0`; 退出 cron 摘 `EXT_PREMARKET_FLATTEN_FROM=925`; 新增 `10 6 * * 2-6` 结算行 |
| `docs/decisions/RFC-149-*.md` §3 | 补「研究臂到期裁决须双读数 (剔最大单笔)」纪律 (P2, 见 §6) |

判定语义等价性由源码契约测试钉住: 内联式不得再出现, 调用点必须恰好 2 个。

## 3. 预注册 (写死在脚本头, 事后不得改 — 否则属阈值购物)

记录 (record-only):`[band_lo, SHORT_GAP_MIN)` 区间的候选按 `(ET日, 时段, 标的)` 去重落
`data/shadow/ext_short_nearmiss/<date>.json`; 首 tick 的价/涨幅 = 事件当时读数, 后续 tick 只累加 `ticks`。

结算: 假设按记录价 `px` 在**记录时刻开空**, 取记录时刻之后**第一个 RTH 时段**的日 K:
```
mfe_pct   = (px - low)   / px * 100
mae_pct   = (high - px)  / px * 100
close_pct = (px - close) / px * 100
verdict   = FADE if mfe_pct >= 0.50 else NO_FADE      # 0.50% = 往返费 0.18% + 薄盘滑点缓冲 0.32%
```

裁决门 (四条全满足才允许提案把 `SHORT_GAP_MIN` 3.0 → 2.0):
1. `n_settled >= 30`
2. `FADE 率 >= 55%`
3. 净 edge = `mean(mfe) - mean(mae) >= 0.30pp`
4. **剔最大单笔后仍过 3** (防 PEP 式单笔主导 — 研究臂 10 笔 +94.64 里 PEP 一笔占 113%, 剔后 9 笔 −12.00)

任一条不满足 ⇒ 判 `NO_EDGE`, 关闭记录面, 不得以「样本再多攒攒看」为由延长。

## 4. 验证 (实跑)

```
venv/bin/python -m pytest tests/test_ext_short_nearmiss_shadow.py -q   → 12 passed
venv/bin/python -m pytest tests/test_control_loop_ledger.py -q         → 10 passed (新环已入账)
venv/bin/python -c "import scripts.extended_hours_entry_executor"      → 导入无副作用
crontab -l | grep -c EXT_ENTRY_SHORT_NEARMISS_BAND=2.0                 → 2 (盘前/盘后各一)
crontab -l | grep -c "EXT_PREMARKET_FLATTEN_FROM=925 "                 → 0 (env 已摘, 注释留痕 1 处)
venv/bin/python scripts/settle_ext_short_nearmiss.py --self-check       → TOO_EARLY (rc=4)
```

**Gate 6 诚实声明 (三态纪律)**: 部署时刻 = 周日, EXT 窗口 (ET 04:00–20:00 工作日) **未开**
⇒ 记录面零行属 **TOO_EARLY, 不可判**, 不等于「接线活」。首次真实落行应在
**2026-10-26 (周一) 16:00 CST 起的盘前窗**; 核验方式 (只读):

```bash
ls -la data/shadow/ext_short_nearmiss/                                    # 有 <date>.json = 落行
grep "空头近失带\|闸计数\[premarket\]" logs/ext_entry.log-<date>          # 记录行随闸计数同批打印
venv/bin/python scripts/settle_ext_short_nearmiss.py --self-check          # LIVE / DEAD / TOO_EARLY
```

若 10-27 收盘后仍是 TOO_EARLY/DEAD ⇒ 按序查: ①cron 两行 env 是否存活 (`crontab -l | grep NEARMISS`)
② executor 是否读到 (`grep -c '闸计数' 当日日志`) ③ 当日候选是否真的没有落在 2–3% 带 (从闸计数与
异动榜 chg 分布确认 —— 这是合法空集, 不是故障)。

## 5. 回滚

- 记录面: 删 crontab 两行的 `EXT_ENTRY_SHORT_NEARMISS_BAND=2.0` (下一 tick 生效, 无需重启)。
- 结算面: 删 `10 6 * * 2-6 ... settle_ext_short_nearmiss.py` 行。
- 代码: 单文件 revert (`_classify_ext_direction` 恢复内联式即可, 判定语义不变)。
- 已落 JSON/jsonl **留档不删** (已产生样本不销毁)。
- 死配置清理 (`FLATTEN_FROM`) 的回滚 = 把 env 加回, 但它本来就是空转, 无行为影响。

## 6. 附: P2 研究臂到期裁决的双读数纪律 (已写入 RFC-149 §3)

`research_watchlist` 8 笔 +78.44 / `research_beat` 2 笔 +16.20 = 合计 10 笔 +94.64,
但**剔掉最大单笔 (PEP +106.64) 后 9 笔 = −12.00**。RFC-149 §3 的原判据「无成交或净 PnL 为负 ⇒
按无 edge 关闭」在单笔主导下会被误读成「正 edge 可转正」。故补: 到期裁决必须同时给
**全样本 / 剔最大单笔 / 分 source** 三行读数, 且任一行不过线即按 NO_EDGE 处置;
`research_momentum` 臂 6 周 0 笔, 10-31 到期自动失效 (无需动作)。

## 7. 明确不做 (边界)

- 不改 `SHORT_GAP_MIN` / `EXT_ENTRY_CHG_BAND` / 量闸 / `$5` 地板 / `EXT_CERT_LOCK_PCT` (全部有 A10 证据支持现状)。
- 不放宽量闸: 影子分档实测每天只多放 1 只 (US.BOLT) 且不在涨带内。
- 不复活 RFC-104 计划层 (10-15 已 FAIL 关停)。
- 不新增供给源: 产能瓶颈由研究源臂承担 (到 11-13/11-30), 同时开第二条会污染归因。