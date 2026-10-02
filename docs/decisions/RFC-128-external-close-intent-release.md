# RFC-128: external_close 对账同步释放 exit_intent (手动单自愈闭环)

- 日期: 2026-10-02
- 类型: L1 — 功能修复 (账目语义补全, 零交易行为变更)
- 状态: 已实施 (用户 2026-10-02 裁「按建议执行」)
- 前置: RFC-127 (ETH HELD→RTH 升级影子, `0f563afdc`) — 本 RFC 补其 §6 遗留的人工处置流程的自动化面
- 回滚: 单提交 revert (不涉及 schema 变更 / 不涉及配置键)

## 1. 问题 (Gate 1: 四问)

### 1.1 改什么

| 文件 | 改动 |
|---|---|
| `app/execution/exit_lifecycle.py` | `ExitLedger` 新增 `release_unsettled()` 方法 (SSOT, 与 `ack_booked` 同级) |
| `app/core/position_sync.py` | `_sync_to_position_store_locked()` 的**两处**删仓分支调用它: external_close (L1911 区) + 真幽灵仓 (L1944 区) |
| `tests/test_exit_intent_external_release.py` | 新增契约测试 |

### 1.2 为什么改 (实际, 非推测)

2026-10-01 US.ACN SHORT 23股 首例, 全程三方实证 (日志 + 两库 + 券商):

1. 盘前 09:25 ET 收口强平限价腿 60s 零成交被撤 → intent `7b149410…` 进 `exit_eth_partial_fill_held` (HELD);
2. 用户随后在富途 App 手动挂平仓单 (oid `8219620`, BUY 23@209, ETH/DAY), 8 小时未成交后自行撤单;
3. 该仓被外部渠道处置时, `position_sync` 的 external_close 对账**只写 `trade_records` + DELETE `open_positions`, 完全不碰 `exit_intents`** (grep 零命中, 已实证);
4. 后果链 (每一条都有命令级证据):
   - intent 永久卡 `PENDING`/HELD → `_resume_playbook_exits` 把该 symbol 加进 `blocked_exits` → **主退出循环对其 `continue`, SL 评估被整体跳过**;
   - fast loop 每 ~10s 告警一次, 当日 **2029 条 ERROR** (`EXIT_PENDING … 判定为真卡单`);
   - RFC-127 影子读 `result.dealt_qty` (= 系统自己那条已撤腿的成交量 0), 不是券商真实持仓 → 每轮落一条「剩余 23 股可升级」**幽灵样本**, 会把阶段1 判据2 (命中意图 100% 满足剩余>0) 顶成**假 PASS**;
   - 需人工跑 `scripts/release_stuck_exit_intent.py` 才能解 (当日 08:27 CST 执行, 告警实测停止)。

### 1.3 不改会怎样 (量化)

- **风险敞口**: 10/1 ACN SL=223.16 被跳过期间, 价格冲到 227.63 (越线 2.0pp)。若 SL 生效亏 −244 (可控), 实际无保护账面最坏 −347 ⇒ **多暴露 42%**。且这是**运气** (午后回落到 212.30, 现价了结 +5);
- **每次外部平仓都要人工介入**: 告警风暴 (2029 条/日) + 我全链路取证 30-60 分钟;
- **攒样判据被污染**: RFC-127 阶段1 需 ≥5 交易日 ∧ ≥3 独立意图, 幽灵样本使判据不可信 ⇒ 攒样期作废重来。

### 1.4 改错会怎样 / 回滚成本

- 释放语义 = `state → 'BOOKED'`, 与既有 `ack_booked()` 完全同一终态, **不新造状态值**;
- `BOOKED` 同时满足: ①退出 `list_unsettled()` 枚举 (`WHERE state != 'BOOKED'`) ⇒ 不再进 `blocked_exits` ⇒ SL 兜底重新接管; ②解除 partial unique index `exit_active_symbol … WHERE state != 'BOOKED'` 的 symbol busy ⇒ 该标的新意图可再建;
- 释放**不会自动平仓** (纯账目), 平仓职责交回 SL/BE/trailing/EOD 兜底链;
- 回滚 = revert 单提交 + 重启 (约 2 分钟)。

## 2. Gate 2: 方案对比

| 方案 | 描述 | 判定 |
|---|---|---|
| **A (选定)** | `ExitLedger.release_unsettled()` SSOT 方法, position_sync **两处**删仓分支调用 | ✅ 数据所有者持有写操作 (与 `ack_booked` 同级); 两处共用一函数不漂移; 源头修 (对账已经知道"仓被外部平了", 顺手释放 = 同一处信息, 不新增检测层) |
| B | external_close 分支内联 SQL UPDATE | ❌ 两处 (external_close + 真幽灵仓) 各写一份 = SSOT 反模式 (协议 Gate 4 明令: 同一逻辑 3+ 处 inline ⇒ 提取集中函数) |
| C | 新建定时对账环扫「`open_positions` 无仓 ∧ intent 未决」释放 | ❌ 新建常驻载体 (违反做减法); 延迟一个 cron 周期; 且判据面比 A 宽 (会释放 A 覆盖不到的场景, 但那部分本就该人工核 — 见 §4 边界) |
| D | 不改, 维持人工释放 | ❌ 用户明确要求「未来手动下单能自己管理」 |

**极简优先检查** (协议 Gate 2): 无增强层 — 一个方法 + 两处调用 + 一条日志。核心改动 ~35 行。

## 3. Gate 3: 规格

### 3.1 成功标准 (可验证)

1. 券商侧该 symbol 无持仓 + 存在未决 intent (symbol/direction/env/account 四键匹配) ⇒ 该 intent `state='BOOKED'`, `receipt_json` 含 `external_close_release=true` + `reason` + `released_at` + `broker_qty_evidence`;
2. 释放后 `list_unsettled()` 不再含该 intent ⇒ 下一轮 `_resume_playbook_exits` 不把该 symbol 加进 `blocked_exits`;
3. 幂等: 同一 intent 重复调用返回空列表, 不重复写 (`WHERE state != 'BOOKED'` 天然保证);
4. 四键不匹配 (方向相反 / 他账户 / 他 env) ⇒ **零释放** (防误释放还有仓的意图);
5. ledger 不可达 / 异常 ⇒ **不阻断对账主流程** (WARNING + 返回空), 因为 external_close 的 trade_records 写入与通知更重要;
6. 任一腿 `state != TERMINAL` 或 `dealt_qty IS NULL` ⇒ **零释放** + WARNING 留痕 (先撤销/等待该腿终态, 防止活跃出场单因意图 BOOKED 变孤儿单);
7. 两处调用点都在 (源码契约测试钉住), 漏一处必红。

### 3.2 边界条件 (什么情况下不生效)

| 场景 | 行为 | 依据 |
|---|---|---|
| intent 已是 `BOOKED` | 跳过 (幂等) | `WHERE state != 'BOOKED'` |
| direction 不匹配 (intent=LONG, 平掉的是 SHORT 仓) | 不释放 | 四键匹配; 防同 symbol 双向意图误伤 |
| account_id / target_env 不匹配 | 不释放 | partial unique index 是 `(account_id, target_env, symbol)` ⇒ **按账户隔离**; 而"券商侧无持仓"是按账户查的 ⇒ 只按 symbol 匹配会误释放另一账户 (仓位可能还在) 的意图 |
| 意图下存在 `PREPARED/ACTIVE/CANCEL_REQUESTED` 腿, 或 TERMINAL 腿 `dealt_qty` 未知 | 不释放 | `orphan_ext_order_watch` 只认 `ext_exit_` remark, playbook 腿是 `pbe_` 前缀 ⇒ 释放意图会让该单脱离现有看护; 先人工撤单/等终态后再释放 |
| 部分平仓 (券商侧仍有剩余持仓) | **不触发** | external_close 的前置是 `ghost_symbols = mem_symbols - futu_symbols` ⇒ 只在券商侧**完全无持仓**时进此分支 (已实证); 部分平仓走 `_sync_once` 的数量变化分支, 不在本 RFC 范围 |
| `_sync_once` 的 L994/L1170 删仓点 | **不改** | L994 有 `playbook_open_check(symbol, _pb_open_syms)` 跳过 playbook 仓 ⇒ playbook/EXT 仓不在此删; L1170 是期权 PRICE_FAIL 清理, 与 exit_intent 无关 |

### 3.3 不做什么 (防范围蔓延)

- ❌ 不给手动单加 remark 命名空间 (App 手动单填不了 remark ⇒ 方案不成立, 已论证);
- ❌ 不改 `preflight` 的 `exit_unowned_active_order` 检测 (它不认 remark 是**正确的 fail-closed**: 有活跃平仓单时不重复下单, 防双平);
- ❌ 不动 RFC-127 影子的判据或阶段1 门槛 (守卫已在 `0f563afdc` 落地, 本 RFC 只消除幽灵源);
- ❌ 不自动撤销/接管用户的手动单 (系统依然不碰);
- ❌ 不释放 `scripts/release_stuck_exit_intent.py` 覆盖的场景 (ETH 限价腿超时被撤而**仓位仍在** ⇒ 那需要人工判断"交回 SL 兜底还是继续等", 保留人工工具)。

### 3.4 依赖

- `app.core.trading_mode.TradingMode` → `target_env()` / `account_id()` (SSOT, 实测 `'sim'` / `'19930823'` 与 ledger 值域逐字一致);
- `app.core.paths.get_exit_ledger_db()` → ledger 路径;
- 需重启 (`app/` 常驻代码, HotReloader 穿不透); 重启时点由用户裁 (军规: 重启必跑 `startup_smoke_check.py`)。

## 4. 与 RFC-127 / 人工工具的分工 (三者不重复)

| 场景 | 归属 |
|---|---|
| 外部渠道**成交**平掉仓 (App 手动单成交 / 券商侧成交) | **本 RFC**: 对账时自动释放, 零人工 |
| ETH 限价腿超时被撤而**仓位仍在** (10/1 ACN 首例) | `scripts/release_stuck_exit_intent.py` (人工判断: 交回 SL 兜底 or 继续等) |
| HELD 意图在 RTH 是否该自动升级市价补腿 | RFC-127 影子攒样 → 阶段1 裁决 |

## 5. Gate 5/6 验证记录

见提交信息与本文件 §6。

## 6. 实施回执

- 提交: (见 git log `[RFC-128]`)
- 测试: `tests/test_exit_intent_external_release.py`
- 重启: 2026-10-02 10:59:44 CST 已部署; `startup_smoke_check.py --wait 90` 7/7 全绿 (exit=0)
- 复查补丁 (2026-10-02): `release_unsettled()` 增加非终态腿守卫;
  `scripts/release_stuck_exit_intent.py` 同步拒绝非终态/成交未知腿, 防人工释放
  制造 `pbe_*` 孤儿单。契约测试新增 4 例参数化场景。
