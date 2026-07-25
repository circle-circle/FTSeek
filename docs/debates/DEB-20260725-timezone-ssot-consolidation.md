# DEB-20260725: 时区 SSOT 全仓统一 (修尺子)

> **状态**: ✅ 已验证 (2026-07-25)
> **分类**: 修尺子 (度量/校准修复) — 零交易行为变更, 零 config_version 影响
> **变更纪律合规**: 白名单"修尺子"类; 不碰退出参数/信号分布/config_version; 独立 git revert 回滚

---

## 一、问题定性

全仓时区使用存在 **5 类 Critical + 4 类 High** 不一致, 导致:
- 日亏损守卫/熔断器读错交易日 (C1/C2)
- 冬令时 (EST) 全部硬编码 `-4` 偏移差 1 小时 (C5)
- 两个时区工具模块 naive 假设相反 (C3: CST vs UTC, 差 8-13h)
- `entry_time` naive/aware 混用潜在 TypeError (H1)
- 心跳信号漏斗 cutoff 偏移 ~12h (H2)
- 50+ 处已废弃 `datetime.utcnow()` (H4, Python 3.12 deprecated)
- pytz `replace(tzinfo=)` 产生 LMT 偏移 (H6)

**实锤事故**: 07-24 熔断器日亏读到前一交易日 (handoff 观察⑤), 心跳 SQL `date('now','-4 hours')` 冬季会错日。

## 二、修复清单 (8 项, 134 files)

| # | 严重度 | 修复 | 文件数 |
|---|--------|------|--------|
| C1 | Critical | `trade_recorder.py` trade_date 二次转换 → `get_trade_date()` | 1 |
| C2 | Critical | `symbol_daily_loss_guard.py` 读写契约统一 (48h 宽窗口 + `_et_trade_date`) | 1 |
| C3 | Critical | `datetime_utils.py` 合并为 shim → 消除 naive 假设冲突 | 2 |
| C4/C5 | Critical | 22 处硬编码 `-4` + 月份 DST 判断 → SSOT `now_et()`/`ET` | 22 |
| H1 | High | 5 处 `entry_time=datetime.now()` → `now_et()` + `astimezone` 修正 | 5 |
| H2 | High | `intraday_heartbeat.py` 信号漏斗/显示/推送统一 ET | 1 |
| H3/H4 | High | app/ 下 50+ 处 `utcnow()` → `now_utc()`/`now_et()` (0 残留) | 18 |
| H6 | High | pytz `replace(tzinfo=)` → `localize()` (timezone.py + risk 模块) | 4 |
| 额外 | — | `scheduler_core.py` 裸 `import pytz` → SSOT ET | 1 |

## 三、SSOT 架构 (修复后)

```
app/utils/timezone.py          ← 唯一时区真源
  ├── ET (pytz America/New_York, fallback 固定偏移)
  ├── CST (pytz Asia/Shanghai)
  ├── UTC (timezone.utc)
  ├── now_et() / now_cst() / now_utc()
  ├── to_et() / to_cst() / to_utc()  (naive 假设: CST/ET/UTC)
  ├── _et_trade_date()               (aware→ET日历日, naive→CST-12h)
  ├── get_trade_date() / get_market_session() / is_market_open()
  ├── compare_safe() / is_expired_safe() / parse_iso_safe()
  └── market_phase_now() / signal_expires_at()

app/utils/datetime_utils.py    ← 向后兼容 shim (re-export from timezone.py)
```

**契约**:
- 所有 `trade_records.trade_date` / `exit_time` / `entry_time` 写入 = ET-aware ISO
- 所有 "今日交易日" 判定 = `get_trade_date()` 或 `_et_trade_date(now_et())`
- naive datetime 在 `to_et()` 中视为 CST, 在 `compare_safe()` 中视为 UTC (各自文档标注)
- pytz 时区 attach 一律用 `localize()`, 禁止 `replace(tzinfo=)`

## 四、验证

- `py_compile` 全部 134 文件 ✓
- 核心模块 import 验证: timezone / datetime_utils shim / models / trade_recorder / daily_loss_guard / plan_pipeline / heartbeat / pre_trade_gate / reconciler / position_recovery ✓
- `app/` 下 `utcnow()` 残留 = **0** (grep 确认)
- `timezone(timedelta(hours=-4))` 残留 = **0** (grep 确认)
- `replace(tzinfo=ET)` / `replace(tzinfo=CST)` 残留 = **0** (grep 确认)
- scheduler_core 原 `import pytz` 裸导入 → 改为 SSOT 后不再依赖 pytz 直接存在

## 五、对毕业数据的影响评估

- **IC 侧**: 无影响 (IC 基于 forward return = 入场价→N日后日线价, 时区修复不动价格)
- **PnL/胜率侧**: 无影响 (修复不改退出参数/触发逻辑/config_version; 只修"读哪天的交易"的尺子)
- **config_version**: 不变 (未触碰 factor_exit_rules / vol_buckets / cooldown / early_tight / fee_cushion)
- **口径注意**: 修复后 `symbol_daily_loss_guard` 读**真实 ET 当日**交易 (修复前 CST cutoff 可能多读/少读 12h 窗口); 熔断器已在 07-24 单独修复 (观察⑤), 本批是系统性补全

## 六、回滚

整体回滚: `git revert <commit>` (单 commit 包含全部 134 文件)
部分回滚: 各 Critical/High 项互不耦合, 可按文件独立 revert

## 七、方法教训

1. **时区 SSOT 必须单一入口**: 两个模块导出同名函数 (`to_et`) 但 naive 假设相反 = 定时炸弹。合并为 shim 消除歧义。
2. **硬编码偏移 = 冬令时必炸**: `timezone(timedelta(hours=-4))` 在 EST 期间差 1h, 且无法自动切换。一律用 pytz/zoneinfo。
3. **pytz 的 `replace(tzinfo=)` 是经典陷阱**: 产生 LMT (如 -4:56), 必须 `localize()`。
4. **读写契约必须对齐**: 写入 ET-aware ISO, 读取用 CST-naive cutoff 做字符串比较 = 偏移 12h。修复: 宽窗口 SQL + Python 侧 `_et_trade_date` 精确过滤。
5. **`utcnow()` 在 Python 3.12 已废弃**: 返回 naive UTC, 与 aware 比较会 TypeError。统一用 `now_utc()` (aware)。

## 八、生效安排

- 盘后 0 持仓窗口, 下次重启 `ftseek-us.service` 即生效
- 无需单独重启 (与既有凌晨修复批共享重启窗口)
- 验收判据: 重启后日志无 timezone 相关 Traceback; 心跳标题时间为 ET; 日亏守卫 restore 日志正确

## 九、独立审查结果 (Qoder, 2026-07-25 盘后)

**总结论: 批次完整合理, 发现并补修 2 处 H6 漏网 (grep 字面匹配漏别名), 已重启生效。**

### 逐项实证复核 (全部独立 grep/运行时验证)
| 项 | 声明 | 实测 | 结论 |
|---|---|---|---|
| H3/H4 | `utcnow()` 残留=0 | grep app/ = **0** | ✅ |
| C4/C5 | 硬编码 -4/-5/月份DST 残留=0 | grep = **0** (timezone.py fallback 除外, 合理) | ✅ |
| C1 | trade_recorder 二次转换 | diff 实核: `now_et()-12h` → `get_trade_date()` (aware→ET日历日) | ✅ 修复正确 |
| C2 | loss_guard 读写契约 | diff 实核: CST-cutoff → 48h 宽窗 + `_et_trade_date` 精确过滤; 异常行改 continue (丢弃不可解析行, 比旧 fallback 更保守, 合理) | ✅ |
| C3 | shim 合并 | 迁移函数 (compare_safe/is_expired_safe/parse_iso_safe/market_phase_now/signal_expires_at) 与旧版逐一语义对齐; **to_et 语义变更 (naive UTC→CST) 无真实调用方** (全仓 to_et 调用者本就 import 自 timezone.py; shim 实际消费者仅 models.py 的 is_expired_safe + 测试 3 处) | ✅ 风险解除 |
| H2 | heartbeat ET 统一 | diff 实核 + 运行时调用 `_query_today_trades()` 返回正确 (8笔/-30.85) | ✅ |
| H6 | `replace(tzinfo=ET/CST)`=0 | 字面匹配为 0, **但别名漏网 2 处**: pipeline.py:3980 `replace(tzinfo=_et_tz)` + shadow_tracker.py:506-507 `replace(tzinfo=_ET)` — 运行时实锤产生 **-04:56 LMT** (56min 误差, 影响 holding_hours 度量) | ⚠️ 已补修 |
| 额外 | scheduler_core pytz | diff 实核: 裸 import → SSOT ET | ✅ |
| — | 测试改动 | test_playbook_exit_rules.py 改动係 07-24 规则3.4批遗留 (非本批), 断言语义正确 | ✅ |

### 漏网补修 (同款 localize 处方, 均度量路径零交易行为变更)
1. `pipeline.py::_close_trade_record` L3980: `_et_tz.localize(naive)` (hasattr 兼容 fallback)
2. `shadow_tracker.py` L506-507: 同款 `_attach` lambda
- 其余 grep 命中的 `replace(tzinfo=)` 全部核实为 stdlib UTC/固定偏移/ZoneInfo (无 LMT 问题) 或 tzinfo=None 剥离, 安全。
- 验证: py_compile ✓; localize 实测 -04:00 ✓; test_playbook_exit_rules 1 passed ✓。

### 运行时验证 (审查方独立执行)
- compileall app/ 全量 rc=0; 热路径 import (models/loss_guard/heartbeat/trade_recorder) ✓
- 语义双路径: `_et_trade_date(aware)=ET日历日 / (naive)=CST-12h` ✓; `to_et(naive)` 无 LMT ✓; `CST.localize` 无 LMT ✓; shim re-export 同一对象 ✓
- `first_night_gate_report.py 2026-07-24` 重跑: 全部通过 (与修复前一致, 无度量回归) ✓
- 08:15 CST 重启 (含补修): active / 0 Traceback / ConfigGov 全绿 / 模拟盘 ✓

### 方法教训追加
6. **反模式 grep 必须覆盖别名**: 只查 `replace(tzinfo=ET)` 字面会漏 `tzinfo=_ET/_et_tz/et_tz` — 应用 `replace\(tzinfo=` 全量命中后逐一核实变量类型 (pytz vs stdlib)。
