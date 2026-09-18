# INCIDENT-001: 延长时段退出链「有持仓即崩」—— pandas Series 真值判定 (P0)

> **严重程度**: P0 (延长时段无任何退出决策能力; 零告警路径)
> **状态**: 已解决 (代码已修 + 生产实跑恢复)
> **开始时间**: 2026-09-18 09:38 ET (首次触发) / 2026-09-18 16:32 ET (盘后持续触发)
> **结束时间**: 2026-09-19 04:39 CST = 2026-09-18 16:39 ET (修复后首个 rc=0 轮)
> **响应人**: Hermes (用户令"请检查盘后交易链路和记录是否正常"触发排查)
> **引入提交**: `66d781dcc` [hotfix][DEB-20260917 §22] 延长时段利润保护链路 + MFE 自适应 trailing (SIM-only)

---

## 事件摘要

延长时段 (盘前/盘后/next_open) 退出链在**存在任何持仓**时 100% 崩溃: `quote_metrics()`
对 futu 快照行走真值判定 (`snapshot.get(k) if snapshot else None`), 而快照行是 **pandas
Series** ⇒ `Series.__bool__` 抛 `ValueError: The truth value of a Series is ambiguous`。
崩点位于 `collect()` 的第一个持仓处理行 ⇒ 退出决策 / 观测 jsonl / 收口强平 / next_open
兑现全部不执行, 且每分钟一轮, **无任何告警** (现有 `error_alert.py` 只认 `] ERROR:` 字面量,
Traceback 文本不含该标记 —— 该失败模式对现有告警链完全隐形)。

窗口内无持仓时该路径不被触达, 因此自 9/18 引入起一直表现为"正常空转"。

---

## 时间线

| 时间 (ET) | 事件 | 操作 |
|------|------|------|
| 9/18 09:38–09:44 | 首次触发 (next_open 窗, 7 轮 rc=1) | 无人在看: 日志只记 rc, 不告警 |
| 9/18 16:30:15 | 盘后 entry 实弹开仓 US.ACN 25股 @192.64 (FILLED_ALL) | — |
| 9/18 16:31:37 | 本地双写完成 (trade_records 1543 + open_positions) | — |
| 9/18 16:32–16:38 | 盘后退出链每分钟 rc=1 (同样 ValueError) | 用户于 16:26 令"检查盘后交易链路和记录" ⇒ 排查中定位 |
| 9/19 04:3x | 根因定位 (blame 到 66d781dcc; 本地 --dry 复现) | 单点修复 `_snapshot_field` |
| 9/19 04:39 | 修复后首个运行 rc=0, 落 1 行 `hold_to_next_open` | 生产恢复正常 |

---

## 影响范围

**资金影响: 0 (本窗口)** —— 崩窗内 ACN 的应然动作正是 `hold_to_next_open` (盘后进场
carry 仓停用三信号), 且当时 `pnl≈-0.07%`, TP/BE/TRAIL 均不可能武装 ⇒ 未错失任何止盈。

**真实缺口 (不因"这次恰好无损失"而淡化)**:

1. **有浮盈时会完全失去盈利保护**: 若崩窗内该仓处于 +TP 或已武装 BE/TRAIL 状态,
   三个信号全部无法执行 (延长时段唯一的利润保护手段);
2. **`next_open` 兑现路从未在有持仓时真正跑过**: 该窗 (09:30–09:45) 专门用于兑现
   盘后 carry 仓, 一旦有对象就会崩 ⇒ 9/17 恰无盘后 carry 仓, 才未造成实际漏平;
3. **观测台账出现空洞**: 崩窗内每分钟少 1 行 (MFE/捕获率/费用读数);
4. **静默失败**: rc=1 无告警 (见摘要), 是本次无法自动发现、需人工排查的直接原因;
5. 每分钟一轮 OpenD 连接白烧 (16:32–16:38 共 7 轮 + 09:38–09:44 共 7 轮)。

---

## 根因

```python
# scripts/extended_hours_market_session.py (修复前)
def quote_metrics(snapshot, direction, now=None):
    bid = _number(snapshot.get("bid_price")) if snapshot else None   # ← 崩在这里
```

调用方 `afterhours_exit_shadow.py:188` / `premarket_exit_shadow.py:275` 传的 `s` 来自
`ctx.get_market_snapshot([...])` 的 `snap.iterrows()` ⇒ `s` 是 pandas Series (非 dict)。

---

## 修复 (单点, 覆盖 3 个调用方)

`scripts/extended_hours_market_session.py` 新增 None-safe 取字段助手 `_snapshot_field()`,
把三处 `if snapshot` 真值判定改为显式 `is None` + `try/except` 兜底:
盘前 / 盘后 / next_open 三条链共用该助手 ⇒ 一处修复三处生效。

**回归钉**: `tests/test_extended_hours_market_session.py::test_quote_metrics_accepts_pandas_series_snapshot_and_none`
—— Series 入力不得抛 / None 全空 / NaN 盘口静默降级 / dict 与 Series 同值。
变异反证: 旧表达式在 Series 上确实抛 ValueError (已实测), 故该测试对回潮敏感。

---

## 验证 (实测, 不采信推断)

| 项 | 证据 |
|---|---|
| 单元 | 该测试文件 **9 passed**; 延长时段相关子集 **185 passed** (62s) |
| 变异 | 旧写法 `s.get('bid_price') if s else None` 在 `pd.Series` 上抛 ValueError; 新助手返回 1.0 |
| 实弹 (生产持仓) | `afterhours_exit_shadow.py --dry` → `[dry] US.ACN ... → hold_to_next_open` + 落 1 行 (此前同命令 100% Traceback) |
| 生产 | 16:39/16:40/16:41 三轮 cron **rc=0**, 每轮落 1 行; jsonl 行含 bid/ask/mid/spread/executable/executor_limit_price/fee/risk_r/adaptive 快照 |
| 一致性 | 4 行 action 序列全为 `hold_to_next_open` = 盘后 carry 仓设计语义 |

生效形态: 脚本为 cron 直接 exec (无进程缓存) ⇒ 修复即时生效, 无需重启主服务。

---

## 后续项 (待裁决/待观察, 不在本批改动范围)

1. **P1 — rc≠0 哨兵缺失**: `extended_hours_exit_cron` 连续多轮 rc≠0 应立即告警。
   现网 `error_alert.py` 只匹配 `] ERROR:` / `] CRITICAL:` 字面量, 抓不到 Traceback
   类失败 ⇒ 延长时段/影子类 cron 的崩溃对告警链隐形 (本次靠人工排查才发现)。
2. **P2 — `next_open` 窗的作用域**: 该窗按"非手工仓"全量加载 (含 RTH 仓), 修复后
   首次会在 09:30–09:45 对 RTH 仓产生评估行 (执行侧受 `_exec_ok = in_window or
   action=="next_open_exit"` 约束, **不会**对 RTH 仓下单)。下个交易日需观察:
   ①`premarket_exit_shadow.jsonl` 是否新增 09:3x 行 ②统一结算器聚合是否受影响。
3. **P3 — 崩溃期的观测空洞**: 9/18 16:32–16:38 与 09:38–09:44 各 ~7 行缺失,
   时段收益台账按持仓日聚合时属轻微低估, 无需回填 (无成交, 仅观测)。

---

## 教训

- **"无数据/无对象"路径长期掩盖真 bug**: 该链自 9/18 引入起从未在有持仓时验证过 (窗口
  内恰好一直空仓)。**新链路上线验收必须造一个"有对象"的真实场景**, 否则等于没验收。
- **pandas 真值判定是静默 P0 的家常形态**: `if <可能是 Series/DataFrame> 对象` 在 dict
  上工作正常, 换 Series 即抛; 单测喂 dict 全绿。审任何"取快照字段"的新代码时,
  显式 `is None` + 单测喂 **Series 行本体** (与生产同为 `iterrows()` 产物)。