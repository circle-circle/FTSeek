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

## 后续项执行状态 (2026-09-19 05:00 CST, 用户裁"按建议执行")

**P1 — rc≠0 告警可见性: ✅ 已落地** (复用既有告警链, 未新建哨兵)

| 件 | 内容 | 验证 |
|---|---|---|
| 源头 (契约行) | `scripts/extended_hours_exit_cron.py` 新增 `_report_failure()`: 子进程失败时**另发**一行 `YYYY-MM-DD HH:MM:SS [extended_hours_exit_cron] ERROR: …` (JSON 块保留) | 新测试 3 例; 行样例见下 |
| 覆盖窗 | crontab 补 `*/15 16-20 * * 1-5` (ET 04:00-08:45 盘前) + `*/15 5-8 * * 2-6` (ET 17:00-20:45 盘后); 原两行只覆盖 ET 09:00-17:45 | 安全编辑: 备份 `/tmp/crontab_bak_20260919_045638.txt` → diff `+8/-0` → 应用 → 回读 2/2 ✅ |
| 判据复用 | 不新建计数器: 现有 `15min 窗计数 ≥ max(3, ⌈3×近 7 日同窗中位数⌉)` 即"连续多轮失败"的等价判据 | 两向: n=2 → `2 条 < 阈值 3` 静默; n=3 → `触发` + 生成推送正文 (dry-run 实跑) ✅ |

实测行样例 (真实代码路径产出):
```
2026-09-19 04:53:07 [extended_hours_exit_cron] ERROR: 退出链子进程失败 session=afterhours 16:58ET rc=1 | ValueError: The truth value of a Series is ambiguous @ File ".../afterhours_exit_shadow.py", line 188, in collect (完整 stderr 见本文件 JSON 块)
```
生产日志现状反证零告警成因: 崩溃期 **67 个 JSON 块 / 0 条契约 ERROR 行**。

台账: `docs/CONTROL-LOOP-LEDGER.md` 新增 `crontab-error-alert` 行 (含 4 窗调度 + 判据 + 失效模式 + 回滚)。
⚠️ 冬令时 (11/1 起) CST 相对 ET 右移 1h ⇒ 本批两窗列入 11 月重评清单。

**P2 — next_open 窗作用域观察: ✅ 已排程** (Hermes 一次性 job `e9ea6362ebd9`, once 2026-09-21 21:50 CST = ET 09:50, 紧随 09:30-09:45 窗关闭; 全程只读, 已入台账)。该项同时是 **next_open 兑现路的首考** (此前每次有持仓即崩 ⇒ 从未真正跑过)。

**P3 — 观测空洞: ⛔ 不回填** (崩窗各缺 ~7 行, 无成交, 按持仓日聚合仅轻微低估)。

---

## 教训

- **"无数据/无对象"路径长期掩盖真 bug**: 该链自 9/18 引入起从未在有持仓时验证过 (窗口
  内恰好一直空仓)。**新链路上线验收必须造一个"有对象"的真实场景**, 否则等于没验收。
- **pandas 真值判定是静默 P0 的家常形态**: `if <可能是 Series/DataFrame> 对象` 在 dict
  上工作正常, 换 Series 即抛; 单测喂 dict 全绿。审任何"取快照字段"的新代码时,
  显式 `is None` + 单测喂 **Series 行本体** (与生产同为 `iterrows()` 产物)。